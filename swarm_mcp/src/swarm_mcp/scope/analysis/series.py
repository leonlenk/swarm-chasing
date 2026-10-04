"""Metric over time: one daily series per group with a trailing rolling mean and a 95% band.

``metric_series(store, metric=..., by=...)`` returns JSON-able data only (no HTML):

  metric  "messages"      agent messages per day (a count)
          "mention_rate"  share of agent messages that name another agent (recipient_ids non-empty)
          "sweep"         share of swept records whose verdict equals ``verdict``, read from a
                          sweep file (``<sweep_id>.jsonl``: a meta line, one verdict line per record,
                          a summary line; see docs/SWEEPS.md) and joined to the store on the
                          record's evidence id (messages, or actions for event records)
  by      "agent" (the ``top`` agents by volume), "lab" (agents.meta.lab folded into Anthropic,
          OpenAI, Google, Other labs), "channel" (the ``top`` channels) or "all"

Days are Village days (``viz.pagekit.village_days``: dates in the village's time zone) when the
store has a day 1, otherwise UTC dates. Only days with any matching record in the filters
("active days") are points: the village skips most weekends, and counting those as zeros would
pull every average down. Each group's series runs from its first to its last active day.

Rolling mean and band, over the trailing ``window`` calendar days ending on the day:
  counts  mean of the group's daily counts on the active days in the window (0 when the group was
          silent on an active day); 95% band = mean +/- 1.96 * sqrt(max(s^2, mean) / k), where s^2
          is the sample variance of those k daily counts: a normal interval that is never narrower
          than the Poisson one; clipped at 0.
  rates   pooled proportion x / n over the window (x hits among n records); 95% Wilson interval.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from swarm_mcp.scope.analysis.recap import _as_dt, _days, _local_date_sql, _sql_ts
from swarm_mcp.scope.db import Store, label_for
from swarm_mcp.scope.viz.pagekit import day_start_utc
from swarm_mcp.toolkit import ToolInputError

METRICS = ("messages", "mention_rate", "sweep")
BY = ("agent", "lab", "channel", "all")
LABS = ("Anthropic", "OpenAI", "Google", "Other labs")
Z95 = 1.959963984540054
MAX_GROUPS = 12


def lab_group(lab: str | None) -> str:
    s = (lab or "").lower()
    if "anthropic" in s:
        return "Anthropic"
    if "openai" in s:
        return "OpenAI"
    if "google" in s:
        return "Google"
    return "Other labs"


def wilson(x: float, n: float, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for x successes in n trials (n > 0)."""
    p = x / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def count_band(values: list[float], z: float = Z95) -> tuple[float, float, float]:
    """(mean, lo, hi) for daily counts: mean +/- z * sqrt(max(sample var, mean) / k), lo >= 0."""
    k = len(values)
    m = sum(values) / k
    var = sum((v - m) ** 2 for v in values) / (k - 1) if k > 1 else 0.0
    se = math.sqrt(max(var, m) / k)
    return m, max(0.0, m - z * se), m + z * se


def rolling(
    days: list[date],
    num: dict[date, float],
    den: dict[date, float],
    *,
    kind: str,
    window: int,
    span: tuple[date, date],
) -> list[tuple[int, float | None, float | None, float | None, float | None, float, float]]:
    """Per active day in ``span``: (index, raw, mean, lo, hi, num, den).

    ``days`` are all active days (sorted); ``kind`` "count" uses num as the count, "rate" uses
    num/den. The window is the trailing ``window`` calendar days ending on the day."""
    out = []
    lo_i = 0
    for i, d in enumerate(days):
        if d < span[0] or d > span[1]:
            continue
        start = d - timedelta(days=window - 1)
        while days[lo_i] < start:
            lo_i += 1
        win = [x for x in days[lo_i : i + 1] if x >= span[0]]
        n_d, d_d = num.get(d, 0.0), den.get(d, 0.0)
        if kind == "count":
            m, lo, hi = count_band([num.get(x, 0.0) for x in win])
            out.append((i, n_d, m, lo, hi, n_d, d_d))
        else:
            x = sum(num.get(w, 0.0) for w in win)
            n = sum(den.get(w, 0.0) for w in win)
            raw = n_d / d_d if d_d else None
            if n:
                lo, hi = wilson(x, n)
                out.append((i, raw, x / n, lo, hi, n_d, d_d))
            else:
                out.append((i, raw, None, None, None, n_d, d_d))
    return out


def read_sweep(path: str | Path) -> dict[str, Any]:
    """Parse a sweep .jsonl: {meta, verdicts: {event_id: verdict}, summary, errors}. The last
    verdict line per event id wins; lines whose call failed (verdict null) count as errors."""
    p = Path(path).expanduser()
    if not p.exists():
        raise ToolInputError(f"sweep file not found: {p}")
    meta: dict[str, Any] = {}
    summary: dict[str, Any] | None = None
    verdicts: dict[str, str] = {}
    errors = bad = 0
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            t = row.get("type")
            if t == "meta":
                meta = row
            elif t == "summary":
                summary = row
            elif t == "verdict" and row.get("event_id"):
                v = row.get("verdict")
                if v is None:
                    errors += 1
                    verdicts.pop(row["event_id"], None)
                else:
                    verdicts[row["event_id"]] = str(v)
    return {"meta": meta, "verdicts": verdicts, "summary": summary, "errors": errors, "bad_lines": bad}


