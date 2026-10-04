"""Export a filtered, redacted subset of standard event records for sharing.

    swarm-mcp export --out DIR [--source S] [--kind K] [--type T] [--channel C] [--author A]
                     [--since T] [--until T] [--query Q] [--with-agents] [--keep-ips] [--no-check] [--json]

The command reads from the SwarmScope store (``scope.records.export_store``) and
runs ``check`` on the result unless ``--no-check``. This module is the library.

An export directory holds:

    events.jsonl    one standard record per line (``scope.records.event_record`` shape), with
                    every string field redacted except the identity fields
                    (event_id, source, kind, time, actor_type)
    agents.jsonl    agent records, if given (every string but ``id`` redacted)
    manifest.json   record counts by source/kind, redaction counts by placeholder type
                    (never values), the redaction policy, the filters description,
                    tool version, created_at, and sha256/bytes/lines per file

``check(out_dir)`` rescans everything with the strictest rules (``Redactor.strict``:
all rules, including private IPs) and verifies the hashes. It reports file, line,
JSON field and type for every hit, never the value, and fails on any hit, hash
mismatch, unlisted file or unparseable line. By default the email allowlist recorded
in the manifest is honoured (it was a deliberate, recorded policy); pass
``honour_allowlist=False`` to flag those too.

Record sources. ``export`` consumes any iterable of ``event_record`` dicts, streaming:
the store provider (``scope.records.store_records``) or a JSONL file (``read_jsonl`` +
``select`` for simple filters).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from swarm_mcp import __version__
from swarm_mcp.redact import Redactor, counts_dict

EXPORT_FORMAT = "swarmscope-export"
FORMAT_VERSION = 1
EVENTS_FILE = "events.jsonl"
AGENTS_FILE = "agents.jsonl"
MANIFEST_FILE = "manifest.json"

# Identity and time fields are copied verbatim: redacting them would break event-id
# references. check() still scans them, so a sensitive value in an id is reported.
KEEP_EVENT_FIELDS = frozenset({"event_id", "source", "kind", "time", "actor_type", "truncated"})
KEEP_AGENT_FIELDS = frozenset({"id", "agent_id"})


class ExportError(ValueError):
    """Bad input records or an unusable output directory."""


# --------------------------------------------------------------------------- ids


def _split_event_id(value: Any) -> tuple[str, str]:
    """(source, kind) of a ``<source>:<kind>:<native_id>`` evidence id (see ``scope.evidence``)."""
    from swarm_mcp.scope import evidence  # deferred: pulls in the store and the MCP toolkit

    try:
        ref = evidence.parse(value if isinstance(value, str) else "")
    except ValueError as e:  # EvidenceError is a ToolInputError, a ValueError
        raise ExportError(str(e)) from None
    return ref.source, ref.kind


# --------------------------------------------------------------------------- writing


class _HashingWriter:
    """Writes JSON lines while tracking sha256, bytes and lines."""

    def __init__(self, path: Path):
        self.path = path
        self._f = path.open("wb")
        self._h = hashlib.sha256()
        self.bytes = 0
        self.lines = 0

    def write(self, obj: Any) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self._f.write(data)
        self._h.update(data)
        self.bytes += len(data)
        self.lines += 1

    def close(self) -> dict[str, Any]:
        self._f.close()
        return {"sha256": self._h.hexdigest(), "bytes": self.bytes, "lines": self.lines}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def export(
    records: Iterable[dict[str, Any]],
    out_dir: str | os.PathLike[str],
    redactor: Redactor,
    agents: Iterable[dict[str, Any]] | None = None,
    filters_desc: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write ``events.jsonl`` (+ ``agents.jsonl``) and ``manifest.json``; return the manifest.

    ``records`` is any iterable of standard event records (dicts with at least a valid
    ``event_id``); it is consumed once, streaming. ``filters_desc`` (text or a JSON-able
    dict) is redacted before it is written. An existing ``agents.jsonl`` from an earlier
    export into the same directory is removed when ``agents`` is None.

    The files are written to a hidden staging directory inside ``out_dir`` and moved into
    place only once everything succeeded. A failed export (a bad record, or a lazy store
    provider raising on an unknown source or a missing store) leaves an existing export
    untouched and removes the directories it created.

    ``out_dir`` must be new, empty, or a previous export (only export files, with its manifest): anything else
    is refused before writing, since the check would fail on every file the manifest does not list.
    """
    out = Path(out_dir)
    if out.exists() and not out.is_dir():
        raise ExportError(f"--out {out} exists and is not a directory")
    _usable_out_dir(out)
    created = _missing_dirs(out)
    try:
        out.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".export-", suffix=".partial", dir=out))
        try:
            manifest = _write(records, stage, redactor, agents, filters_desc)
            # an old manifest must never describe new files: drop it first, move the new one in last
            (out / MANIFEST_FILE).unlink(missing_ok=True)
            for name in (EVENTS_FILE, AGENTS_FILE):
                if (stage / name).exists():
                    os.replace(stage / name, out / name)
                else:
                    (out / name).unlink(missing_ok=True)
            os.replace(stage / MANIFEST_FILE, out / MANIFEST_FILE)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    except BaseException:
        for d in created:  # deepest first; rmdir only removes them while they are empty
            try:
                d.rmdir()
            except FileNotFoundError:
                continue
            except OSError:
                break
        raise
    return manifest


