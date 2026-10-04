"""Standard event records (``event_record``) streamed from the SwarmScope store.

One filter vocabulary, shared by sweeps and exports:

    source   str | [str]   only these sources (e.g. "village")
    kind     str | [str]   only these schema kinds: "msg" (messages) or "event" (actions);
                           "source:kind" is accepted too
    type     str | [str]   only these dataset types: ``messages.msg_type`` or ``actions.kind``
                           (e.g. "commit", "revision", "SEND_MESSAGE")
    channel  str           only messages in this channel (actions have no channel, so they drop out)
    author   str           agent name/alias/id, "human" (every human) or "human:<id>"
    since    str           inclusive UTC start (ISO date or datetime)
    until    str           exclusive UTC end; a bare date includes that whole day
    query    str           case-insensitive substring of the text

Records come from ``messages`` and ``actions`` in time order (ts, then evidence id).
``store_records`` yields them lazily (``count_store_records`` counts them); ``StoreRecordProvider`` is the
``sweep.RecordProvider`` the scope module registers as ``"store"``; ``export_store``
feeds them to ``export.export`` with a ``redact.Redactor``. ``from_store_record`` turns a
``store_api["get_record"]`` result (a message, action or period) into the same record shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from swarm_mcp.scope import db, evidence
from swarm_mcp.scope.analysis.timeline import record_filters, ts_iso
from swarm_mcp.toolkit import ToolInputError, parse_time, truncate

FILTER_KEYS = ("source", "kind", "type", "channel", "author", "since", "until", "query")
RECORD_KINDS = {"msg": "messages", "event": "actions"}
RECORD_KEYS = ("event_id", "source", "kind", "time", "actor", "actor_type", "location", "text")


def event_record(
    event_id: str,
    *,
    time: str | None,
    actor: str | None,
    actor_type: str | None = None,
    location: str | None = None,
    text: str = "",
    truncated: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """The standard record shape sweeps and exports work on (``RECORD_KEYS`` first, then ``extra``).

    ``event_id`` is a store evidence id (``evidence.parse`` validates it); ``kind`` is its schema kind."""
    ref = evidence.parse(event_id)
    d: dict[str, Any] = {
        "event_id": event_id,
        "source": ref.source,
        "kind": ref.kind,
        "time": time,
        "actor": actor,
        "actor_type": actor_type,
        "location": location,
        "text": text,
    }
    if truncated:
        d["truncated"] = True
    d.update({k: v for k, v in extra.items() if v is not None})
    return d


_BATCH = 1000

_SELECT = {
    "messages": "SELECT evidence_id, ts, channel AS location, author_id AS who, content, msg_type AS subtype, meta, "
    "'messages' AS tbl FROM messages",
    "actions": "SELECT evidence_id, ts, NULL AS location, agent_id AS who, content, kind AS subtype, meta, "
    "'actions' AS tbl FROM actions",
}


def _as_list(value: Any, key: str) -> list[str]:
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ToolInputError(f"filter {key!r} must be a string or a list of strings")


def _one(value: Any, key: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ToolInputError(f"filter {key!r} must be a string")
    return value


def _meta(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def check_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    """The non-empty filters, or ``ToolInputError`` for unknown keys."""
    f = {k: v for k, v in dict(filters or {}).items() if v not in (None, "", [])}
    unknown = sorted(set(f) - set(FILTER_KEYS))
    if unknown:
        raise ToolInputError(f"Unknown filter(s) {', '.join(unknown)}. Filters: {', '.join(FILTER_KEYS)}.")
    return f


def store_records(
    db_path: Path,
    filters: Mapping[str, Any] | None = None,
    *,
    limit: int | None = None,
    max_chars: int | None = None,
    mask: Callable[[str], str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield standard event records from the store, oldest first.

    ``max_chars`` caps each text (None = full text); ``mask`` is applied to the text before the cap
    (sweeps pass the server's scrubber; exports pass nothing and redact everything afterwards).
    The read-only connection stays open while the generator is consumed."""
    f = check_filters(filters)
    with db.connect(db_path) as s:
        parts, params = _select(s, f)
        if not parts:
            return
        sql = " UNION ALL ".join(parts) + " ORDER BY ts NULLS LAST, evidence_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        names = s.display_names()
        cur = s.con.execute(sql, params)
        cols = [d[0] for d in cur.description or []]
        while True:
            rows = cur.fetchmany(_BATCH)
            if not rows:
                break
            for row in rows:
                yield _record(dict(zip(cols, row, strict=True)), names, max_chars, mask)