def _filters(
    source: str | None, channel: str | None, t0: datetime | None, t1: datetime | None, alias: str = ""
) -> tuple[str, list[Any]]:
    p = f"{alias}." if alias else ""
    where, params = [f"{p}ts IS NOT NULL"], []
    if source:
        where.append(f"{p}source = ?")
        params.append(source)
    if channel is not None:
        where.append(f"{p}channel = ?")
        params.append(channel)
    if t0 is not None:
        where.append(f"{p}ts >= CAST(? AS TIMESTAMP)")
        params.append(_sql_ts(t0))
    if t1 is not None:
        where.append(f"{p}ts < CAST(? AS TIMESTAMP)")
        params.append(_sql_ts(t1))
    return " AND ".join(where), params


def metric_series(
    store: Store,
    *,
    metric: str = "messages",
    by: str = "agent",
    top: int = 6,
    since: Any = None,
    until: Any = None,
    source: str | None = None,
    channel: str | None = None,
    window: int = 7,
    sweep_path: str | Path | None = None,
    verdict: str = "yes",
    day_one: Any = None,
) -> dict[str, Any]:
    """Daily values per group, a trailing ``window``-day rolling mean and a 95% band (see module doc)."""
    if metric not in METRICS:
        raise ToolInputError(f"metric must be one of {', '.join(METRICS)}, not {metric!r}")
    if by not in BY:
        raise ToolInputError(f"by must be one of {', '.join(BY)}, not {by!r}")
    if metric == "sweep" and not sweep_path:
        raise ToolInputError('metric "sweep" needs sweep_path (a <sweep_id>.jsonl file)')
    if sweep_path and metric != "sweep":
        raise ToolInputError('sweep_path is only used with metric="sweep"')
    window = int(window)
    if window < 1:
        raise ToolInputError("window must be >= 1 day")
    top = max(1, min(int(top), MAX_GROUPS))
    t0, t1 = _as_dt(since, field="since"), _as_dt(until, end=True, field="until")
    ch = store.resolve_channel(channel, source) if channel else None
    spec = _days(store, day_one, source)
    dsql = _local_date_sql("ts", spec)
    kind = "count" if metric == "messages" else "rate"
    agents_meta = {
        r["agent_id"]: (json.loads(r["meta"]) if isinstance(r["meta"], str) else (r["meta"] or {}))
        for r in store.all("SELECT agent_id, meta FROM agents")
    }
    names = store.display_names()
    notes: list[str] = []
    excluded: dict[str, int] = {}

    # ---- data: (group key, day, num, den) ----------------------------------------------------
    gcol = {"agent": "author_id", "lab": "author_id", "channel": "coalesce(channel, '(none)')", "all": "'all'"}[by]
    rows: list[dict[str, Any]] = []
    if metric in ("messages", "mention_rate"):
        where, params = _filters(source, ch, t0, t1)
        rows = store.all(
            f"""SELECT {gcol} AS g, {dsql} AS d, count(*) AS den,
                       count(*) FILTER (WHERE len(recipient_ids) > 0) AS hits
                FROM messages WHERE {where} AND author_id IN (SELECT agent_id FROM agents) GROUP BY 1, 2""",
            params,
        )
        for r in rows:
            r["num"] = r["den"] if metric == "messages" else r["hits"]
        excluded["non_agent_messages"] = int(  # humans, external and unknown actors (not in the agents table)
            store.scalar(
                f"SELECT count(*) FROM messages WHERE {where} AND author_id NOT IN (SELECT agent_id FROM agents)",
                params,
            )
            or 0
        )
        what = "agent messages per day" if metric == "messages" else "share of agent messages that name another agent"
    else:
        sw = read_sweep(sweep_path)  # type: ignore[arg-type]
        ids = list(sw["verdicts"])
        where_m, pm = _filters(source, ch, t0, t1)
        found = store.all(
            f"""SELECT evidence_id AS id, author_id AS actor, channel, {dsql} AS d FROM messages
                WHERE {where_m} AND evidence_id IN (SELECT unnest(?::VARCHAR[]))""",
            pm + [ids],
        )
        if ch is None:
            where_a, pa = _filters(source, None, t0, t1)
            found += store.all(
                f"""SELECT evidence_id AS id, agent_id AS actor, NULL AS channel, {dsql} AS d FROM actions
                    WHERE {where_a} AND evidence_id IN (SELECT unnest(?::VARCHAR[]))""",
                pa + [ids],
            )
        agg: dict[tuple[str, date], list[int]] = defaultdict(lambda: [0, 0])
        non_agents = 0
        for r in found:
            if r["actor"] not in agents_meta:  # humans, external and unknown actors are not agents
                non_agents += 1
                continue
            g = {"agent": r["actor"], "lab": r["actor"], "channel": r["channel"] or "(none)", "all": "all"}[by]
            a = agg[(g, r["d"])]
            a[1] += 1
            a[0] += int(sw["verdicts"][r["id"]] == verdict)
        rows = [{"g": g, "d": d, "num": v[0], "den": v[1]} for (g, d), v in agg.items()]
        excluded.update(
            sweep_records=len(ids),
            not_in_store_or_filters=len(ids) - len(found),
            non_agent_records=non_agents,
            failed_calls=int(sw["errors"]),
        )
        rubric = (sw["meta"] or {}).get("rubric")
        what = f"share of swept records judged “{verdict}”"
        notes.append(
            f"sweep {sw['meta'].get('sweep_id', Path(str(sweep_path)).stem)}: {len(found)} of {len(ids)} verdicts joined "
            "to the store by evidence id; failed calls (verdict null) are left out; 'unclear' counts in the denominator."
            + (f" Rubric: {rubric}" if rubric else "")
        )

    # ---- groups -------------------------------------------------------------------------------
    def lab_of(aid: str) -> str:
        return lab_group(agents_meta.get(aid, {}).get("lab"))

    if by == "lab":
        for r in rows:
            r["g"] = lab_of(r["g"])
    num: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    den: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        num[r["g"]][r["d"]] += float(r["num"])
        den[r["g"]][r["d"]] += float(r["den"])
    vol = Counter({g: sum(v.values()) for g, v in den.items()})
    if by == "lab":
        keys = [g for g in LABS if g in vol]
    else:
        keys = [g for g, _ in sorted(vol.items(), key=lambda kv: (-kv[1], str(kv[0])))][:top]
    others = [g for g in vol if g not in keys]
    active = sorted({d for g in den for d, v in den[g].items() if v})

    # ---- rolling mean and band ---------------------------------------------------------------
    groups = []
    for g in keys:
        gd = [d for d, v in den[g].items() if v]
        if not gd:
            continue
        pts = rolling(active, num[g], den[g], kind=kind, window=window, span=(min(gd), max(gd)))
        name = (
            label_for(g, names)
            if by == "agent"
            else (f"#{g}" if by == "channel" and g != "(none)" else ("All agents" if by == "all" else str(g)))
        )
        item: dict[str, Any] = {
            "key": g,
            "name": name,
            "total_num": int(sum(num[g].values())),
            "total_den": int(sum(den[g].values())),
            "points": [[i, _r(raw), _r(m), _r(lo), _r(hi), int(nd), int(dd)] for i, raw, m, lo, hi, nd, dd in pts],
        }
        if by == "agent":
            item["lab"] = agents_meta.get(g, {}).get("lab")
            item["lab_group"] = lab_of(g)
        groups.append(item)

    starts = []
    for d in active:
        if spec:
            n = (d - date.fromisoformat(spec["day_one"])).days + 1
            starts.append(day_start_utc(n, spec).strftime("%Y-%m-%dT%H:%M:%SZ"))
        else:
            starts.append(f"{d.isoformat()}T00:00:00Z")
    unit = "messages per day" if kind == "count" else "share"
    notes[:0] = [
        f"{what}; points are active days (days with any matching record), "
        + (f"Village days in {spec['tz']}" if spec else "UTC dates")
        + "; only agents (authors in the agents table) count: humans, external and unknown actors are left out.",
        f"line: trailing {window}-day mean over the active days in the window"
        + (" of the group's daily counts (0 on active days it was silent)" if kind == "count" else " (pooled x / n)"),
        "band: 95% normal interval mean +/- 1.96 sqrt(max(s^2, mean)/k) on the k daily counts (never narrower "
        "than Poisson), clipped at 0"
        if kind == "count"
        else "band: 95% Wilson interval on the pooled window counts",
        "points: [day index, day value, rolling mean, band low, band high, numerator, denominator]",
    ]
    if others:
        notes.append(f"{len(others)} more {by}s not shown ({int(sum(vol[g] for g in others)):,} records).")
    return {
        "metric": metric,
        "by": by,
        "kind": kind,
        "unit": unit,
        "label": what,
        "window": window,
        "verdict": verdict if metric == "sweep" else None,
        "days": spec,
        "dates": [d.isoformat() for d in active],
        "starts": starts,
        "day_numbers": [(d - date.fromisoformat(spec["day_one"])).days + 1 for d in active] if spec else None,
        "groups": groups,
        "other_groups": {"groups": len(others), "records": int(sum(vol[g] for g in others))},
        "excluded": excluded,
        "notes": notes,
    }


def _r(v: float | None) -> float | None:
    return None if v is None else round(float(v), 4)
