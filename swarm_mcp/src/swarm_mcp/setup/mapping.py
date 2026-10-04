"""Declarative dataset mappings and the ``MappedAdapter`` that executes them.

A mapping is a JSON document (schema: ``spec_schema.MAPPING_SCHEMA``, docs:
``ADDING_MODULES.md``, "Mapping a new dataset") that says, per file or table, which field is
the record id, time, actor, location, text, reply target and recipients. It is
pure data: paths are looked up in rows, filters are compared with fixed
operators, and nothing in a spec is ever evaluated or imported.

``MappedAdapter(spec, root)`` follows ``protocol_bridge.Adapter``: ``agents()``,
``records()`` and ``periods()`` stream ``AgentRecord`` / ``StandardRecord`` /
``PeriodRecord`` with ids from ``events.make_event_id``. Actors resolve to agent
keys (``<source>:agent:<id>``) by agent id, name or alias; values that resolve to
no agent become ``<unmatched_prefix><value>`` and are counted, so the check can
report them.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from swarm_mcp.events import make_event_id
from swarm_mcp.setup import readers
from swarm_mcp.setup.protocol_bridge import (
    AgentRecord,
    KindInfo,
    PeriodRecord,
    StandardRecord,
    agent_key,
)
from swarm_mcp.setup.spec_schema import MAPPING_SCHEMA, SLUG
from swarm_mcp.setup.timeparse import FORMATS, iso_z, to_datetime

LOOKUP_MAX_ROWS = 1_000_000
_SLUG = re.compile(SLUG)
_AT = re.compile(r"@([\w.\-]+)")


class MappingError(ValueError):
    """The mapping is malformed or does not fit the dataset. The message says what to fix."""


# --------------------------------------------------------------------------- spec loading + validation


def load_spec(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        spec = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MappingError(f"mapping file not found: {p}") from None
    except ValueError as e:
        raise MappingError(f"mapping {p} is not valid JSON: {e}") from None
    if not isinstance(spec, dict):
        raise MappingError(f"mapping {p} must be a JSON object")
    return spec


def schema_errors(spec: Any) -> list[str]:
    """JSON Schema violations (``path: message``), or structural fallbacks without ``jsonschema``."""
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema ships with mcp
        if not isinstance(spec, dict) or "source" not in spec or "records" not in spec:
            return ["mapping must be an object with 'source' and 'records'"]
        return []
    v = jsonschema.Draft202012Validator(MAPPING_SCHEMA)
    out = []
    for e in sorted(v.iter_errors(spec), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in e.absolute_path) or "(top)"
        msg = e.message if len(e.message) < 300 else e.message[:300] + "…"
        out.append(f"{loc}: {msg}")
    return out


def semantic_errors(spec: dict[str, Any]) -> list[str]:
    """Checks the schema can't express: unique kinds, known lookups and reply kinds, time formats."""
    out: list[str] = []
    kinds = [r.get("kind") for r in spec.get("records", []) if isinstance(r, dict)]
    pkinds = [p.get("kind") for p in spec.get("periods", []) or [] if isinstance(p, dict)]
    for k, n in Counter(kinds + pkinds).items():
        if n > 1:
            out.append(
                f"kind {k!r} is used by {n} records/periods entries; kinds must be unique (merge them with a glob 'from')"
            )
    if "agent" in kinds + pkinds:
        out.append("kind 'agent' is reserved for agents")
    lookups = spec.get("lookups") or {}
    for i, r in enumerate(spec.get("records", [])):
        if not isinstance(r, dict):
            continue
        for role in ("location", "actor_type", "type"):
            fs = r.get(role)
            if isinstance(fs, dict) and fs.get("lookup") and fs["lookup"] not in lookups:
                out.append(f"records/{i}/{role}: unknown lookup {fs['lookup']!r} (define it under 'lookups')")
        rt = r.get("reply_to")
        if isinstance(rt, dict) and rt.get("kind") and rt["kind"] not in kinds:
            out.append(
                f"records/{i}/reply_to: kind {rt['kind']!r} is not a records kind ({', '.join(map(str, kinds))})"
            )
        for tkey in ("time",):
            t = r.get(tkey)
            if isinstance(t, dict):
                if t.get("format", "auto") not in FORMATS:
                    out.append(f"records/{i}/time: unknown format {t.get('format')!r}")
                if t.get("format") == "strptime" and not t.get("pattern"):
                    out.append(f"records/{i}/time: format 'strptime' needs a 'pattern'")
    ag = spec.get("agents") or {}
    if ag.get("from") and not ag.get("id"):
        out.append("agents: 'id' is required when 'from' is set")
    return out