def count_store_records(db_path: Path, filters: Mapping[str, Any] | None = None) -> int:
    """How many records ``store_records(db_path, filters)`` would yield without a limit (same WHERE)."""
    f = check_filters(filters)
    with db.connect(db_path) as s:
        parts, params = _select(s, f)
        if not parts:
            return 0
        row = s.con.execute(f"SELECT count(*) FROM ({' UNION ALL '.join(parts)})", params).fetchone()
    return int(row[0]) if row else 0


def _select(s: Any, f: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
    """(one SELECT per table, their params) for checked filters ``f`` on an open store session ``s``."""
    sources = _as_list(f.get("source"), "source")
    kind_specs = []  # (source or None, schema kind): "source:kind" keeps its source
    for k in _as_list(f.get("kind"), "kind"):
        src, _, kind = k.rpartition(":")
        kind_specs.append((src or None, kind))
    kinds = [k for _src, k in kind_specs]
    bad_kinds = sorted({k for k in kinds if k not in RECORD_KINDS})
    if bad_kinds:
        raise ToolInputError(
            f"Unknown record kind(s) {', '.join(bad_kinds)}: kind is 'msg' (messages) or 'event' (actions). "
            "A dataset's own type (e.g. 'commit', 'revision') goes in the 'type' filter."
        )
    types = _as_list(f.get("type"), "type")
    query = _one(f.get("query"), "query")
    lo = parse_time(_one(f.get("since"), "since"), field="since")
    hi = parse_time(_one(f.get("until"), "until"), end=True, field="until")
    if lo and hi and lo >= hi:
        raise ToolInputError(f"since ({f.get('since')}) must be before until ({f.get('until')})")
    known = {r["source"] for r in s.all("SELECT source FROM sources")}
    bad = [x for x in [*sources, *(src for src, _k in kind_specs if src)] if x not in known]
    if bad:
        raise ToolInputError(f"Unknown source(s) {', '.join(bad)}. Sources: {', '.join(sorted(known)) or 'none'}")
    one_source = sources[0] if len(sources) == 1 else None
    channel = s.resolve_channel(_one(f.get("channel"), "channel"), one_source)
    author = s.author_filter(_one(f.get("author"), "author"), one_source)
    parts: list[str] = []
    params: list[Any] = []
    for table in ("messages", "actions"):
        if channel is not None and table == "actions":
            continue
        if kinds and not any(RECORD_KINDS[k] == table for k in kinds):
            continue
        where, p = record_filters(table, channel=channel, since=lo, until=hi)
        if sources:
            where.append(f"source IN ({', '.join('?' * len(sources))})")
            p += sources
        if kind_specs:
            kind_sources = [src for src, k in kind_specs if RECORD_KINDS[k] == table]
            if None not in kind_sources:  # only qualified kinds for this table: keep their sources
                where.append(f"source IN ({', '.join('?' * len(kind_sources))})")
                p += kind_sources
        if types:
            col = "msg_type" if table == "messages" else "kind"
            where.append(f"{col} IN ({', '.join('?' * len(types))})")
            p += types
        if author is not None:
            sql, ap, _ = author
            where.append(sql.replace("author_id", "agent_id") if table == "actions" else sql)
            p += ap
        if query:
            where.append("content ILIKE ? ESCAPE '\\'")
            p.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        parts.append(f"{_SELECT[table]} WHERE {' AND '.join(where) or 'TRUE'}")
        params += p
    return parts, params


def _record(
    r: dict[str, Any], names: dict[str, str], max_chars: int | None, mask: Callable[[str], str] | None
) -> dict[str, Any]:
    text = r["content"] or ""
    if mask is not None:
        text = mask(text)
    cut = False
    if max_chars is not None:
        text, cut = truncate(text, max_chars)
    who = r["who"]
    extra = {"actor_id": who, ("msg_type" if r["tbl"] == "messages" else "action_kind"): r["subtype"]}
    return event_record(
        r["evidence_id"],
        time=ts_iso(r["ts"]),
        actor=db.label_for(who, names),
        actor_type=_actor_type(who, _meta(r["meta"]), names),
        location=r["location"],
        text=text,
        truncated=cut,
        **extra,
    )


def _actor_type(who: Any, meta: Mapping[str, Any], agents: Mapping[str, Any] | None = None) -> str:
    """meta.actor_type, else from the id: human:..., an agent (in ``agents`` when given), "unknown" for none,
    else "external" (an unmatched actor is not an agent)."""
    if meta.get("actor_type"):
        return str(meta["actor_type"])
    if str(who).startswith("human:"):
        return "human"
    if not who or who == "unknown":
        return "unknown"
    return "agent" if agents is None or who in agents else "external"


def _untrusted_text(value: Any) -> tuple[str, bool]:
    """(text, truncated) of a ``{"content", "untrusted", "truncated"?}`` field from ``get_record``."""
    if isinstance(value, Mapping):
        return str(value.get("content") or ""), bool(value.get("truncated"))
    return str(value or ""), False


def from_store_record(d: Mapping[str, Any]) -> dict[str, Any]:
    """A standard record from a ``store_api["get_record"]`` result (messages, actions, periods).

    The text is ``content["content"]`` (already masked and capped by get_record). Agents and artifacts
    have no text to judge, so they raise ``ToolInputError``."""
    table, eid = d.get("table"), d.get("evidence_id")
    meta = _meta(d.get("meta"))
    if table == "messages":
        text, cut = _untrusted_text(d.get("content"))
        who = d.get("author_id")
        return event_record(
            eid, time=d.get("ts"), actor=d.get("author"), actor_type=_actor_type(who, meta),
            location=d.get("channel"), text=text, truncated=cut, actor_id=who, msg_type=d.get("msg_type"),
        )  # fmt: skip
    if table == "actions":
        text, cut = _untrusted_text(d.get("content"))
        who = d.get("agent_id")
        return event_record(
            eid, time=d.get("ts"), actor=d.get("agent"), actor_type=_actor_type(who, meta),
            text=text, truncated=cut, actor_id=who, action_kind=d.get("kind"),
        )  # fmt: skip
    if table == "periods":
        text, cut = _untrusted_text(d.get("label"))
        return event_record(
            eid, time=d.get("start"), actor=None, text=text, truncated=cut, period_kind=d.get("kind"), end=d.get("end")
        )
    raise ToolInputError(f"{eid} is a {table} record with no text to evaluate; use message, action or period ids.")


def store_agents(db_path: Path, sources: list[str] | None = None) -> list[dict[str, Any]]:
    """Agent rows (optionally of some sources) as plain dicts, for exports."""
    with db.connect(db_path) as s:
        where = f" WHERE source IN ({', '.join('?' * len(sources))})" if sources else ""
        rows = s.all(
            "SELECT agent_id, source, display_name, aliases, first_seen, last_seen, meta FROM agents"
            + where
            + " ORDER BY agent_id",
            list(sources or []),
        )
    return [
        {
            "agent_id": r["agent_id"],
            "source": r["source"],
            "display_name": r["display_name"],
            "aliases": list(r["aliases"] or []),
            "first_seen": ts_iso(r["first_seen"]),
            "last_seen": ts_iso(r["last_seen"]),
            "meta": _meta(r["meta"]),
        }
        for r in rows
    ]


class StoreRecordProvider:
    """``sweep.RecordProvider`` over the store: ``filters`` use the vocabulary in this module's docstring."""

    def __init__(self, db_path: Path | Callable[[], Path], *, max_chars: int | None = None, mask=None):
        self._db_path = db_path
        self.max_chars = max_chars
        self.mask = mask

    @property
    def db_path(self) -> Path:
        return self._db_path() if callable(self._db_path) else self._db_path

    def iter_records(self, filters: Mapping[str, Any], limit: int) -> list[dict[str, Any]]:
        # materialized, so the read-only connection is closed before the caller does slow work
        return list(store_records(self.db_path, filters, limit=limit, max_chars=self.max_chars, mask=self.mask))

    def count(self, filters: Mapping[str, Any]) -> int:
        """How many records match ``filters`` in all (``iter_records`` returns the oldest ``limit`` of them)."""
        return count_store_records(self.db_path, filters)


def export_store(
    db_path: Path,
    out_dir: Path,
    filters: Mapping[str, Any] | None = None,
    *,
    allow_email_domains: tuple[str, ...] | list[str] = (),
    rules: str | list[str] = "default",
    with_agents: bool = False,
    check: bool = True,
) -> dict[str, Any]:
    """Export the filtered store records (full text) through ``export.export`` with a ``Redactor``,
    then (by default) re-check the export with the strictest rules. Returns a summary dict
    (counts only) with ``check`` holding the check report when it ran."""
    from swarm_mcp import export as exp
    from swarm_mcp.redact import Redactor

    f = check_filters(filters)
    redactor = Redactor(rules, allow_email_domains=list(allow_email_domains))
    agents = store_agents(db_path, _as_list(f.get("source"), "source")) if with_agents else None
    manifest = exp.export(
        store_records(db_path, f),
        out_dir,
        redactor,
        agents=agents,
        filters_desc={"from": "swarmscope store", **f},
    )
    out: dict[str, Any] = {
        "out_dir": str(out_dir),
        "records": manifest["records"],
        "redaction": {
            k: manifest["redaction"][k] for k in ("rules", "allow_email_domains", "counts", "records_changed")
        },
    }
    if check:
        out["check"] = exp.check(out_dir).to_dict()
        out["ok"] = out["check"]["ok"]
    return out
