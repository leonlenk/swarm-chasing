"""Standard event records (``events.event_record``) streamed from the SwarmScope store.

One filter vocabulary, shared by sweeps and exports:

    source   str | [str]   only these sources (e.g. "village")
    kind     str | [str]   only these evidence-id kinds ("chat", "event", or a mapped kind such as
                           "post"); "source:kind" is accepted too
    channel  str           only messages in this channel (actions have no channel, so they drop out)
    author   str           agent name/alias/id, "human" (every human) or "human:<id>"
    since    str           inclusive UTC start (ISO date or datetime)
    until    str           exclusive UTC end; a bare date includes that whole day
    query    str           case-insensitive substring of the text

Records come from ``messages`` and ``actions`` in time order (ts, then evidence id).
``store_records`` yields them lazily; ``StoreRecordProvider`` is the
``sweep.RecordProvider`` the scope module registers as ``"store"``; ``export_store``
feeds them to ``export.export`` with a ``redact.Redactor``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from swarm_mcp.events import event_record
from swarm_mcp.scope import db
from swarm_mcp.scope.analysis.timeline import record_filters, ts_iso
from swarm_mcp.toolkit import ToolInputError, parse_time, truncate

FILTER_KEYS = ("source", "kind", "channel", "author", "since", "until", "query")
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
    sources = _as_list(f.get("source"), "source")
    kinds = [k.split(":", 1)[1] if ":" in k else k for k in _as_list(f.get("kind"), "kind")]
    query = _one(f.get("query"), "query")
    lo = parse_time(_one(f.get("since"), "since"), field="since")
    hi = parse_time(_one(f.get("until"), "until"), end=True, field="until")
    if lo and hi and lo >= hi:
        raise ToolInputError(f"since ({f.get('since')}) must be before until ({f.get('until')})")
    with db.connect(db_path) as s:
        known = {r["source"] for r in s.all("SELECT source FROM sources")}
        bad = [x for x in sources if x not in known]
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
            where, p = record_filters(table, channel=channel, since=lo, until=hi)
            if sources:
                where.append(f"source IN ({', '.join('?' * len(sources))})")
                p += sources
            if kinds:
                where.append(f"split_part(evidence_id, ':', 2) IN ({', '.join('?' * len(kinds))})")
                p += kinds
            if author is not None:
                sql, ap, _ = author
                where.append(sql.replace("author_id", "agent_id") if table == "actions" else sql)
                p += ap
            if query:
                where.append("content ILIKE ? ESCAPE '\\'")
                p.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
            parts.append(f"{_SELECT[table]} WHERE {' AND '.join(where) or 'TRUE'}")
            params += p
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
    meta = _meta(r["meta"])
    actor_type = meta.get("actor_type") or ("human" if str(who).startswith("human:") else "agent")
    extra = {"actor_id": who, ("msg_type" if r["tbl"] == "messages" else "action_kind"): r["subtype"]}
    return event_record(
        r["evidence_id"],
        time=ts_iso(r["ts"]),
        actor=db.label_for(who, names),
        actor_type=actor_type,
        location=r["location"],
        text=text,
        truncated=cut,
        **extra,
    )


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
        "redaction": {k: manifest["redaction"][k] for k in ("rules", "allow_email_domains", "counts", "records_changed")},
    }
    if check:
        out["check"] = exp.check(out_dir).to_dict()
        out["ok"] = out["check"]["ok"]
    return out
