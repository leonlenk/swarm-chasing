"""Findings: investigator claims that cite evidence ids, plus spot-check sampling.

Storage. ``<findings dir>/findings.jsonl`` is the source of truth: one JSON
object per line (the ``schema.Finding`` model), appended under an ``fcntl``
lock. Each recorded finding is also mirrored into the DuckDB ``findings`` table
(``INSERT OR REPLACE``) so SQL can join claims to records; when the store is
locked by another process the mirror is skipped (``db_synced: False``) and the
JSONL still holds the finding. If a finding_id appears on several lines, the
last line wins when listing.

Strictness. ``record_finding`` refuses a finding unless *every* evidence id
resolves in the store, and ``check_findings`` (the CLI and the Stop hook)
reports any current finding (last line per id, not rejected or retracted)
whose ids do not resolve. Both use short-lived read-only
connections so they work while the MCP server or other readers are running.

This module returns raw record text (``evidence_view(...)["text"]``); callers
that hand results to an LLM must wrap it with ``toolkit.untrusted``.
"""

from __future__ import annotations

import contextlib
import json
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

import duckdb

from swarm_mcp.scope import db, evidence
from swarm_mcp.scope.evidence import EvidenceError
from swarm_mcp.scope.schema import Finding
from swarm_mcp.toolkit import ToolInputError, parse_time

try:  # POSIX only; on other platforms appends are unlocked
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

FINDINGS_FILE = "findings.jsonl"
CONFIDENCES = ("low", "medium", "high")
STATUSES = ("open", "confirmed", "rejected", "retracted")
SpotKind = Literal["findings", "messages", "actions"]

MIRROR_TIMEOUT = 3.0  # seconds to wait for a read-write handle before giving up on the DuckDB mirror
_COPY_HINT = (
    "Evidence ids must be copied exactly from tool results (the evidence_id / agent_id fields), "
    "e.g. 'village:chat:<uuid>'. Nothing was written."
)


def findings_file(findings_dir: Path | str) -> Path:
    return Path(findings_dir) / FINDINGS_FILE


# --------------------------------------------------------------------------- reading