def validate_spec(spec: Any) -> list[str]:
    errs = schema_errors(spec)
    if not errs and isinstance(spec, dict):
        errs = semantic_errors(spec)
    return errs


# --------------------------------------------------------------------------- field access (data only)


def get_path(row: Any, path: str) -> Any:
    """Look a dotted path up in a row. Arrays along the way are mapped over and flattened."""
    if isinstance(row, dict) and path in row:
        return row[path]
    cur: Any = row
    parts = path.split(".")
    for i, raw in enumerate(parts):
        part = raw[:-2] if raw.endswith("[]") else raw
        if isinstance(cur, list):
            rest = ".".join([part, *parts[i + 1 :]])
            out = []
            for item in cur:
                v = get_path(item, rest)
                if isinstance(v, list):
                    out.extend(v)
                elif v is not None:
                    out.append(v)
            return out
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def has_path(row: Any, path: str) -> bool:
    if isinstance(row, dict) and path in row:
        return True
    cur: Any = row
    for raw in path.split("."):
        part = raw[:-2] if raw.endswith("[]") else raw
        if isinstance(cur, list):
            cur = next((x for x in cur if isinstance(x, dict)), None)
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _same(a: Any, b: Any) -> bool:
    return a == b or (a is not None and b is not None and str(a) == str(b))


def row_matches(row: Any, where: list[dict[str, Any]] | None) -> bool:
    for cond in where or []:
        v = get_path(row, cond["field"])
        op = cond.get("op", "==")
        target = cond.get("value")
        if op == "==" and not _same(v, target):
            return False
        if op == "!=" and _same(v, target):
            return False
        if op in ("in", "not_in"):
            options = target if isinstance(target, list) else [target]
            hit = any(_same(v, o) for o in options)
            if hit != (op == "in"):
                return False
        if op == "exists" and v in (None, "", []):
            return False
        if op == "missing" and v not in (None, "", []):
            return False
        if op == "contains" and (v is None or str(target) not in (v if isinstance(v, list) else str(v))):
            return False
    return True


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def _scalar(v: Any) -> str | None:
    if v is None or v == "" or isinstance(v, (dict, list)):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def _norm_field(fs: Any) -> dict[str, Any]:
    return {"field": fs} if isinstance(fs, str) else dict(fs or {})


