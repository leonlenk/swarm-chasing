"""Conformance check: run a mapping on a sample (or the whole dataset) and validate the output.

Checks, each reported with counts and up to 5 masked examples:

| code                 | severity        | what                                                        |
|----------------------|-----------------|-------------------------------------------------------------|
| spec_invalid         | error           | JSON Schema / semantic errors in the mapping                |
| table_missing        | error           | a ``from`` matches no file or table                          |
| field_missing        | error           | a mapped field path never occurs in the sampled rows        |
| no_records           | error           | a records entry yields nothing                               |
| id_missing           | error >5% / warn| rows dropped because local_id is empty                       |
| id_duplicate         | error           | two records with the same evidence id                        |
| id_unparseable       | error           | an id (or reply_to) fails ``scope.evidence.parse``, or does  |
|                      |                 | not parse back to itself (stray whitespace)                  |
| record_invalid       | error           | ``scope.records.event_record`` rejects a record              |
| time_unparseable     | error >1% / warn| time present but not parseable with the given format        |
| time_out_of_range    | error >1% / warn| parsed time outside 1990–2100 (wrong epoch unit?)            |
| time_missing         | warn >5%        | no time value                                                |
| actor_unmatched      | error >20% / warn| actor value resolves to no agent                            |
| actor_missing        | warn >5%        | no actor value (and no fallback)                             |
| text_empty           | error >20% / warn| message-category record with empty text                     |
| recipient_unmatched  | warn            | recipient values that resolve to no agent                    |
| reply_dangling       | warn >10% (full), >50% (sample) | reply_to targets not among the mapped ids |
| no_agents            | error           | records have actors but no agents were loaded                |

Status is ``pass`` when there are no errors (warnings allowed); the CLI exits 0/1.
"""

from __future__ import annotations

import difflib
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swarm_mcp.scope import evidence
from swarm_mcp.setup.mapping import MappedAdapter, MappingError, has_path, mapped_paths, validate_spec
from swarm_mcp.setup.masking import show
from swarm_mcp.setup.profile import flatten
from swarm_mcp.setup.timeparse import plausible, to_datetime

DEFAULT_SAMPLE_ROWS = 2000
FIELD_SAMPLE_ROWS = 200
MAX_EXAMPLES = 5


@dataclass
class Problem:
    severity: str
    code: str
    message: str
    count: int = 0
    examples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "count": self.count,
            "examples": self.examples[:MAX_EXAMPLES],
        }


class _Collector:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.examples: dict[str, list[str]] = defaultdict(list)

    def hit(self, code: str, example: str | None = None) -> None:
        self.counts[code] += 1
        if example is not None and len(self.examples[code]) < MAX_EXAMPLES:
            self.examples[code].append(example)


def _ex(table: str, row: int, label: str, value: Any) -> str:
    return f"{table} row {row}: {label}={show(value)}"


def _sev(rate: float, error_above: float) -> str:
    return "error" if rate > error_above else "warning"