def _usable_out_dir(out: Path) -> None:
    """Raise unless ``out`` does not exist, is empty, or holds only a previous export (its manifest included)."""
    if not out.is_dir():
        return
    names = sorted(p.name for p in out.iterdir())
    if not names:
        return
    other = [n for n in names if n not in (EVENTS_FILE, AGENTS_FILE, MANIFEST_FILE)]
    try:
        manifest = json.loads((out / MANIFEST_FILE).read_text(encoding="utf-8"))
        is_export = isinstance(manifest, dict) and manifest.get("format") == EXPORT_FORMAT
    except (OSError, ValueError, UnicodeDecodeError):
        is_export = False
    if other or not is_export:
        shown = ", ".join(other[:5] or names[:5]) + (" ..." if len(other or names) > 5 else "")
        raise ExportError(
            f"--out {out} is not empty and is not a previous export (it holds {shown}); "
            "pass a new or empty folder (nothing was written)"
        )


def _missing_dirs(path: Path) -> list[Path]:
    """``path`` and its ancestors that do not exist yet, deepest first."""
    missing = []
    while not path.exists() and path != path.parent:
        missing.append(path)
        path = path.parent
    return missing


def _write(
    records: Iterable[dict[str, Any]],
    out: Path,
    redactor: Redactor,
    agents: Iterable[dict[str, Any]] | None,
    filters_desc: str | dict[str, Any] | None,
) -> dict[str, Any]:
    """Write all export files into the (empty) directory ``out``; return the manifest."""
    redactions: Counter[str] = Counter()
    by_field: Counter[str] = Counter()
    by_source: dict[str, Counter[str]] = {}
    changed = 0
    first = last = None

    writer = _HashingWriter(out / EVENTS_FILE)
    try:
        for n, rec in enumerate(records, 1):
            if not isinstance(rec, dict):
                raise ExportError(f"record {n}: expected a JSON object, got {type(rec).__name__}")
            try:
                source, kind = _split_event_id(rec.get("event_id"))
            except ExportError as e:
                raise ExportError(f"record {n}: {e}") from None
            by_source.setdefault(source, Counter())[kind] += 1
            clean: dict[str, Any] = {}
            rec_counts: Counter[str] = Counter()
            for key, value in rec.items():
                if key in KEEP_EVENT_FIELDS:
                    clean[key] = value
                    continue
                out_key, kc = redactor.redact_key(key, clean)
                clean[out_key], c = redactor.redact_value(value, key)
                c = c + kc
                if c:
                    rec_counts.update(c)
                    by_field[out_key] += sum(c.values())
            if rec_counts:
                changed += 1
                redactions.update(rec_counts)
            t = rec.get("time")
            if isinstance(t, str) and t:
                first = t if first is None or t < first else first
                last = t if last is None or t > last else last
            writer.write(clean)
    finally:
        events_info = writer.close()

    files = {EVENTS_FILE: events_info}
    agent_count = None
    if agents is not None:
        aw = _HashingWriter(out / AGENTS_FILE)
        agent_counts: Counter[str] = Counter()
        try:
            for n, agent in enumerate(agents, 1):
                if not isinstance(agent, dict):
                    raise ExportError(f"agent {n}: expected a JSON object, got {type(agent).__name__}")
                clean_agent, c = redactor.redact_obj(agent, skip_keys=KEEP_AGENT_FIELDS)
                agent_counts.update(c)
                aw.write(clean_agent)
        finally:
            files[AGENTS_FILE] = aw.close()
        agent_count = files[AGENTS_FILE]["lines"]
        redactions.update(agent_counts)
        if agent_counts:
            by_field["<agents>"] += sum(agent_counts.values())

    filters_clean = None
    if filters_desc is not None:
        filters_clean, c = redactor.redact_obj(filters_desc)
        redactions.update(c)

    total = events_info["lines"]
    manifest: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "format_version": FORMAT_VERSION,
        "tool": "swarm_mcp.export",
        "tool_version": __version__,
        "created_at": _now(),
        "filters": filters_clean,
        "records": {
            "events": total,
            "agents": agent_count,
            "by_source": {s: sum(k.values()) for s, k in sorted(by_source.items())},
            "by_source_kind": {s: dict(sorted(k.items())) for s, k in sorted(by_source.items())},
            "time_range": {"first": first, "last": last},
        },
        "redaction": {
            **redactor.config(),
            "counts": counts_dict(redactions),  # by placeholder type; values are never recorded
            "by_field": dict(sorted(by_field.items())),
            "records_changed": changed,
            "kept_fields": sorted(KEEP_EVENT_FIELDS),
        },
        "files": files,
    }
    (out / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------- check


@dataclass
class CheckReport:
    """Result of ``check``. ``findings`` and ``problems`` never contain matched values."""

    out_dir: str
    ok: bool = True
    rules: list[str] = field(default_factory=list)
    honoured_email_domains: list[str] = field(default_factory=list)
    files_scanned: list[str] = field(default_factory=list)
    lines_scanned: int = 0
    findings: list[dict[str, Any]] = field(default_factory=list)  # {file, line, field, type, count}
    problems: list[dict[str, Any]] = field(default_factory=list)  # {file, problem}
    counts: dict[str, int] = field(default_factory=dict)  # findings by type

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __bool__(self) -> bool:
        return self.ok


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(
    out_dir: str | os.PathLike[str],
    *,
    honour_allowlist: bool = True,
    redactor: Redactor | None = None,
) -> CheckReport:
    """Rescan an export with the strictest rules; ``report.ok`` is False on any hit.

    Checks, per file listed in the manifest: present, sha256 and line count match.
    Scans every file in the directory: ``*.jsonl`` line by line as JSON (every string
    value and its key, so ``"api_key": "..."`` is caught), anything else as text lines.
    Files not listed in the manifest are scanned too and reported as problems.
    """
    out = Path(out_dir)
    report = CheckReport(out_dir=str(out))
    found: Counter[str] = Counter()

    def problem(file: str, what: str) -> None:
        report.problems.append({"file": file, "problem": what})

    manifest: dict[str, Any] = {}
    mpath = out / MANIFEST_FILE
    if not out.is_dir():
        problem(str(out), "export directory does not exist")
    elif not mpath.is_file():
        problem(MANIFEST_FILE, "missing")
    else:
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            problem(MANIFEST_FILE, "not valid JSON")

    allow: list[str] = []
    if honour_allowlist:
        allow = list((manifest.get("redaction") or {}).get("allow_email_domains") or [])
    strict = redactor or Redactor.strict(allow_email_domains=allow)
    report.rules = strict.rule_names
    report.honoured_email_domains = list(strict.allow_email_domains)

    listed: dict[str, Any] = manifest.get("files") or {}
    for name, info in sorted(listed.items()):
        p = out / name
        if not p.is_file():
            problem(name, "listed in manifest but missing")
        elif not isinstance(info, dict) or info.get("sha256") != _sha256(p):
            problem(name, "sha256 does not match manifest (file changed after export)")

    if out.is_dir():
        for p in sorted(out.iterdir()):
            name = p.name
            if p.is_dir():
                problem(name, "unexpected directory in export")
                continue
            if name != MANIFEST_FILE and name not in listed:
                problem(name, "not listed in manifest")
            report.files_scanned.append(name)
            lines = _scan_file(p, strict, report, found, problem)
            info = listed.get(name)
            if isinstance(info, dict) and info.get("lines") is not None and info["lines"] != lines:
                problem(name, f"line count {lines} does not match manifest ({info['lines']})")

    report.counts = counts_dict(found)
    report.ok = not report.findings and not report.problems
    return report


def _scan_file(path: Path, strict: Redactor, report: CheckReport, found: Counter[str], problem: Any) -> int:
    name = path.name
    is_jsonl = name.endswith(".jsonl")
    n = 0
    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        for n, raw in enumerate(f, 1):
            report.lines_scanned += 1
            line = raw.rstrip("\r\n")
            hits: list[tuple[str, Counter[str]]] = []
            if is_jsonl:
                if not line.strip():
                    problem(name, f"line {n}: blank line")
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    problem(name, f"line {n}: not valid JSON")
                    obj = None
                if obj is not None:
                    hits.extend(strict.scan_obj(obj))
                    hits.extend(("<keys>", c) for c in [_scan_keys(obj, strict)] if c)
                else:
                    _, c = strict.redact(line)
                    if c:
                        hits.append(("<raw>", c))
            else:
                _, c = strict.redact(line)
                if c:
                    hits.append(("<text>", c))
            for fld, c in hits:
                for typ, cnt in sorted(c.items()):
                    report.findings.append({"file": name, "line": n, "field": fld, "type": typ, "count": cnt})
                    found[typ] += cnt
    return n


def _scan_keys(obj: Any, strict: Redactor) -> Counter[str]:
    """Dict keys are data too; scan them as text."""
    total: Counter[str] = Counter()
    stack = [obj]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            for k, val in v.items():
                total.update(strict.redact(str(k))[1])
                stack.append(val)
        elif isinstance(v, list):
            stack.extend(v)
    return total


# --------------------------------------------------------------------------- JSONL source + filters


def read_jsonl(path: str | os.PathLike[str]) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from a JSONL file (``-`` = stdin); blank lines are skipped."""
    f = sys.stdin if str(path) == "-" else open(path, encoding="utf-8")
    try:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError as e:
                raise ExportError(f"{path}:{n}: not valid JSON ({e.msg})") from None
    finally:
        if f is not sys.stdin:
            f.close()


def _parse_bound(value: str, *, end: bool) -> datetime:
    raw = value.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ExportError(f"could not parse time {value!r}; use ISO, e.g. 2026-01-05 or 2026-01-05T14:30Z") from None
    if end and len(raw) <= 10 and "T" not in raw:
        dt += timedelta(days=1)  # a bare date as upper bound includes that whole day
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_kinds(kinds: Iterable[str]) -> list[tuple[str | None, str]]:
    """The record kind filter, shared by ``select`` and ``scope.records`` (store records, exports, sweeps): each is a
    bare kind ("event", any source) or "source:kind" (that source only) -> ``(source or None, kind)``."""
    out = []
    for k in kinds:
        src, _, kind = str(k).rpartition(":")
        out.append((src or None, kind))
    return out


def kind_matches(source: str, kind: str, specs: Iterable[tuple[str | None, str]]) -> bool:
    """Does a record of ``source``/``kind`` pass the parsed kind filter ``specs`` (empty = everything)?"""
    specs = list(specs)
    return not specs or any(k == kind and (s is None or s == source) for s, k in specs)


def select(
    records: Iterable[dict[str, Any]],
    *,
    sources: Iterable[str] = (),
    kinds: Iterable[str] = (),
    actors: Iterable[str] = (),
    since: str | None = None,
    until: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Filter standard records. Time bounds are half-open ``[since, until)`` in UTC;
    records without a parseable ``time`` are dropped when a bound is given."""
    src, knd, act = set(sources), parse_kinds(kinds), set(actors)
    lo = _parse_bound(since, end=False) if since else None
    hi = _parse_bound(until, end=True) if until else None
    for rec in records:
        if not isinstance(rec, dict):
            yield rec  # export() reports it with its position
            continue
        source, kind = ([*str(rec.get("event_id") or "").split(":", 2), "", ""])[:2]
        if src and source not in src:
            continue
        if not kind_matches(source, kind, knd):
            continue
        if act and rec.get("actor") not in act:
            continue
        if lo or hi:
            try:
                t = _parse_bound(str(rec.get("time") or ""), end=False)
            except ExportError:
                continue
            if (lo and t < lo) or (hi and t >= hi):
                continue
        yield rec


def describe_filters(**f: Any) -> dict[str, Any]:
    """The non-empty filters as a dict, for ``filters_desc``."""
    return {k: (sorted(v) if isinstance(v, (list, tuple, set)) else v) for k, v in f.items() if v}