def mapped_paths(spec: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """``from`` -> [(role, path)] for every field path the spec reads (for the 'fields exist' check)."""
    out: dict[str, list[tuple[str, str]]] = {}

    def add(frm: str, role: str, p: Any) -> None:
        if isinstance(p, str) and p and not p.startswith("@"):
            out.setdefault(frm, []).append((role, p))

    def meta(frm: str, m: Any) -> None:
        for p in m.values() if isinstance(m, dict) else m or []:
            add(frm, "meta", p)

    ag = spec.get("agents") or {}
    if ag.get("from"):
        for role in ("id", "display_name"):
            add(ag["from"], f"agents.{role}", ag.get(role))
        for p in ag.get("aliases") or []:
            add(ag["from"], "agents.aliases", p)
        meta(ag["from"], ag.get("meta"))
    for name, lk in (spec.get("lookups") or {}).items():
        add(lk["from"], f"lookup {name}.key", lk.get("key"))
        add(lk["from"], f"lookup {name}.value", lk.get("value"))
    for r in spec.get("records", []):
        frm = r["from"]
        lid = r.get("local_id")
        for p in lid if isinstance(lid, list) else [lid]:
            add(frm, "local_id", p)
        for role in ("time", "actor", "actor_type", "location", "type", "reply_to", "recipients"):
            fs = _norm_field(r.get(role)) if r.get(role) is not None else {}
            add(frm, role, fs.get("field"))
            add(frm, f"{role}.fallback_field", fs.get("fallback_field"))
        tx = _norm_field(r.get("text")) if r.get("text") is not None else {}
        add(frm, "text", tx.get("field"))
        for p in tx.get("fields") or []:
            add(frm, "text", p)
        for c in r.get("where") or []:
            add(frm, "where", c.get("field"))
        meta(frm, r.get("meta"))
    for pr in spec.get("periods") or []:
        frm = pr["from"]
        for role in ("label",):
            add(frm, role, pr.get(role))
        for role in ("start", "end"):
            add(frm, role, _norm_field(pr.get(role)).get("field") if pr.get(role) else None)
        lid = pr.get("local_id")
        for p in lid if isinstance(lid, list) else [lid]:
            add(frm, "local_id", p)
    return out


# --------------------------------------------------------------------------- agents index


class AgentIndex:
    """Agents by id and by lower-cased name/alias, for resolving actor and recipient values."""

    def __init__(self) -> None:
        self.by_id: dict[str, AgentRecord] = {}
        self.by_name: dict[str, str] = {}
        self._names_re: re.Pattern[str] | None = None

    def add(self, a: AgentRecord) -> None:
        self.by_id.setdefault(a.local_id, a)
        for n in [a.display_name, *a.aliases]:
            if n:
                self.by_name.setdefault(n.strip().lower(), a.local_id)
        self._names_re = None

    def resolve(self, value: Any, match: str = "any") -> str | None:
        """Agent local id for a raw value, or None."""
        v = _scalar(value)
        if v is None:
            return None
        if match in ("id", "any") and v in self.by_id:
            return v
        if match in ("name", "any"):
            return self.by_name.get(v.strip().lower())
        return None

    def mentioned(self, text: str, mode: str) -> list[str]:
        if not text:
            return []
        if mode == "at":
            out = []
            for m in _AT.findall(text):
                aid = self.resolve(m.rstrip("."), "any")
                if aid and aid not in out:
                    out.append(aid)
            return out
        if self._names_re is None:
            names = sorted((n for n in self.by_name if len(n) >= 3), key=len, reverse=True)
            if not names:
                return []
            self._names_re = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in names) + r")(?![\w])", re.I)
        out = []
        for m in self._names_re.findall(text):
            aid = self.by_name.get(m.lower())
            if aid and aid not in out:
                out.append(aid)
        return out

    def __len__(self) -> int:
        return len(self.by_id)


# --------------------------------------------------------------------------- the adapter


@dataclass
class Diag:
    """Per-row diagnostics for the check (not part of the record)."""

    table: str
    row: int
    skip: str | None = None
    actor_status: str = "missing"  # agent | fallback | unmatched | missing | none
    actor_raw: Any = None
    time_raw: Any = None
    time_error: str | None = None
    recipients_unmatched: list[str] = field(default_factory=list)
    reply_raw: Any = None
    local_id_raw: Any = None