def read_findings(path: Path | str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse a findings.jsonl file. Returns ``(entries, parse_errors)``.

    ``entries`` are ``{"line": n, "finding": {...}}`` for every line holding a
    JSON object (1-based physical line numbers, blank lines skipped);
    ``parse_errors`` are ``{"line": n, "error": "..."}`` for lines that are not
    a JSON object. A missing file yields two empty lists.
    """
    path = Path(path)
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not path.exists():
        return entries, errors
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append({"line": n, "error": f"not valid JSON ({e.msg} at column {e.colno})"})
                continue
            if not isinstance(obj, dict):
                errors.append({"line": n, "error": f"expected a JSON object, got {type(obj).__name__}"})
                continue
            entries.append({"line": n, "finding": obj})
    return entries, errors


def _latest(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per finding_id (the last line wins), in order of first appearance."""
    by_id: dict[str, dict[str, Any]] = {}
    for e in entries:
        # a dict keeps the first-insertion position while the value becomes the latest line
        by_id[str(e["finding"].get("finding_id") or f"line:{e['line']}")] = e
    return list(by_id.values())


def list_findings(findings_dir: Path | str, *, status: str | None = None, limit: int | None = None) -> dict[str, Any]:
    """Findings from ``<findings_dir>/findings.jsonl``, newest first.

    Returns ``{"findings": [...], "total_matching", "returned", "has_more",
    "parse_errors": [{line, error}]}``. Corrupt lines are skipped and reported,
    never fatal. Each finding carries its ``line`` number in the file.
    """
    if status is not None and status not in STATUSES:
        raise ToolInputError(f"status must be one of {', '.join(STATUSES)} (got {status!r})")
    entries, errors = read_findings(findings_file(findings_dir))
    valid = []
    for e in entries:
        if e["finding"].get("finding_id") and "claim" in e["finding"]:
            valid.append(e)
        else:
            errors.append({"line": e["line"], "error": "JSON object is not a finding (no finding_id or claim)"})
    errors.sort(key=lambda x: x["line"])
    items = [{**e["finding"], "line": e["line"]} for e in _latest(valid)]
    if status is not None:
        items = [f for f in items if f.get("status", "open") == status]
    items.reverse()  # newest (last appended) first
    total = len(items)
    if limit is not None:
        items = items[: max(0, int(limit))]
    return {
        "findings": items,
        "total_matching": total,
        "returned": len(items),
        "has_more": len(items) < total,
        "parse_errors": errors,
    }


# --------------------------------------------------------------------------- evidence helpers


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def record_text(table: str, record: dict[str, Any]) -> str:
    """The human-readable text of a resolved record (raw dataset text: wrap before returning)."""
    if table in ("messages", "actions"):
        return record.get("content") or ""
    if table == "periods":
        return record.get("label") or ""
    if table == "agents":
        return record.get("display_name") or ""
    return ""


def evidence_view(resolved: dict[str, Any]) -> dict[str, Any]:
    """Compact, JSON-friendly view of ``evidence.resolve`` output.

    Ids, timestamps and kinds are structural; ``text`` is raw dataset text.
    """
    table, rec = resolved["table"], resolved["record"]
    out: dict[str, Any] = {"evidence_id": resolved["evidence_id"], "table": table}
    if table == "messages":
        out.update(ts=_iso(rec.get("ts")), author_id=rec.get("author_id"), channel=rec.get("channel"))
    elif table == "actions":
        out.update(ts=_iso(rec.get("ts")), agent_id=rec.get("agent_id"), kind=rec.get("kind"))
    elif table == "periods":
        out.update(start_ts=_iso(rec.get("start_ts")), end_ts=_iso(rec.get("end_ts")), kind=rec.get("kind"))
    out["text"] = record_text(table, rec)
    return out


def _dedupe(ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _bad_ids_message(bad: dict[str, str], total: int) -> str:
    lines = [f"Finding not recorded: {len(bad)} of {total} evidence ids do not resolve:"]
    lines += [f"  - {eid!r}: {reason}" for eid, reason in bad.items()]
    lines.append(_COPY_HINT)
    return "\n".join(lines)


def validate_evidence(db_path: Path | str, evidence_ids: Iterable[str] | str) -> list[dict[str, Any]]:
    """Resolve every id (deduplicated, order kept) with one read-only connection.

    Returns the ``evidence.resolve`` results. Raises ``ToolInputError`` for an
    empty list and ``EvidenceError`` listing every bad id if ANY fails.
    Raises ``db.StoreMissing`` if the store does not exist.
    """
    if isinstance(evidence_ids, str):
        evidence_ids = [evidence_ids]
    ids = _dedupe(str(i).strip() for i in (evidence_ids or []) if str(i).strip())
    if not ids:
        raise ToolInputError(
            "A finding needs at least one evidence id. " + _COPY_HINT.replace(" Nothing was written.", "")
        )
    resolved: dict[str, dict[str, Any]] = {}
    bad: dict[str, str] = {}
    with db.connect(Path(db_path), read_only=True) as store:
        for eid in ids:
            try:
                r = evidence.resolve(store, eid)
            except EvidenceError as e:
                bad[eid] = str(e)
                continue
            resolved.setdefault(r["evidence_id"], r)
    if bad:
        raise EvidenceError(_bad_ids_message(bad, len(ids)))
    return list(resolved.values())


# --------------------------------------------------------------------------- writing


@contextlib.contextmanager
def _locked_append(path: Path) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            # never glue a new line onto a hand-edited file that lacks a trailing newline
            f.seek(0, 2)
            if f.tell() > 0:
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    f.write(b"\n")
            yield f
            f.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _mirror(db_path: Path, finding: Finding, timeout: float = MIRROR_TIMEOUT) -> tuple[bool, str | None]:
    """INSERT OR REPLACE the finding into the DuckDB table. Never raises."""
    created = finding.created_at.astimezone(timezone.utc).replace(tzinfo=None)
    row = [
        finding.finding_id,
        created,
        finding.claim,
        list(finding.evidence_ids),
        finding.confidence,
        finding.author,
        finding.status,
    ]
    deadline = time.monotonic() + timeout
    delay = 0.05
    last: Exception | None = None
    while True:
        try:
            con = db.open_connection(db_path, read_only=False, timeout=max(0.0, deadline - time.monotonic()))
            try:
                con.execute(
                    "INSERT OR REPLACE INTO findings "
                    "(finding_id, created_at, claim, evidence_ids, confidence, author, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
            finally:
                con.close()
            return True, None
        except (duckdb.Error, OSError) as e:
            last = e
            # same-process readers hold the file with a different config: wait for them briefly too
            retryable = "different configuration" in str(e) or "lock" in str(e).lower()
            if not retryable or time.monotonic() >= deadline:
                break
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    return False, (
        f"DuckDB findings table not updated ({type(last).__name__}: {str(last).splitlines()[0] if last else '?'}); "
        "the store is probably open read-write elsewhere. findings.jsonl is the source of truth; "
        "the row will be missing from SQL queries until the finding is recorded again."
    )


def record_finding(
    findings_dir: Path | str,
    db_path: Path | str,
    *,
    claim: str,
    evidence_ids: list[str],
    confidence: str = "medium",
    author: str = "claude",
    status: str = "open",
) -> dict[str, Any]:
    """Validate and append one finding; mirror it into DuckDB.

    Returns ``{"finding", "evidence": [evidence_view...], "findings_file",
    "db_synced", "notes"}``. Raises ``ToolInputError``/``EvidenceError`` (and
    writes nothing) for an empty claim, no ids, or any unresolvable id.
    """
    claim = (claim or "").strip()
    if not claim:
        raise ToolInputError("claim must not be empty: state the finding in one or two sentences.")
    if confidence not in CONFIDENCES:
        raise ToolInputError(f"confidence must be one of {', '.join(CONFIDENCES)} (got {confidence!r})")
    if status not in STATUSES:
        raise ToolInputError(f"status must be one of {', '.join(STATUSES)} (got {status!r})")
    author = (author or "").strip() or "unknown"

    resolved = validate_evidence(db_path, evidence_ids)
    finding = Finding(
        finding_id=f"f-{uuid.uuid4().hex[:12]}",
        created_at=datetime.now(timezone.utc),
        claim=claim,
        evidence_ids=[r["evidence_id"] for r in resolved],
        confidence=confidence,  # type: ignore[arg-type]
        author=author,
        status=status,  # type: ignore[arg-type]
    )
    data = finding.model_dump(mode="json")
    path = findings_file(findings_dir)
    with _locked_append(path) as f:
        f.write((json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"))

    synced, note = _mirror(Path(db_path), finding)
    return {
        "finding": data,
        "evidence": [evidence_view(r) for r in resolved],
        "findings_file": str(path),
        "db_synced": synced,
        "notes": [note] if note else [],
    }


# --------------------------------------------------------------------------- checking


INACTIVE_STATUSES = ("rejected", "retracted")  # withdrawn findings: their evidence is not checked


def check_findings(findings_file: Path, db_path: Path) -> dict[str, Any]:
    """Verify the current findings in ``findings_file`` against the store at ``db_path``.

    Current means the last line per finding_id (the same last-line-wins rule as listing),
    minus findings whose status is rejected or retracted (counted in ``skipped``). Returns
    ``{ok, checked, skipped, problems: [{finding_id, line, bad_evidence: {id: reason}, error?}],
    parse_errors: [{line, error}], store_missing, message}``. A missing or empty file is ok.
    Corrupt lines and findings without evidence ids are problems.
    """
    findings_file, db_path = Path(findings_file), Path(db_path)
    all_entries, parse_errors = read_findings(findings_file)
    latest = _latest(all_entries)
    entries = [e for e in latest if e["finding"].get("status", "open") not in INACTIVE_STATUSES]
    problems: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "ok": True,
        "checked": 0,
        "skipped": len(all_entries) - len(entries),
        "problems": problems,
        "parse_errors": parse_errors,
        "store_missing": False,
        "findings_file": str(findings_file),
        "db_path": str(db_path),
        "message": "",
    }
    if not entries and not parse_errors:
        if not findings_file.exists():
            result["message"] = f"No findings to check ({findings_file} does not exist)."
        elif all_entries:
            result["message"] = f"No current findings to check in {findings_file} (all superseded or withdrawn)."
        else:
            result["message"] = f"No findings to check ({findings_file} is empty)."
        return result

    # one candidate problem per finding line; kept only if something is wrong with it
    rows: list[tuple[dict[str, Any], list[str]]] = []
    for e in entries:
        f, line = e["finding"], e["line"]
        p: dict[str, Any] = {
            "finding_id": str(f.get("finding_id") or f"<no finding_id, line {line}>"),
            "line": line,
            "bad_evidence": {},
        }
        ids = f.get("evidence_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i.strip() for i in ids):
            p["error"] = "evidence_ids is missing, empty, or not a list of non-empty strings"
            ids = []
        elif not str(f.get("claim") or "").strip():
            p["error"] = "claim is empty"
        rows.append((p, ids))

    if any(ids for _p, ids in rows):
        if not db_path.exists():
            problems.extend(p for p, _ids in rows if "error" in p)
            result.update(
                ok=False,
                store_missing=True,
                message=db.missing_store_hint(db_path) + " Evidence ids could not be checked.",
            )
            return result
        cache: dict[str, str | None] = {}  # id -> reason (None = resolves)
        with db.connect(db_path, read_only=True) as store:
            for p, ids in rows:
                for eid in ids:
                    if eid not in cache:
                        try:
                            evidence.resolve(store, eid)
                            cache[eid] = None
                        except EvidenceError as err:
                            cache[eid] = str(err)
                    if cache[eid] is not None:
                        p["bad_evidence"][eid] = cache[eid]
    result["checked"] = len(rows)
    problems.extend(p for p, _ids in rows if "error" in p or p["bad_evidence"])

    result["ok"] = not problems and not parse_errors
    n_bad_ids = sum(len(p["bad_evidence"]) for p in problems)
    if result["ok"]:
        result["message"] = f"All {result['checked']} findings cite evidence ids that resolve."
    else:
        parts = []
        if problems:
            parts.append(f"{len(problems)} finding(s) with problems ({n_bad_ids} unresolvable evidence ids)")
        if parse_errors:
            parts.append(f"{len(parse_errors)} corrupt line(s)")
        result["message"] = f"{findings_file}: " + "; ".join(parts) + "."
    return result


# --------------------------------------------------------------------------- spot checks


def spotcheck_sample(
    store: db.Store,
    *,
    kind: SpotKind,
    n: int = 5,
    seed: int = 0,
    findings_dir: Path | str | None = None,
    source: str | None = None,
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """A deterministic random sample for human review (``status`` filters findings).

    Records (``messages``/``actions``) are ordered by ``md5(evidence_id || seed)``
    (stable across runs and DuckDB versions) after the filters. Findings use
    ``random.Random(seed).sample`` over the latest version of each finding;
    each is returned with its cited evidence resolved (``evidence_view``, or
    ``{"evidence_id", "error"}`` when an id no longer resolves).
    Returned ``text`` fields are raw dataset text.
    """
    n = max(1, int(n))
    since_p = parse_time(since, field="since")
    until_p = parse_time(until, end=True, field="until")

    if kind == "findings":
        if findings_dir is None:
            raise ToolInputError("findings_dir is required for kind='findings'")
        if channel:
            raise ToolInputError("channel does not apply to kind='findings'")
        entries, _errors = read_findings(findings_file(findings_dir))
        items = _latest(entries)
        if source:
            items = [
                e for e in items if any(str(i).startswith(f"{source}:") for i in e["finding"].get("evidence_ids") or [])
            ]
        if since_p or until_p:
            items = [e for e in items if _in_window(e["finding"].get("created_at"), since_p, until_p)]
        if status is not None:
            items = [e for e in items if e["finding"].get("status", "open") == status]
        picked = random.Random(seed).sample(items, min(n, len(items)))
        out = []
        for e in picked:
            ev = []
            for eid in e["finding"].get("evidence_ids") or []:
                try:
                    ev.append(evidence_view(evidence.resolve(store, str(eid))))
                except EvidenceError as err:
                    ev.append({"evidence_id": str(eid), "error": str(err)})
            out.append({"line": e["line"], "finding": e["finding"], "evidence": ev})
        return out

    if kind == "messages":
        cols = "evidence_id, source, channel, author_id, ts, msg_type, content"
        table = "messages"
    elif kind == "actions":
        if channel:
            raise ToolInputError("channel applies only to kind='messages' (actions have no channel)")
        cols = "evidence_id, source, agent_id, ts, kind, content"
        table = "actions"
    else:
        raise ToolInputError(f"kind must be one of findings, messages, actions (got {kind!r})")

    where: list[str] = []
    params: list[Any] = []
    if source:
        where.append("source = ?")
        params.append(source)
    if channel:
        where.append("channel = ?")
        params.append(store.resolve_channel(channel, source))
    w, p = db.date_filters("ts", since_p, until_p)
    where += w
    params += p
    sql = (
        f"SELECT {cols} FROM {table}"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY md5(evidence_id || ?), evidence_id LIMIT ?"
    )
    rows = store.all(sql, [*params, f":{int(seed)}", n])
    out = []
    for r in rows:
        r = {k: _iso(v) for k, v in r.items()}
        r["text"] = r.pop("content") or ""
        out.append(r)
    return out


def _in_window(created_at: Any, since: str | None, until: str | None) -> bool:
    try:
        ts = parse_time(str(created_at), field="created_at")
    except ToolInputError:
        return False
    if ts is None:
        return False
    return (since is None or ts >= since) and (until is None or ts < until)