def run_check(
    spec: dict[str, Any],
    root: str | Path | None = None,
    *,
    full: bool = False,
    rows: int = DEFAULT_SAMPLE_ROWS,
) -> dict[str, Any]:
    """Check ``spec`` against the dataset at ``root``. Returns a JSON-friendly report."""
    t0 = time.perf_counter()
    problems: list[Problem] = []
    report: dict[str, Any] = {
        "source": spec.get("source") if isinstance(spec, dict) else None,
        "root": str(root if root is not None else (spec.get("root") if isinstance(spec, dict) else None)),
        "mode": "full" if full else f"sample ({rows} rows per table)",
    }

    def finish() -> dict[str, Any]:
        errors = [p for p in problems if p.severity == "error"]
        report["status"] = "fail" if errors else "pass"
        report["errors"] = len(errors)
        report["warnings"] = len(problems) - len(errors)
        report["problems"] = [p.as_dict() for p in sorted(problems, key=lambda p: (p.severity != "error", p.code))]
        report["seconds"] = round(time.perf_counter() - t0, 3)
        return report

    errs = validate_spec(spec)
    if errs:
        problems.append(
            Problem(
                "error", "spec_invalid", "the mapping does not match the mapping schema", len(errs), errs[:MAX_EXAMPLES]
            )
        )
        return finish()
    try:
        adapter = MappedAdapter(spec, root, validate=False)
    except MappingError as e:
        problems.append(Problem("error", "spec_invalid", str(e), 1))
        return finish()
    report["root"] = str(adapter.root)

    # ---------------------------------------------------------------- every mapped field exists
    for frm, roles in mapped_paths(spec).items():
        try:
            tables = adapter.tables(frm)
        except MappingError as e:
            problems.append(Problem("error", "table_missing", str(e), 1))
            continue
        rows_seen: list[dict[str, Any]] = []
        for t in tables[:5]:
            rows_seen.extend(t.rows(limit=FIELD_SAMPLE_ROWS))
        known = sorted({p for r in rows_seen for p in flatten(r)})
        for role, path in dict.fromkeys(roles):
            if rows_seen and not any(has_path(r, path) for r in rows_seen):
                close = difflib.get_close_matches(path, known, n=3, cutoff=0.5)
                hint = f"; did you mean {', '.join(close)}?" if close else ""
                problems.append(
                    Problem(
                        "error",
                        "field_missing",
                        f"{frm}: {role} field {path!r} is never present in {len(rows_seen)} sampled rows{hint}",
                        1,
                    )
                )
    if any(p.code == "table_missing" for p in problems):
        return finish()

    # ---------------------------------------------------------------- records
    c = _Collector()
    limit = None if full else rows
    seen_ids: set[str] = set()
    replies: list[tuple[str, str, int]] = []
    by_kind: Counter[str] = Counter()
    message_kinds = {k for k, v in adapter.kinds.items() if v.category == "message" and v.table == "events"}
    n = n_actor = n_time_spec = 0
    actor_status: Counter[str] = Counter()
    try:
        for rec, d in adapter.iter_records(limit):
            if rec is None:
                c.hit("id_missing", _ex(d.table, d.row, "local_id", d.local_id_raw))
                continue
            n += 1
            kind = rec.kind
            by_kind[kind] += 1
            try:  # an id must parse back to itself, or citations of it never resolve
                if str(evidence.parse(rec.event_id)) != rec.event_id:
                    c.hit("id_unparseable", show(rec.event_id))
            except Exception:  # noqa: BLE001
                c.hit("id_unparseable", show(rec.event_id))
            if rec.event_id in seen_ids:
                c.hit("id_duplicate", show(rec.event_id))
            seen_ids.add(rec.event_id)
            try:
                rec.as_event_record()
            except Exception as e:  # noqa: BLE001
                c.hit("record_invalid", f"{d.table} row {d.row}: {show(str(e))}")
            spec_r = next(r for r in spec["records"] if r["kind"] == kind)
            if spec_r.get("time") is not None:
                n_time_spec += 1
                if d.time_error and d.time_error.startswith("out of range"):
                    c.hit("time_out_of_range", _ex(d.table, d.row, "time", d.time_raw))
                elif d.time_error:
                    c.hit("time_unparseable", _ex(d.table, d.row, "time", d.time_raw))
                elif rec.time is None:
                    c.hit("time_missing", _ex(d.table, d.row, "time", d.time_raw))
                else:
                    dt = to_datetime(rec.time)
                    if dt is not None and not plausible(dt):
                        c.hit("time_out_of_range", f"{d.table} row {d.row}: raw {show(d.time_raw)} -> {rec.time}")
            if d.actor_status != "none":
                n_actor += 1
                actor_status[d.actor_status] += 1
                if d.actor_status == "unmatched":
                    c.hit("actor_unmatched", _ex(d.table, d.row, "actor", d.actor_raw))
                elif d.actor_status == "missing":
                    c.hit("actor_missing", f"{d.table} row {d.row}")
            if kind in message_kinds and not (rec.text or "").strip():
                c.hit("text_empty", f"{d.table} row {d.row}: {rec.event_id}")
            for v in d.recipients_unmatched:
                c.hit("recipient_unmatched", _ex(d.table, d.row, "recipient", v))
            if rec.reply_to:
                try:
                    if str(evidence.parse(rec.reply_to)) != rec.reply_to:
                        c.hit("id_unparseable", f"{d.table} row {d.row}: reply_to {show(rec.reply_to)}")
                except Exception:  # noqa: BLE001
                    c.hit("id_unparseable", f"{d.table} row {d.row}: reply_to {show(rec.reply_to)}")
                replies.append((rec.reply_to, d.table, d.row))
    except MappingError as e:
        problems.append(Problem("error", "table_missing", str(e), 1))
        return finish()

    record_ids = set(seen_ids)
    agents = list(adapter.agents())
    periods = 0
    try:
        for p in adapter.periods(limit):
            periods += 1
            try:
                if str(evidence.parse(p.event_id)) != p.event_id:
                    c.hit("id_unparseable", show(p.event_id))
            except Exception:  # noqa: BLE001
                c.hit("id_unparseable", show(p.event_id))
            if p.event_id in seen_ids:
                c.hit("id_duplicate", show(p.event_id))
            seen_ids.add(p.event_id)
    except MappingError as e:
        problems.append(Problem("error", "table_missing", str(e), 1))

    def rate(code: str, denom: int) -> float:
        return c.counts[code] / denom if denom else 0.0

    for r in spec["records"]:
        if by_kind[r["kind"]] == 0:
            problems.append(
                Problem(
                    "error",
                    "no_records",
                    f"records entry kind {r['kind']!r} (from {r['from']!r}) produced no records",
                    1,
                )
            )
    skipped = c.counts["id_missing"]
    if skipped:
        r_ = skipped / (n + skipped)
        problems.append(
            Problem(
                _sev(r_, 0.05),
                "id_missing",
                f"{skipped} rows ({r_:.1%}) dropped: empty local_id",
                skipped,
                c.examples["id_missing"],
            )
        )
    for code, msg in (
        ("id_duplicate", "duplicate evidence ids"),
        ("id_unparseable", "evidence ids that do not parse, or not back to themselves (stray whitespace?)"),
        ("record_invalid", "records rejected by scope.records.event_record"),
    ):
        if c.counts[code]:
            problems.append(Problem("error", code, f"{c.counts[code]} {msg}", c.counts[code], c.examples[code]))
    for code, thr, msg in (
        ("time_unparseable", 0.01, "times present but unparseable"),
        ("time_out_of_range", 0.01, "times outside 1990-2100 (wrong epoch unit or format?)"),
    ):
        if c.counts[code]:
            r_ = rate(code, n_time_spec)
            problems.append(
                Problem(_sev(r_, thr), code, f"{c.counts[code]} {msg} ({r_:.1%})", c.counts[code], c.examples[code])
            )
    if c.counts["time_missing"] and rate("time_missing", n_time_spec) > 0.05:
        r_ = rate("time_missing", n_time_spec)
        problems.append(
            Problem(
                "warning",
                "time_missing",
                f"{c.counts['time_missing']} records ({r_:.1%}) have no time",
                c.counts["time_missing"],
                c.examples["time_missing"],
            )
        )
    no_time = [r["kind"] for r in spec["records"] if r.get("time") is None]
    if no_time:
        problems.append(
            Problem("warning", "time_missing", f"no time mapped for kinds {', '.join(no_time)}", len(no_time))
        )
    if c.counts["actor_unmatched"]:
        r_ = rate("actor_unmatched", n_actor)
        problems.append(
            Problem(
                _sev(r_, 0.2),
                "actor_unmatched",
                f"{c.counts['actor_unmatched']} actors ({r_:.1%}) resolve to no agent",
                c.counts["actor_unmatched"],
                c.examples["actor_unmatched"],
            )
        )
    if c.counts["actor_missing"] and rate("actor_missing", n_actor) > 0.05:
        r_ = rate("actor_missing", n_actor)
        problems.append(
            Problem(
                "warning",
                "actor_missing",
                f"{c.counts['actor_missing']} records ({r_:.1%}) have no actor",
                c.counts["actor_missing"],
                c.examples["actor_missing"],
            )
        )
    n_msg = sum(by_kind[k] for k in message_kinds)
    if c.counts["text_empty"]:
        r_ = rate("text_empty", n_msg)
        problems.append(
            Problem(
                _sev(r_, 0.2),
                "text_empty",
                f"{c.counts['text_empty']} message records ({r_:.1%}) have empty text",
                c.counts["text_empty"],
                c.examples["text_empty"],
            )
        )
    if c.counts["recipient_unmatched"]:
        problems.append(
            Problem(
                "warning",
                "recipient_unmatched",
                f"{c.counts['recipient_unmatched']} recipient values resolve to no agent (dropped)",
                c.counts["recipient_unmatched"],
                c.examples["recipient_unmatched"],
            )
        )
    if replies:
        dangling = [(rid, t, row) for rid, t, row in replies if rid not in record_ids]
        r_ = len(dangling) / len(replies)
        if dangling and r_ > (0.1 if full else 0.5):
            note = "" if full else " (sample mode: targets outside the sample count as dangling)"
            problems.append(
                Problem(
                    "warning",
                    "reply_dangling",
                    f"{len(dangling)} of {len(replies)} reply_to targets ({r_:.0%}) are not mapped ids{note}",
                    len(dangling),
                    [f"{t} row {row}: reply_to {show(rid)}" for rid, t, row in dangling[:MAX_EXAMPLES]],
                )
            )
    if n_actor and actor_status["agent"] + actor_status["unmatched"] and not agents:
        problems.append(
            Problem("error", "no_agents", "records have actors but no agents were loaded (check 'agents.from')", 1)
        )

    report["counts"] = {
        "records": n,
        "by_kind": dict(by_kind),
        "rows_dropped": skipped,
        "agents": len(agents),
        "periods": periods,
        "actors": dict(actor_status),
        "replies": len(replies),
    }
    report["rates"] = {
        "actor_unmatched": round(rate("actor_unmatched", n_actor), 4),
        "time_unparseable": round(rate("time_unparseable", n_time_spec), 4),
        "text_empty": round(rate("text_empty", n_msg), 4),
    }
    return finish()


def format_report(report: dict[str, Any]) -> str:
    """Readable text version of a check report."""
    lines = [
        f"check {report.get('source')} @ {report.get('root')}  [{report.get('mode')}]  -> "
        f"{report['status'].upper()} ({report['errors']} errors, {report['warnings']} warnings, {report['seconds']}s)"
    ]
    cnt = report.get("counts")
    if cnt:
        kinds = ", ".join(f"{k}={v:,}" for k, v in cnt["by_kind"].items())
        lines.append(
            f"  records {cnt['records']:,} ({kinds}); agents {cnt['agents']:,}; periods {cnt['periods']:,}; dropped {cnt['rows_dropped']:,}"
        )
        if cnt["actors"]:
            lines.append("  actors: " + ", ".join(f"{k} {v:,}" for k, v in sorted(cnt["actors"].items())))
    for p in report["problems"]:
        lines.append(f"  [{p['severity'].upper()}] {p['code']}: {p['message']}")
        for e in p["examples"]:
            lines.append(f"      e.g. {e}")
    return "\n".join(lines)