class MappedAdapter:
    """Run a mapping over a dataset folder. Follows ``protocol_bridge.Adapter``."""

    name = "mapped"

    def __init__(self, spec: dict[str, Any], root: str | Path | None = None, *, validate: bool = True):
        if validate:
            errs = validate_spec(spec)
            if errs:
                raise MappingError("invalid mapping:\n  " + "\n  ".join(errs[:20]))
        self.spec = spec
        root = root if root is not None else spec.get("root")
        if root is None:
            raise MappingError("no dataset path: pass one, or set 'root' in the mapping")
        self.root = Path(root)
        self.source: str = spec["source"]
        self.description: str = spec.get("description") or f"dataset mapped declaratively from {self.root.name}"
        self.email_allowlist: tuple[str, ...] = tuple(spec.get("email_allowlist") or ())
        self.kinds: dict[str, KindInfo] = {"agent": KindInfo("an agent", table="agents")}
        for r in spec["records"]:
            self.kinds[r["kind"]] = KindInfo(
                r.get("description") or f"a {r['kind']} record from {r['from']}", category=r.get("category", "message")
            )
        for p in spec.get("periods") or []:
            self.kinds[p["kind"]] = KindInfo(p.get("description") or f"a {p['kind']} period", table="periods")
        self.stats: Counter[str] = Counter()
        self._index: AgentIndex | None = None
        self._lookups: dict[str, dict[str, Any]] | None = None
        self._tables_cache: dict[str, list[readers.Table]] = {}
        self._seen: dict[str, list[Any]] = {}

    # ------------------------------------------------------------------ tables
    def tables(self, pattern: str) -> list[readers.Table]:
        if pattern not in self._tables_cache:
            found = readers.find_tables(self.root, pattern)
            if not found:
                raise MappingError(
                    f"no table matches {pattern!r} under {self.root}. Run `swarm-mcp add <path> --dry-run` to profile the dataset and list its table keys."
                )
            self._tables_cache[pattern] = found
        return self._tables_cache[pattern]

    def _rows(self, pattern: str, limit: int | None = None) -> Iterator[tuple[readers.Table, int, dict[str, Any]]]:
        for t in self.tables(pattern):
            for i, row in enumerate(t.rows(limit=limit), 1):
                yield t, i, row

    # ------------------------------------------------------------------ agents
    def _derive(self) -> bool:
        ag = self.spec.get("agents") or {}
        return bool(ag.get("derive_from_actors")) or not ag.get("from")

    def _load_agents(self) -> AgentIndex:
        if self._index is not None:
            return self._index
        idx = AgentIndex()
        ag = self.spec.get("agents") or {}
        if ag.get("from"):
            for _, _, row in self._rows(ag["from"]):
                if not row_matches(row, ag.get("where")):
                    continue
                aid = _scalar(get_path(row, ag["id"]))
                if aid is None:
                    self.stats["agents_skipped_no_id"] += 1
                    continue
                name = _scalar(get_path(row, ag.get("display_name") or ag["id"])) or aid
                aliases: list[str] = []
                for p in ag.get("aliases") or []:
                    v = get_path(row, p)
                    for x in v if isinstance(v, list) else [v]:
                        s = _scalar(x)
                        if s and s != name and s not in aliases:
                            aliases.append(s)
                idx.add(AgentRecord(aid, name, aliases, meta=_meta(row, ag.get("meta"))))
        self._index = idx
        return idx

    def _load_lookups(self) -> dict[str, dict[str, Any]]:
        if self._lookups is None:
            self._lookups = {}
            for name, lk in (self.spec.get("lookups") or {}).items():
                table: dict[str, Any] = {}
                for _, _, row in self._rows(lk["from"], limit=LOOKUP_MAX_ROWS):
                    k = _scalar(get_path(row, lk["key"]))
                    if k is not None:
                        table.setdefault(k, get_path(row, lk["value"]))
                self._lookups[name] = table
        return self._lookups

    # ------------------------------------------------------------------ value roles
    def _value(self, row: dict[str, Any], fs: Any) -> str | None:
        if fs is None:
            return None
        fs = _norm_field(fs)
        if "value" in fs and "field" not in fs:
            return fs["value"]
        raw = get_path(row, fs["field"]) if fs.get("field") else None
        v = _scalar(raw) if not isinstance(raw, list) else ", ".join(filter(None, map(_scalar, raw))) or None
        if v is not None and fs.get("lookup"):
            hit = self._load_lookups()[fs["lookup"]].get(v)
            v = _scalar(hit) if hit is not None else v
        if v is not None and fs.get("values") is not None:
            v = fs["values"].get(v, v)
        if v is None:
            v = fs.get("default")
        return v

    def _local_id(self, t: readers.Table, i: int, row: dict[str, Any], spec: Any) -> str | None:
        if spec == "@row":
            return f"{t.key}:{i}"
        parts = []
        for p in spec if isinstance(spec, list) else [spec]:
            v = _scalar(get_path(row, p))
            if v is None:
                return None
            parts.append(v)
        return ":".join(parts)

    def _time(self, row: dict[str, Any], spec: Any, diag: Diag | None) -> str | None:
        if spec is None:
            return None
        ts = _norm_field(spec)
        raw = get_path(row, ts["field"])
        if diag is not None:
            diag.time_raw = raw
        try:
            dt = to_datetime(raw, ts.get("format", "auto"), ts.get("pattern"))
        except (ValueError, OverflowError, OSError) as e:
            if diag is not None:
                diag.time_error = str(e)[:120]
            self.stats["time_unparseable"] += 1
            return None
        return iso_z(dt)

    # ------------------------------------------------------------------ records
    def iter_records(self, limit: int | None = None) -> Iterator[tuple[StandardRecord | None, Diag]]:
        """Records with diagnostics; ``limit`` rows per table (``None`` = all). Skipped rows yield ``(None, diag)``."""
        idx = self._load_agents()
        derive = self._derive()
        src = self.source
        for r in self.spec["records"]:
            kind = r["kind"]
            actor_spec = _norm_field(r.get("actor")) if r.get("actor") is not None else None
            rec_spec = _norm_field(r.get("recipients")) if r.get("recipients") is not None else None
            text_spec = _norm_field(r.get("text")) if r.get("text") is not None else {}
            reply_spec = _norm_field(r.get("reply_to")) if r.get("reply_to") is not None else None
            for t, i, row in self._rows(r["from"], limit=limit):
                if not row_matches(row, r.get("where")):
                    continue
                diag = Diag(t.key, i)
                lid = self._local_id(t, i, row, r["local_id"])
                if lid is None:
                    diag.skip = "missing local_id"
                    diag.local_id_raw = get_path(row, r["local_id"]) if isinstance(r["local_id"], str) else None
                    self.stats[f"{kind}_skipped_no_id"] += 1
                    yield None, diag
                    continue
                eid = make_event_id(src, kind, lid)
                when = self._time(row, r.get("time"), diag)

                actor = actor_type_auto = None
                if actor_spec:
                    raw = get_path(row, actor_spec["field"])
                    diag.actor_raw = raw
                    v = _scalar(raw)
                    if v is not None:
                        aid = v if derive else idx.resolve(v, actor_spec.get("match", "any"))
                        if derive and aid not in idx.by_id:
                            idx.add(AgentRecord(aid, aid))
                        if aid is not None:
                            actor, actor_type_auto, diag.actor_status = agent_key(src, aid), "agent", "agent"
                        else:
                            actor = f"{actor_spec.get('unmatched_prefix', 'external:')}{v}"
                            diag.actor_status = "unmatched"
                            self.stats["actor_unmatched"] += 1
                    elif actor_spec.get("fallback_field"):
                        fv = _scalar(get_path(row, actor_spec["fallback_field"]))
                        if fv is not None:
                            actor = f"{actor_spec.get('fallback_prefix', 'human:')}{fv}"
                            actor_type_auto, diag.actor_status = "human", "fallback"
                else:
                    diag.actor_status = "none"

                if text_spec.get("fields"):
                    parts = [_as_text(get_path(row, p)) for p in text_spec["fields"]]
                    text = text_spec.get("sep", "\n\n").join(p for p in parts if p)
                else:
                    text = _as_text(get_path(row, text_spec["field"])) if text_spec.get("field") else ""

                recipients: list[str] = []
                if rec_spec:
                    raw = get_path(row, rec_spec["field"])
                    if isinstance(raw, str) and rec_spec.get("split"):
                        raw = [x.strip() for x in raw.split(rec_spec["split"])]
                    for x in raw if isinstance(raw, list) else [raw]:
                        s = _scalar(x)
                        if s is None:
                            continue
                        aid = idx.resolve(s, rec_spec.get("match", "any"))
                        if aid is None and derive:
                            aid = s
                        if aid is None:
                            diag.recipients_unmatched.append(s)
                            continue
                        key = agent_key(src, aid)
                        if key not in recipients:
                            recipients.append(key)
                if r.get("text_mentions"):
                    for aid in idx.mentioned(text, r["text_mentions"]):
                        key = agent_key(src, aid)
                        if key != actor and key not in recipients:
                            recipients.append(key)

                reply_to = None
                if reply_spec:
                    rv = _scalar(get_path(row, reply_spec["field"]))
                    diag.reply_raw = rv
                    if rv is not None:
                        reply_to = make_event_id(src, reply_spec.get("kind") or kind, rv)

                actor_type = (
                    self._value(row, r.get("actor_type")) if r.get("actor_type") is not None else actor_type_auto
                )
                rec = StandardRecord(
                    event_id=eid,
                    time=when,
                    actor=actor,
                    actor_type=actor_type,
                    location=self._value(row, r.get("location")),
                    text=text,
                    type=self._value(row, r.get("type")),
                    recipients=recipients,
                    reply_to=reply_to,
                    ts_quality=r.get("ts_quality") or ("exact" if when else "missing"),
                    meta=_meta(row, r.get("meta")),
                )
                if actor and actor_type_auto == "agent" and when:
                    a = idx.by_id.get(actor.split(":", 2)[2])
                    dt = to_datetime(when)
                    if a is not None and dt is not None:
                        seen = self._seen.setdefault(a.local_id, [dt, dt])
                        seen[0], seen[1] = min(seen[0], dt), max(seen[1], dt)
                        a.first_seen, a.last_seen = iso_z(seen[0]), iso_z(seen[1])
                yield rec, diag

    def records(self, limit: int | None = None) -> Iterator[StandardRecord]:
        for rec, _ in self.iter_records(limit):
            if rec is not None:
                yield rec

    def agents(self) -> Iterator[AgentRecord]:
        """Agents (call after ``records()`` for first/last seen and derived agents)."""
        yield from self._load_agents().by_id.values()

    def periods(self, limit: int | None = None) -> Iterator[PeriodRecord]:
        for p in self.spec.get("periods") or []:
            for t, i, row in self._rows(p["from"], limit=limit):
                if not row_matches(row, p.get("where")):
                    continue
                lid = self._local_id(t, i, row, p["local_id"])
                if lid is None:
                    self.stats[f"{p['kind']}_skipped_no_id"] += 1
                    continue
                yield PeriodRecord(
                    event_id=make_event_id(self.source, p["kind"], lid),
                    label=_as_text(get_path(row, p["label"])) if p.get("label") else "",
                    start_time=self._time(row, p.get("start"), None),
                    end_time=self._time(row, p.get("end"), None),
                    meta=_meta(row, p.get("meta")),
                )

    def load(self, limit: int | None = None) -> Iterator[StandardRecord | AgentRecord | PeriodRecord]:
        """Everything, records first (so agents carry first/last seen), then agents, then periods."""
        yield from self.records(limit)
        yield from self.agents()
        yield from self.periods(limit)

    @classmethod
    def from_file(cls, mapping: str | Path, root: str | Path | None = None) -> "MappedAdapter":
        return cls(load_spec(mapping), root)


def _meta(row: dict[str, Any], spec: Any) -> dict[str, Any]:
    if not spec:
        return {}
    pairs = spec.items() if isinstance(spec, dict) else ((p, p) for p in spec)
    out = {}
    for name, p in pairs:
        v = get_path(row, p)
        if v is not None and v != "":
            out[name] = v if isinstance(v, (str, int, float, bool, list, dict)) else str(v)
    return out


def is_slug(s: str) -> bool:
    return bool(_SLUG.match(s or ""))
