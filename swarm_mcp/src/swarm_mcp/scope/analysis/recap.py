"""What happened, when, and who: window recaps, period recaps, agent arcs and notable moments.

Pure functions of a ``Store`` (an open SwarmScope connection) that return JSON-able dicts and
lists; no HTML. Every definition is simple enough to state in one sentence, and each result
carries a ``notes`` field (or, for ``notable_moments``, a ``why`` per item) that states it.

- ``window_recap(store, since, until)``: who was active, who mentioned whom, which terms rose
  against the preceding window, and the busiest threads.
- ``period_recaps(store)``: the same recap for every period (AI Village: each village goal),
  each against the previous period, computed in a handful of batched SQL queries.
- ``agent_arc(store, agent)``: one agent's activity per bin, its mention partners per period
  (with a Jensen-Shannon change score), and the novel terms it coined or adopted.
- ``notable_moments(store)``: activity bursts (z-scores against a trailing baseline), partner
  shifts and first uses of terms that later spread, each with the numbers behind it and up to
  30 evidence ids to open.

Shared definitions:
  term       a lowercased token matching ``[a-z][a-z0-9_-]{2,}`` or two adjacent such tokens,
             minus a short stopword list; counted once per message, agent messages only.
  mention    author -> each agent in ``recipient_ids``, self and humans excluded (the
             mention edge of ``analysis/graph.py``).
  actor kind ``human`` for ``human:*`` authors, ``agent`` for ids in the agents table,
             ``external`` for anything else.
  day        the Village day (``viz.pagekit.village_days``) when the store has one.

Windows are half-open [since, until) in UTC; ``since``/``until`` accept ``toolkit.parse_time``
strings ("2026-01-05", "2026-01-05T12:00Z", ...) or datetimes (naive = UTC).
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_mcp.scope.analysis.timeline import ts_iso
from swarm_mcp.scope.db import Store, label_for
from swarm_mcp.scope.viz.pagekit import day_number, day_range_label, day_start_utc, village_days
from swarm_mcp.toolkit import TS_FORMAT, ToolInputError, parse_time

STOPWORDS = frozenset(
    """
    the and for you that this with are have will was but not all can just our your their they them
    what when where which who how about from into over then than there these those been being also its
    let get got one two out now new any more most some such only very much many each other here like
    make made need use used using via per yes okay thanks thank please would could should might must may
    did does doing done has had his her him she our ours were don isn aren didn doesn won wasn haven
    hasn couldn wouldn shouldn ill ive youre theyre thats theres whats lets its itself myself yourself
    https http www com org net html
    """.split()
)
assert all(w.isalpha() and w.islower() for w in STOPWORDS)  # inlined into SQL as a literal list
_STOP_SQL = "[" + ",".join(f"'{w}'" for w in sorted(STOPWORDS)) + "]::VARCHAR[]"
TOKEN_RE = "[a-z][a-z0-9_-]{2,}"
PRIOR_MASS = 500.0  # cap on a0, the informative Dirichlet prior mass in the rising-terms log-odds
PRIOR_SHARE = 0.10  # a0 = min(PRIOR_MASS, PRIOR_SHARE * messages in window + baseline)
MAX_BURST_IDS = 40
MAX_MOMENT_IDS = 30
NOVEL_SPAN_SHARE = 0.10  # a novel term is first seen after the first 10% of the store's span ...
NOVEL_MIN_DELAY = timedelta(days=1)  # ... and at least a day in
FAST_DAYS = 14  # first_use moments rank terms by adopters within this many days of the first use


# ----------------------------------------------------------------------------- small helpers


def _as_dt(value: Any, *, end: bool = False, field: str = "time") -> datetime | None:
    """str (parse_time formats) or datetime -> naive UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    parsed = parse_time(str(value), end=end, field=field)
    return datetime.strptime(parsed, TS_FORMAT) if parsed else None


def _sql_ts(d: datetime) -> str:
    return d.strftime(TS_FORMAT)


def _iso(d: Any) -> str | None:
    return ts_iso(d)


def _agent_ids(store: Store) -> set[str]:
    return {r["agent_id"] for r in store.all("SELECT agent_id FROM agents")}


def actor_kind(author_id: str | None, agent_ids: set[str]) -> str:
    if not author_id:
        return "external"
    if author_id.startswith("human:"):
        return "human"
    return "agent" if author_id in agent_ids else "external"


def _filters(table: str, source: str | None, channel: str | None, alias: str = "") -> tuple[str, list[Any]]:
    p = f"{alias}." if alias else ""
    where, params = [f"{p}ts IS NOT NULL"], []
    if source:
        where.append(f"{p}source = ?")
        params.append(source)
    if channel is not None and table == "messages":
        where.append(f"{p}channel = ?")
        params.append(channel)
    return " AND ".join(where), params


def _segs_cte(segs: list[tuple[datetime, datetime]]) -> tuple[str, list[Any]]:
    rows = ", ".join("(?, CAST(? AS TIMESTAMP), CAST(? AS TIMESTAMP))" for _ in segs)
    params: list[Any] = []
    for i, (s, e) in enumerate(segs):
        params += [i, _sql_ts(s), _sql_ts(e)]
    return f"segs AS (SELECT * FROM (VALUES {rows}) v(seg, s0, s1))", params


def _msgs_cte(where: str, lo: datetime, hi: datetime) -> tuple[str, list[Any]]:
    """Messages assigned to non-overlapping segments (an ASOF join on the segment start)."""
    sql = f"""m AS (
        SELECT x.evidence_id, x.author_id, x.channel, x.ts, x.recipient_ids, x.content, s.seg
        FROM (SELECT * FROM messages WHERE {where} AND ts >= CAST(? AS TIMESTAMP) AND ts < CAST(? AS TIMESTAMP)) x
        ASOF JOIN segs s ON x.ts >= s.s0
        WHERE x.ts < s.s1)"""
    return sql, [_sql_ts(lo), _sql_ts(hi)]


def _terms_cte(src: str, cols: str) -> str:
    """One row per (message, distinct term): unigrams + adjacent bigrams, stopwords removed."""
    return f"""tl AS (SELECT {cols}, regexp_extract_all(lower(content), '{TOKEN_RE}') AS L FROM {src}),
    tt AS (SELECT {cols}, unnest(list_distinct(list_concat(
              L, list_transform(range(1, len(L)), i -> L[i] || ' ' || L[i + 1])))) AS term FROM tl),
    stop AS (SELECT unnest({_STOP_SQL}) AS w),
    terms AS (SELECT tt.* FROM tt
              ANTI JOIN stop s1 ON split_part(tt.term, ' ', 1) = s1.w
              ANTI JOIN stop s2 ON split_part(tt.term, ' ', 2) = s2.w)"""


def _days(store: Store, day_one: Any = None) -> dict[str, str] | None:
    return village_days(store, day_one)


def _local_date_sql(col: str, spec: dict[str, str] | None) -> str:
    """SQL for the bin date of a naive-UTC timestamp column: the Village-day date in the
    spec's time zone, or the UTC date. The tz name is validated by ZoneInfo in day_spec."""
    if spec:
        tz = spec["tz"].replace("'", "")
        return f"CAST(timezone('{tz}', {col} AT TIME ZONE 'UTC') AS DATE)"
    return f"CAST({col} AS DATE)"


def _store_span(store: Store, source: str | None = None) -> tuple[datetime | None, datetime | None]:
    w, p = _filters("messages", source, None)
    r = store.one(f"SELECT min(ts) AS t0, max(ts) AS t1 FROM messages WHERE {w}", p) or {}
    return r.get("t0"), r.get("t1")


def _js_distance(p: dict[str, int], q: dict[str, int]) -> float | None:
    """Jensen-Shannon distance (base 2, in [0, 1]) between two count distributions."""
    sp, sq = sum(p.values()), sum(q.values())
    if not sp or not sq:
        return None
    keys = set(p) | set(q)
    js = 0.0
    for k in keys:
        a, b = p.get(k, 0) / sp, q.get(k, 0) / sq
        m = (a + b) / 2
        if a:
            js += 0.5 * a * math.log2(a / m)
        if b:
            js += 0.5 * b * math.log2(b / m)
    return math.sqrt(max(js, 0.0))


def _day_fields(t: Any, spec: dict[str, str] | None) -> dict[str, Any]:
    return {"day": day_number(t, spec)} if spec and t is not None else {}


# ----------------------------------------------------------------------------- recaps


def _recap_batch(
    store: Store,
    segs: list[tuple[datetime, datetime]],
    targets: list[int],
    *,
    source: str | None,
    channel: str | None,
    top_terms: int,
    top_bursts: int,
    max_agents: int,
    gap_minutes: float,
    min_term_msgs: int,
    min_term_agents: int,
) -> dict[int, dict[str, Any]]:
    """Recaps for segments ``targets`` (indexes into the non-overlapping, time-ordered ``segs``);
    each target's baseline for rising terms is segment target-1 (none for segment 0)."""
    if not segs or not targets:
        return {}
    names = store.display_names()
    agents = _agent_ids(store)
    lo, hi = min(s for s, _ in segs), max(e for _, e in segs)
    seg_sql, seg_p = _segs_cte(segs)
    where, wp = _filters("messages", source, channel)
    m_sql, m_p = _msgs_cte(where, lo, hi)
    head, hp = f"WITH {seg_sql}, {m_sql}", seg_p + wp + m_p
    tset = sorted(set(targets))
    tlist = ", ".join(str(int(t)) for t in tset)  # ints we built; inlined

    # activity (messages per author; actions per agent, not channel-scoped)
    msg_rows = store.all(f"{head} SELECT seg, author_id, count(*) AS n FROM m WHERE seg IN ({tlist}) GROUP BY 1, 2", hp)
    aw, ap = _filters("actions", source, None, "a")
    act_rows = store.all(
        f"""WITH {seg_sql}
            SELECT s.seg, a.agent_id, count(*) AS n
            FROM (SELECT * FROM actions a WHERE {aw} AND a.ts >= CAST(? AS TIMESTAMP) AND a.ts < CAST(? AS TIMESTAMP)) a
            ASOF JOIN segs s ON a.ts >= s.s0
            WHERE a.ts < s.s1 AND s.seg IN ({tlist}) GROUP BY 1, 2""",
        seg_p + ap + [_sql_ts(lo), _sql_ts(hi)],
    )
    totals = {
        r["seg"]: r
        for r in store.all(
            f"""{head} SELECT seg, count(*) AS messages,
                       count(*) FILTER (WHERE author_id NOT LIKE 'human:%') AS agent_messages
                FROM m GROUP BY 1""",
            hp,
        )
    }

    # mentions (graph.py's mention edge: author -> recipient, self and humans excluded)
    men_rows = store.all(
        f"""{head}, e AS (SELECT seg, author_id AS src, unnest(recipient_ids) AS dst FROM m
                          WHERE seg IN ({tlist}) AND len(recipient_ids) > 0)
            SELECT seg, src, dst, count(*) AS n FROM e
            WHERE src <> dst AND src NOT LIKE 'human:%' AND dst NOT LIKE 'human:%'
            GROUP BY 1, 2, 3""",
        hp,
    )

    # rising terms: log-odds with an informative Dirichlet prior (Monroe et al. 2008), per
    # segment against the previous one, over message counts (a message counts a term once)
    term_rows = store.all(
        f"""{head}, {_terms_cte("(SELECT * FROM m WHERE author_id NOT LIKE 'human:%')", "seg, evidence_id, author_id, ts")},
            c AS (SELECT seg, term, count(*) AS n, count(DISTINCT author_id) AS agents,
                         arg_min(evidence_id, (ts, evidence_id)) AS first_id
                  FROM terms GROUP BY 1, 2),
            tot AS (SELECT seg, count(*) AS N FROM m WHERE author_id NOT LIKE 'human:%' GROUP BY 1),
            cand AS (
              SELECT w.seg, w.term, w.n, coalesce(b.n, 0) AS nb, w.agents, w.first_id,
                     CAST(coalesce(ti.N, 0) AS DOUBLE) AS ni, CAST(coalesce(tj.N, 0) AS DOUBLE) AS nj
              FROM c w
              LEFT JOIN c b ON b.seg = w.seg - 1 AND b.term = w.term
              LEFT JOIN tot ti ON ti.seg = w.seg
              LEFT JOIN tot tj ON tj.seg = w.seg - 1
              WHERE w.seg IN ({tlist}) AND w.n >= ? AND w.agents >= ?),
            p0 AS (SELECT *, least({PRIOR_MASS}, greatest({PRIOR_SHARE} * (ni + nj), 1.0)) AS a0 FROM cand),
            pr AS (SELECT *, a0 * (n + nb) / (ni + nj) AS a FROM p0),
            z AS (SELECT *,
                    ln((n + a) / greatest(ni + a0 - n - a, 1e-9))
                  - ln((nb + a) / greatest(nj + a0 - nb - a, 1e-9)) AS delta,
                    sqrt(1.0 / (n + a) + 1.0 / (nb + a)) AS se
                  FROM pr)
            SELECT seg, term, n, nb, agents, first_id, ni, nj, delta, delta / se AS score
            FROM z WHERE delta > 0
            QUALIFY row_number() OVER (PARTITION BY seg ORDER BY delta / se DESC, term) <= ?""",
        hp + [int(min_term_msgs), int(min_term_agents), int(top_terms) * 3],
    )

    # bursts: runs of messages in one channel with gaps <= gap_minutes, within a segment
    burst_sql = f"""{head},
        b AS (SELECT seg, evidence_id, author_id, channel, ts,
                     CASE WHEN lag(ts) OVER w IS NULL
                            OR epoch_ms(ts) - epoch_ms(lag(ts) OVER w) > ? THEN 1 ELSE 0 END AS brk
              FROM m WHERE channel IS NOT NULL AND seg IN ({tlist})
              WINDOW w AS (PARTITION BY seg, channel ORDER BY ts, evidence_id)),
        r AS (SELECT *, sum(brk) OVER (PARTITION BY seg, channel ORDER BY ts, evidence_id
                                       ROWS UNBOUNDED PRECEDING) AS run FROM b),
        g AS (SELECT seg, channel, run, count(*) AS n, min(ts) AS t0, max(ts) AS t1,
                     arg_min(evidence_id, (ts, evidence_id)) AS first_id,
                     list(evidence_id ORDER BY ts, evidence_id)[1:{MAX_BURST_IDS}] AS ids
              FROM r GROUP BY 1, 2, 3
              QUALIFY row_number() OVER (PARTITION BY seg ORDER BY count(*) DESC, min(ts)) <= ?),
        ac AS (SELECT r.seg, r.channel, r.run, r.author_id, count(*) AS c
               FROM r SEMI JOIN g ON g.seg = r.seg AND g.channel = r.channel AND g.run = r.run
               GROUP BY 1, 2, 3, 4)
        SELECT g.*, (SELECT list(ac.author_id ORDER BY ac.c DESC, ac.author_id) FROM ac
                     WHERE ac.seg = g.seg AND ac.channel = g.channel AND ac.run = g.run) AS authors
        FROM g ORDER BY seg, n DESC, t0"""
    burst_rows = store.all(burst_sql, hp + [int(round(gap_minutes * 60_000)), int(top_bursts)])

    name_toks = _name_tokens(store)

    # ---- assemble per target segment
    out: dict[int, dict[str, Any]] = {}
    acts: dict[int, Counter[str]] = defaultdict(Counter)
    msgs: dict[int, Counter[str]] = defaultdict(Counter)
    for r in msg_rows:
        msgs[r["seg"]][r["author_id"]] += int(r["n"])
    for r in act_rows:
        acts[r["seg"]][r["agent_id"]] += int(r["n"])
    men: dict[int, Counter[tuple[str, str]]] = defaultdict(Counter)
    for r in men_rows:
        men[r["seg"]][(r["src"], r["dst"])] += int(r["n"])
    terms: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in term_rows:
        terms[r["seg"]].append(r)
    bursts: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in burst_rows:
        bursts[r["seg"]].append(r)

    for seg in tset:
        ids = set(msgs[seg]) | set(acts[seg])
        act = sorted(
            (
                {
                    "agent_id": a,
                    "name": label_for(a, names),
                    "kind": actor_kind(a, agents),
                    "messages": msgs[seg][a],
                    "actions": acts[seg][a],
                }
                for a in ids
            ),
            key=lambda d: (-(d["messages"] + d["actions"]), d["name"]),
        )
        shown = act[:max_agents]
        rest = act[max_agents:]
        # mention matrix over the shown non-human actors, in activity order
        order = [d["agent_id"] for d in shown if d["kind"] != "human"]
        idx = {a: i for i, a in enumerate(order)}
        rows, dropped = [], 0
        for (s, d), n in sorted(men[seg].items()):
            if s in idx and d in idx:
                rows.append([idx[s], idx[d], n])
            else:
                dropped += n
        # rising terms: drop a unigram when a listed bigram containing it accounts for >= 80% of it
        cand = [t for t in terms[seg] if not set(t["term"].split(" ")) <= name_toks]
        bigrams = [t for t in cand if " " in t["term"]]
        kept = []
        for t in cand:
            if " " not in t["term"] and any(
                t["term"] in b["term"].split(" ") and b["n"] >= 0.8 * t["n"] for b in bigrams
            ):
                continue
            kept.append(t)
        rising = []
        for t in kept[:top_terms]:
            n, nb, ni, nj = int(t["n"]), int(t["nb"]), int(t["ni"]), int(t["nj"])
            before = f"{nb} of {nj:,} before" if nj else "no baseline"
            rising.append(
                {
                    "term": t["term"],
                    "n": n,
                    "n_before": nb,
                    "agents": int(t["agents"]),
                    "score": round(float(t["score"]), 2),
                    "log_odds": round(float(t["delta"]), 3),
                    "first_id": t["first_id"],
                    "why": f"{n} of {ni:,} msgs vs {before}, {int(t['agents'])} agents",
                }
            )
        bl = []
        for b in bursts[seg]:
            bl.append(
                {
                    "channel": b["channel"],
                    "start": _iso(b["t0"]),
                    "end": _iso(b["t1"]),
                    "n": int(b["n"]),
                    "agents": [label_for(a, names) for a in (b["authors"] or [])],
                    "first_id": b["first_id"],
                    "ids": list(b["ids"] or []),
                }
            )
        tot = totals.get(seg, {})
        base = totals.get(seg - 1, {}) if seg > 0 else {}
        out[seg] = {
            "totals": {
                "messages": int(tot.get("messages") or 0),
                "agent_messages": int(tot.get("agent_messages") or 0),
                "actions": int(sum(acts[seg].values())),
                "actors_active": len(act),
                "baseline_agent_messages": int(base.get("agent_messages") or 0) if seg > 0 else None,
            },
            "activity": shown,
            "activity_others": {
                "actors": len(rest),
                "messages": sum(d["messages"] for d in rest),
                "actions": sum(d["actions"] for d in rest),
            },
            "mentions": {
                "agents": order,
                "names": [label_for(a, names) for a in order],
                "rows": rows,
                "total": int(sum(men[seg].values())),
                "outside_shown": dropped,
            },
            "rising_terms": rising,
            "bursts": bl,
        }
    return out


_RECAP_NOTES = [
    "activity: messages per author and actions per agent in [since, until); actions are not channel-scoped.",
    "mentions: author -> each agent in recipient_ids, self and humans excluded; rows are [i, j, n] "
    "over 'agents' (the shown non-human actors, in activity order).",
    "rising_terms: unigrams and adjacent bigrams of agent messages (lowercased tokens matching "
    f"{TOKEN_RE}, stopwords removed), counted once per message, ranked by the z-score of the "
    f"log-odds ratio against the baseline window with an informative Dirichlet prior "
    f"(Monroe et al. 2008; prior mass a0 = {PRIOR_SHARE:.0%} of the messages in both windows, at most "
    f"{PRIOR_MASS:g}, spread by the term's pooled rate); only terms "
    "more common than in the baseline, in >= min_term_msgs messages by >= min_term_agents agents, "
    "not made only of agent-name tokens; a unigram is dropped when a listed bigram holds >= 80% of its uses.",
    "bursts: runs of consecutive messages in one channel with gaps <= gap_minutes, ranked by size; "
    f"ids lists the first {MAX_BURST_IDS} in order.",
]


def window_recap(
    store: Store,
    since: Any,
    until: Any,
    *,
    source: str | None = None,
    channel: str | None = None,
    baseline: str | tuple[Any, Any] | None = "previous",
    top_terms: int = 12,
    top_bursts: int = 5,
    max_agents: int = 30,
    gap_minutes: float = 20,
    min_term_msgs: int = 4,
    min_term_agents: int = 2,
    day_one: Any = None,
) -> dict[str, Any]:
    """What happened in [since, until): activity, mentions, rising terms and the busiest threads.

    ``baseline``: "previous" (the same length immediately before), None/"none", or an explicit
    (since, until) that ends at or before ``since``. ``channel`` is an exact channel name.
    """
    t0, t1 = _as_dt(since, field="since"), _as_dt(until, end=True, field="until")
    if t0 is None or t1 is None:
        lo, hi = _store_span(store, source)
        if lo is None:
            raise ToolInputError("the store has no dated messages")
        t0 = t0 or lo
        t1 = t1 or (hi + timedelta(microseconds=1))
    if t1 <= t0:
        raise ToolInputError("until must be after since")
    if baseline == "previous":
        b0, b1 = t0 - (t1 - t0), t0
    elif baseline in (None, "none", False):
        b0 = b1 = None
    elif isinstance(baseline, (tuple, list)) and len(baseline) == 2:
        b0, b1 = _as_dt(baseline[0], field="baseline since"), _as_dt(baseline[1], end=True, field="baseline until")
        if b0 is None or b1 is None or b1 <= b0 or b1 > t0:
            raise ToolInputError("baseline must be a (since, until) window that ends at or before since")
    else:
        raise ToolInputError('baseline must be "previous", None or a (since, until) pair')
    segs = ([(b0, b1)] if b0 is not None else []) + [(t0, t1)]
    target = len(segs) - 1
    rec = _recap_batch(
        store,
        segs,
        [target],
        source=source,
        channel=channel,
        top_terms=top_terms,
        top_bursts=top_bursts,
        max_agents=max_agents,
        gap_minutes=gap_minutes,
        min_term_msgs=min_term_msgs,
        min_term_agents=min_term_agents,
    )[target]
    spec = _days(store, day_one)
    win = {
        "since": _iso(t0),
        "until": _iso(t1),
        "baseline_since": _iso(b0),
        "baseline_until": _iso(b1),
    }
    if spec:
        win.update(
            day_from=day_number(t0, spec),
            day_to=day_number(t1 - timedelta(microseconds=1), spec),
            days=day_range_label(t0, t1 - timedelta(microseconds=1), spec),
        )
    for b in rec["bursts"]:
        b.update(_day_fields(_as_dt(b["start"]), spec))
    return {"window": win, **rec, "notes": _RECAP_NOTES}


def period_recaps(
    store: Store,
    *,
    kind: str | None = None,
    source: str | None = None,
    channel: str | None = None,
    top_terms: int = 12,
    top_bursts: int = 5,
    max_agents: int = 30,
    gap_minutes: float = 20,
    min_term_msgs: int = 4,
    min_term_agents: int = 2,
    day_one: Any = None,
) -> list[dict[str, Any]]:
    """``window_recap`` for every period (AI Village: each village goal), in time order, each
    against the previous period. A period without an end runs to the next period's start (or
    the last message); overlapping periods are cut at the next period's start."""
    sql = "SELECT evidence_id, label, kind, start_ts, end_ts FROM periods WHERE start_ts IS NOT NULL"
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if source:
        sql += " AND source = ?"
        params.append(source)
    periods = store.all(sql + " ORDER BY start_ts, evidence_id", params)
    if not periods:
        return []
    _, last = _store_span(store, source)
    segs: list[tuple[datetime, datetime]] = []
    cut: list[bool] = []
    for i, p in enumerate(periods):
        s = p["start_ts"]
        nxt = periods[i + 1]["start_ts"] if i + 1 < len(periods) else None
        e = p["end_ts"] or nxt or ((last + timedelta(microseconds=1)) if last and last >= s else s + timedelta(days=1))
        cut.append(bool(nxt and e > nxt))
        if nxt and e > nxt:
            e = nxt
        if segs and s < segs[-1][1]:  # same start as the previous period: give it an empty slot
            s = segs[-1][1]
        segs.append((s, max(e, s + timedelta(microseconds=1))))
    recs = _recap_batch(
        store,
        segs,
        list(range(len(segs))),
        source=source,
        channel=channel,
        top_terms=top_terms,
        top_bursts=top_bursts,
        max_agents=max_agents,
        gap_minutes=gap_minutes,
        min_term_msgs=min_term_msgs,
        min_term_agents=min_term_agents,
    )
    spec = _days(store, day_one)
    out = []
    for i, (p, (s, e)) in enumerate(zip(periods, segs, strict=True)):
        rec = recs.get(i)
        if rec is None:
            continue
        win = {
            "since": _iso(s),
            "until": _iso(e),
            "baseline_since": _iso(segs[i - 1][0]) if i else None,
            "baseline_until": _iso(segs[i - 1][1]) if i else None,
        }
        item = {"period_id": p["evidence_id"], "label": p["label"], "kind": p["kind"], "start": _iso(s), "end": _iso(e)}
        if cut[i]:
            item["cut_at_next_start"] = True
        if spec:
            last_t = e - timedelta(microseconds=1)
            item.update(
                day_from=day_number(s, spec), day_to=day_number(last_t, spec), days=day_range_label(s, last_t, spec)
            )
            win.update(day_from=item["day_from"], day_to=item["day_to"], days=item["days"])
            for b in rec["bursts"]:
                b.update(_day_fields(_as_dt(b["start"]), spec))
        item["recap"] = {"window": win, **rec}
        out.append(item)
    if out:
        out[0]["notes"] = _RECAP_NOTES + ["baseline: the previous period; the first period has none."]
    return out


# ----------------------------------------------------------------------------- novel terms


_NOVEL_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def default_min_term_msgs(store: Store, source: str | None = None) -> int:
    """max(5, agent messages / 20,000): 9 on the 173k-message AI Village store, 5 on small stores."""
    w, p = _filters("messages", source, None)
    n = store.scalar(f"SELECT count(*) FROM messages WHERE {w} AND author_id NOT LIKE 'human:%'", p) or 0
    return max(5, round(n / 20_000))


def novel_terms(
    store: Store, *, min_msgs: int | None = None, min_agents: int = 3, source: str | None = None
) -> dict[str, Any]:
    """Terms that appear after the first 10% of the store's span (and at least a day in), used in
    >= min_msgs agent messages (default ``default_min_term_msgs``) by >= min_agents agents, and not
    made only of agent-name tokens. Per term: first use, coiner, totals, adopters (agents other than
    the coiner with >= 2 messages using it) and fast adopters (the same within 14 days of the first
    use). One scan of every agent message (~6 s on the AI Village store), cached per store file."""
    if min_msgs is None:
        min_msgs = default_min_term_msgs(store, source)
    key = None
    if store.path is not None:
        try:
            st = os.stat(store.path)
            key = (str(store.path), st.st_size, st.st_mtime_ns, int(min_msgs), int(min_agents), source)
        except OSError:
            key = None
    if key is not None and key in _NOVEL_CACHE:
        return _NOVEL_CACHE[key]
    t0, t1 = _store_span(store, source)
    if t0 is None:
        return {"terms": {}, "cutoff": None, "span": [None, None]}
    cutoff = t0 + max((t1 - t0) * NOVEL_SPAN_SHARE, NOVEL_MIN_DELAY)
    where, params = _filters("messages", source, None)
    rows = store.all(
        f"""WITH {_terms_cte(f"(SELECT * FROM messages WHERE {where} AND author_id NOT LIKE 'human:%')", "evidence_id, author_id, ts")},
            f AS (SELECT term, min(ts) AS first_ts, count(*) AS n, count(DISTINCT author_id) AS agents
                  FROM terms GROUP BY term
                  HAVING count(*) >= ? AND count(DISTINCT author_id) >= ? AND min(ts) > CAST(? AS TIMESTAMP)),
            ft AS (SELECT t.* FROM terms t SEMI JOIN f ON f.term = t.term),
            ta AS (SELECT term, author_id, count(*) AS n FROM ft GROUP BY 1, 2),
            co AS (SELECT term, arg_min(author_id, (ts, evidence_id)) AS coiner,
                          arg_min(evidence_id, (ts, evidence_id)) AS first_id
                   FROM ft GROUP BY 1),
            ad AS (SELECT ta.term, count(*) FILTER (WHERE ta.author_id <> co.coiner AND ta.n >= 2) AS adopters
                   FROM ta JOIN co USING (term) GROUP BY 1),
            early AS (SELECT ft.term, ft.author_id, count(*) AS n FROM ft JOIN f USING (term)
                      WHERE ft.ts < f.first_ts + INTERVAL {FAST_DAYS} DAY GROUP BY 1, 2),
            fast AS (SELECT early.term, count(*) FILTER (WHERE early.author_id <> co.coiner AND early.n >= 2) AS fast
                     FROM early JOIN co USING (term) GROUP BY 1)
            SELECT f.term, f.first_ts, f.n, f.agents, co.coiner, co.first_id, ad.adopters, coalesce(fast.fast, 0) AS fast
            FROM f JOIN co USING (term) JOIN ad USING (term) LEFT JOIN fast USING (term)""",
        params + [int(min_msgs), int(min_agents), _sql_ts(cutoff)],
    )
    name_tokens = _name_tokens(store)
    terms = {
        r["term"]: {
            "term": r["term"],
            "first_ts": r["first_ts"],
            "coiner": r["coiner"],
            "first_id": r["first_id"],
            "n": int(r["n"]),
            "agents": int(r["agents"]),
            "adopters": int(r["adopters"]),
            "adopters_fast": int(r["fast"]),
        }
        for r in rows
        if not set(r["term"].split(" ")) <= name_tokens
    }
    res = {
        "terms": terms,
        "cutoff": cutoff,
        "span": [t0, t1],
        "min_msgs": int(min_msgs),
        "min_agents": int(min_agents),
        "excluded_name_terms": len(rows) - len(terms),
    }
    if key is not None:
        if len(_NOVEL_CACHE) > 8:
            _NOVEL_CACHE.clear()
        _NOVEL_CACHE[key] = res
    return res


def _name_tokens(store: Store) -> set[str]:
    """Tokens of every agent display name and alias ("Claude Haiku 4.5" -> claude, haiku): a term
    made only of these names an agent, not an idea, and is left out of novel terms."""
    toks: set[str] = set()
    for r in store.all("SELECT display_name, aliases FROM agents"):
        for n in [r["display_name"], *(r["aliases"] or [])]:
            toks.update(re.findall(TOKEN_RE, (n or "").lower()))
    return toks


def _term_regex(term: str) -> str:
    """A regex that finds the term the way the tokenizer does (lowercased text, token boundaries)."""
    parts = [re.escape(w) for w in term.split(" ")]
    return "(^|[^a-z0-9_-])" + "[^a-z0-9_-]+".join(parts) + "([^a-z0-9_-]|$)"


def _term_ids(store: Store, term: str, source: str | None, limit: int = MAX_MOMENT_IDS) -> list[str]:
    where, params = _filters("messages", source, None)
    rows = store.all(
        f"""SELECT evidence_id FROM messages WHERE {where} AND author_id NOT LIKE 'human:%'
              AND regexp_matches(lower(content), ?) ORDER BY ts, evidence_id LIMIT ?""",
        params + [_term_regex(term), int(limit)],
    )
    return [r["evidence_id"] for r in rows]


def _agent_term_use(store: Store, aid: str, source: str | None) -> dict[str, dict[str, Any]]:
    """term -> {n, first_ts, first_id} over one agent's messages (same tokenizer)."""
    where, params = _filters("messages", source, None)
    rows = store.all(
        f"""WITH {_terms_cte(f"(SELECT * FROM messages WHERE {where} AND author_id = ?)", "evidence_id, author_id, ts")}
            SELECT term, count(*) AS n, min(ts) AS first_ts, arg_min(evidence_id, (ts, evidence_id)) AS first_id
            FROM terms GROUP BY term""",
        params + [aid],
    )
    return {r["term"]: r for r in rows}


def _novel_rule(min_msgs: int | None, min_agents: int) -> str:
    return (
        f"novel term: first used after the first {NOVEL_SPAN_SHARE:.0%} of the store's span (and at least "
        f"{NOVEL_MIN_DELAY.days} day in), in >= {min_msgs} agent messages by >= {min_agents} agents, and not made "
        "only of agent-name tokens; "
        "coined = this agent used it first; adopted = this agent used it in >= 2 messages after "
        "another agent first used it; adopters = agents other than the coiner with >= 2 messages using it."
    )


# ----------------------------------------------------------------------------- agent arc


def _resolve(store: Store, agent: str) -> dict[str, Any]:
    row = store.one("SELECT agent_id, display_name, meta FROM agents WHERE agent_id = ?", [agent])
    if row is None:
        a = store.resolve_agent(agent)
        row = store.one("SELECT agent_id, display_name, meta FROM agents WHERE agent_id = ?", [a["agent_id"]])
    return row or {}


def _period_segments(store: Store, source: str | None) -> list[dict[str, Any]]:
    """Village goals (or any periods) as non-overlapping [start, end) segments in time order."""
    sql = "SELECT evidence_id, label, start_ts, end_ts FROM periods WHERE start_ts IS NOT NULL"
    params = []
    if source:
        sql += " AND source = ?"
        params.append(source)
    ps = store.all(sql + " ORDER BY start_ts, evidence_id", params)
    _, last = _store_span(store, source)
    out = []
    for i, p in enumerate(ps):
        nxt = ps[i + 1]["start_ts"] if i + 1 < len(ps) else None
        e = p["end_ts"] or nxt or ((last + timedelta(microseconds=1)) if last else p["start_ts"] + timedelta(days=1))
        if nxt and e > nxt:
            e = nxt
        s = p["start_ts"] if not out or p["start_ts"] >= out[-1]["end"] else out[-1]["end"]
        out.append(
            {"id": p["evidence_id"], "label": p["label"], "start": s, "end": max(e, s + timedelta(microseconds=1))}
        )
    return out


def _bin_segments(lo: datetime, hi: datetime, origin: datetime, bin_days: int) -> list[dict[str, Any]]:
    width = timedelta(days=bin_days)
    k0 = math.floor((lo - origin) / width)
    out, s = [], origin + k0 * width
    while s <= hi:
        out.append({"id": None, "label": None, "start": s, "end": s + width})
        s += width
    return out


def agent_arc(
    store: Store,
    agent_id: str,
    *,
    bin_days: int = 7,
    top_partners: int = 3,
    min_period_mentions: int = 5,
    min_term_msgs: int | None = None,
    min_term_agents: int = 3,
    max_terms: int = 40,
    source: str | None = None,
    day_one: Any = None,
) -> dict[str, Any]:
    """One agent's arc: activity per bin, mention partners per period with a change score, and
    the novel terms it coined or adopted. ``agent_id`` may also be a display name or alias."""
    if bin_days < 1:
        raise ToolInputError("bin_days must be >= 1")
    a = _resolve(store, agent_id)
    aid = a["agent_id"]
    names = store.display_names()
    spec = _days(store, day_one)
    meta = a.get("meta") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    mw, mp = _filters("messages", source, None)
    aw, ap = _filters("actions", source, None)
    span = (
        store.one(
            f"""SELECT min(t) AS t0, max(t) AS t1 FROM (
              SELECT ts AS t FROM messages WHERE {mw} AND author_id = ?
              UNION ALL SELECT ts FROM actions WHERE {aw} AND agent_id = ?)""",
            mp + [aid] + ap + [aid],
        )
        or {}
    )
    result: dict[str, Any] = {
        "agent_id": aid,
        "name": a.get("display_name") or label_for(aid, names),
        "kind": "agent",
        "lab": meta.get("lab"),
        "first": _iso(span.get("t0")),
        "last": _iso(span.get("t1")),
        "bin_days": bin_days,
        "bins": [],
        "partners": [],
        "terms": [],
    }
    if span.get("t0") is None:
        result["notes"] = ["no dated messages or actions for this agent"]
        return result
    lo, hi = span["t0"], span["t1"]

    # activity per bin, aligned to Village day 1 (or the store's first UTC midnight)
    if spec:
        origin = day_start_utc(1, spec).astimezone(timezone.utc).replace(tzinfo=None)
    else:
        s0, _ = _store_span(store, source)
        origin = datetime(s0.year, s0.month, s0.day)
    width_ms = bin_days * 86_400_000
    origin_ms = int((origin - datetime(1970, 1, 1)).total_seconds() * 1000)
    rows = store.all(
        f"""SELECT k, sum(msg) AS messages, sum(act) AS actions FROM (
              SELECT (epoch_ms(ts) - ?) // ? AS k, 1 AS msg, 0 AS act FROM messages WHERE {mw} AND author_id = ?
              UNION ALL
              SELECT (epoch_ms(ts) - ?) // ? AS k, 0, 1 FROM actions WHERE {aw} AND agent_id = ?)
            GROUP BY 1 ORDER BY 1""",
        [origin_ms, width_ms] + mp + [aid, origin_ms, width_ms] + ap + [aid],
    )
    got = {int(r["k"]): r for r in rows}
    for k in range(min(got), max(got) + 1):
        s = origin + timedelta(days=k * bin_days)
        r = got.get(k, {})
        b = {
            "start": _iso(s),
            "end": _iso(s + timedelta(days=bin_days)),
            "messages": int(r.get("messages") or 0),
            "actions": int(r.get("actions") or 0),
        }
        if spec:
            b["day"] = day_number(s + timedelta(hours=12), spec) if bin_days == 1 else day_number(s, spec)
        result["bins"].append(b)

    # partners per period (village goals; fixed bins when the store has no periods)
    segs = _period_segments(store, source)
    unit = "period"
    if not segs:
        segs, unit = _bin_segments(lo, hi, origin, max(bin_days, 7)), "bin"
    segs = [s for s in segs if s["end"] > lo and s["start"] <= hi]
    if segs:
        seg_sql, seg_p = _segs_cte([(s["start"], s["end"]) for s in segs])
        m_sql, m_p = _msgs_cte(mw, segs[0]["start"], segs[-1]["end"])
        head = f"WITH {seg_sql}, {m_sql}"
        outs = store.all(
            f"""{head}, e AS (SELECT seg, unnest(recipient_ids) AS dst FROM m WHERE author_id = ? AND len(recipient_ids) > 0)
                SELECT seg, dst, count(*) AS n FROM e WHERE dst <> ? AND dst NOT LIKE 'human:%' GROUP BY 1, 2""",
            seg_p + mp + m_p + [aid, aid],
        )
        ins = store.all(
            f"""{head} SELECT seg, author_id AS src, count(*) AS n FROM m
                WHERE list_contains(recipient_ids, ?) AND author_id <> ? AND author_id NOT LIKE 'human:%'
                GROUP BY 1, 2""",
            seg_p + mp + m_p + [aid, aid],
        )
        own = {
            r["seg"]: int(r["n"])
            for r in store.all(
                f"{head} SELECT seg, count(*) AS n FROM m WHERE author_id = ? GROUP BY 1", seg_p + mp + m_p + [aid]
            )
        }
        o: dict[int, Counter[str]] = defaultdict(Counter)
        n_in: dict[int, Counter[str]] = defaultdict(Counter)
        for r in outs:
            o[r["seg"]][r["dst"]] += int(r["n"])
        for r in ins:
            n_in[r["seg"]][r["src"]] += int(r["n"])
        prev: Counter[str] | None = None
        prev_label = None
        for i, s in enumerate(segs):
            if not (own.get(i) or o[i] or n_in[i]):
                continue
            out_total, in_total = sum(o[i].values()), sum(n_in[i].values())
            js = None
            if prev is not None and out_total >= min_period_mentions and sum(prev.values()) >= min_period_mentions:
                js = _js_distance(prev, o[i])
            item = {
                "unit": unit,
                "period_id": s["id"],
                "label": s["label"],
                "start": _iso(s["start"]),
                "end": _iso(s["end"]),
                "messages": own.get(i, 0),
                "mentions_out": out_total,
                "mentions_in": in_total,
                "top_mentioned": [
                    {"agent_id": k, "name": label_for(k, names), "n": n} for k, n in o[i].most_common(top_partners)
                ],
                "top_mentioned_by": [
                    {"agent_id": k, "name": label_for(k, names), "n": n} for k, n in n_in[i].most_common(top_partners)
                ],
                "change_js": None if js is None else round(js, 3),
                "change_vs": prev_label if js is not None else None,
            }
            if spec:
                item.update(_day_fields(s["start"], spec))
            result["partners"].append(item)
            if out_total >= min_period_mentions:
                prev, prev_label = o[i], s["label"] or _iso(s["start"])

    # novel terms the agent coined or adopted
    nt = novel_terms(store, min_msgs=min_term_msgs, min_agents=min_term_agents, source=source)
    coined, adopted = [], []
    for term, u in _agent_term_use(store, aid, source).items():
        t = nt["terms"].get(term)
        if t is None:
            continue
        base = {
            "term": t["term"],
            "first_ts": _iso(u["first_ts"]),
            "first_id": u["first_id"],
            "n": int(u["n"]),
            "n_total": t["n"],
            "agents": t["agents"],
            "adopters": t["adopters"],
        }
        if spec:
            base.update(_day_fields(u["first_ts"], spec))
        if t["coiner"] == aid:
            coined.append({**base, "role": "coined"})
        elif int(u["n"]) >= 2:
            adopted.append(
                {
                    **base,
                    "role": "adopted",
                    "coined_by": label_for(t["coiner"], names),
                    "coined_ts": _iso(t["first_ts"]),
                }
            )
    coined.sort(key=lambda d: (-d["adopters"], d["first_ts"], d["term"]))
    adopted.sort(key=lambda d: (-d["adopters"], d["first_ts"], d["term"]))
    result["terms"] = coined[:max_terms] + adopted[:max_terms]
    result["term_counts"] = {"coined": len(coined), "adopted": len(adopted)}
    result["notes"] = [
        f"bins: {bin_days}-day bins aligned to "
        + ("Village day 1" if spec else "the store's first UTC midnight")
        + "; messages authored and actions taken by this agent.",
        f"partners: per {unit}, the agents this agent mentioned (out) and that mentioned it (in), self and "
        "humans excluded; change_js is the Jensen-Shannon distance (base 2, 0 = same mix, 1 = disjoint) of "
        f"the out-mention distribution vs the previous {unit} with >= {min_period_mentions} out-mentions.",
        _novel_rule(nt.get("min_msgs"), min_term_agents),
        f"terms lists up to {max_terms} coined and {max_terms} adopted terms, most adopters first; "
        "term_counts has the totals.",
    ]
    return result


# ----------------------------------------------------------------------------- notable moments


def _daily_counts(
    store: Store, entity: str, where: str, params: list[Any], spec: dict[str, str] | None
) -> list[dict[str, Any]]:
    col = {"agent": "author_id", "channel": "channel"}[entity]
    extra = " AND author_id NOT LIKE 'human:%'" if entity == "agent" else " AND channel IS NOT NULL"
    d = _local_date_sql("ts", spec)
    return store.all(
        f"SELECT {col} AS e, {d} AS d, count(*) AS n FROM messages WHERE {where}{extra} GROUP BY 1, 2", params
    )


def _bursts(
    store: Store,
    spec: dict[str, str] | None,
    source: str | None,
    *,
    baseline_days: int,
    min_msgs: int,
    z_min: float,
    lo: datetime | None,
    hi: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    where, params = _filters("messages", source, None)
    all_days = sorted(
        r["d"]
        for r in store.all(f"SELECT DISTINCT {_local_date_sql('ts', spec)} AS d FROM messages WHERE {where}", params)
    )
    pos = {d: i for i, d in enumerate(all_days)}
    found = []
    names = store.display_names()
    for entity in ("agent", "channel"):
        series: dict[str, dict[Any, int]] = defaultdict(dict)
        for r in _daily_counts(store, entity, where, params, spec):
            series[r["e"]][r["d"]] = int(r["n"])
        for e, by_day in series.items():
            first = pos[min(by_day)]
            for d, x in by_day.items():
                if x < min_msgs:
                    continue
                i = pos[d]
                prior = [by_day.get(all_days[j], 0) for j in range(max(first, i - baseline_days), i)]
                if len(prior) < max(7, baseline_days // 2):
                    continue
                mean = sum(prior) / len(prior)
                sd = math.sqrt(sum((v - mean) ** 2 for v in prior) / (len(prior) - 1)) if len(prior) > 1 else 0.0
                sd_eff = max(sd, math.sqrt(mean), 1.0)
                z = (x - mean) / sd_eff
                if z < z_min:
                    continue
                found.append(
                    {"entity": entity, "e": e, "d": d, "x": x, "mean": mean, "sd": sd, "k": len(prior), "z": z}
                )
    found.sort(key=lambda f: -f["z"])
    # one moment per entity per run of adjacent days
    kept: list[dict[str, Any]] = []
    for f in found:
        if any(k["entity"] == f["entity"] and k["e"] == f["e"] and abs(pos[k["d"]] - pos[f["d"]]) <= 2 for k in kept):
            continue
        kept.append(f)
    out = []
    dsql = _local_date_sql("ts", spec)
    d_lo = (lo - timedelta(days=1)).date() if lo is not None else None
    d_hi = (hi + timedelta(days=1)).date() if hi is not None else None
    for f in kept:
        if len(out) >= limit:
            break
        if (d_lo is not None and f["d"] < d_lo) or (d_hi is not None and f["d"] > d_hi):
            continue
        col = "author_id" if f["entity"] == "agent" else "channel"
        r = (
            store.one(
                f"""SELECT min(ts) AS t0, max(ts) AS t1,
                       list(evidence_id ORDER BY ts, evidence_id)[1:{MAX_MOMENT_IDS}] AS ids,
                       mode(channel) AS ch
                FROM messages WHERE {where} AND {col} = ? AND {dsql} = ?""",
                params + [f["e"], f["d"]],
            )
            or {}
        )
        if lo is not None and r.get("t0") is not None and (r["t0"] < lo or (hi is not None and r["t0"] >= hi)):
            continue
        when = f"Day {day_number(r['t0'], spec)}" if spec and r.get("t0") else str(f["d"])
        who = label_for(f["e"], names) if f["entity"] == "agent" else f"#{f['e']}"
        out.append(
            {
                "kind": "burst",
                "t": _iso(r.get("t0")),
                "end": _iso(r.get("t1")),
                "agent": label_for(f["e"], names) if f["entity"] == "agent" else None,
                "agent_id": f["e"] if f["entity"] == "agent" else None,
                "channel": f["e"] if f["entity"] == "channel" else r.get("ch"),
                "score": round(f["z"], 2),
                "why": (
                    f"{who}: {f['x']} msgs on {when} vs {f['mean']:.0f} ± {f['sd']:.0f} in the prior "
                    f"{f['k']} active days (z = {f['z']:.1f})"
                ),
                "ids": list(r.get("ids") or []),
                **_day_fields(r.get("t0"), spec),
            }
        )
    return out


def _silences(
    store: Store,
    spec: dict[str, str] | None,
    source: str | None,
    *,
    baseline_days: int,
    min_days: int,
    min_rate: float,
    lo: datetime | None,
    hi: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """An agent that posted on most recent active days goes quiet for >= min_days consecutive
    active days and then posts again (a departure without return is not a silence)."""
    where, params = _filters("messages", source, None)
    all_days = sorted(
        r["d"]
        for r in store.all(f"SELECT DISTINCT {_local_date_sql('ts', spec)} AS d FROM messages WHERE {where}", params)
    )
    pos = {d: i for i, d in enumerate(all_days)}
    series: dict[str, dict[Any, int]] = defaultdict(dict)
    for r in _daily_counts(store, "agent", where, params, spec):
        series[r["e"]][r["d"]] = int(r["n"])
    names = store.display_names()
    found = []
    for e, by_day in series.items():
        idx = sorted(pos[d] for d in by_day)
        for a, b in zip(idx, idx[1:], strict=False):
            gap = b - a - 1  # active days with no message from e
            if gap < min_days:
                continue
            prior = [by_day.get(all_days[j], 0) for j in range(max(idx[0], a - baseline_days + 1), a + 1)]
            if len(prior) < min(7, baseline_days):
                continue
            rate = sum(prior) / len(prior)
            if rate < min_rate:
                continue
            found.append({"e": e, "a": a, "b": b, "gap": gap, "rate": rate, "k": len(prior), "expected": rate * gap})
    found.sort(key=lambda f: (-f["expected"], f["e"]))
    out = []
    for f in found:
        if len(out) >= limit:
            break
        d0, d1 = all_days[f["a"] + 1], all_days[f["b"] - 1]
        if (lo is not None and d1 < lo.date()) or (hi is not None and d0 > hi.date()):
            continue
        last = (
            store.one(
                f"""SELECT max(ts) AS t_last FROM messages WHERE {where} AND author_id = ? AND {_local_date_sql("ts", spec)} = ?""",
                params + [f["e"], all_days[f["a"]]],
            )
            or {}
        )
        nxt = (
            store.one(
                f"""SELECT min(ts) AS t_next,
                       list(evidence_id ORDER BY ts, evidence_id)[1:{MAX_MOMENT_IDS}] AS ids
                FROM messages WHERE {where} AND author_id = ? AND {_local_date_sql("ts", spec)} = ?""",
                params + [f["e"], all_days[f["b"]]],
            )
            or {}
        )
        who = label_for(f["e"], names)
        if spec and last.get("t_last") and nxt.get("t_next"):
            a_day, b_day = day_number(last["t_last"], spec) + 1, day_number(nxt["t_next"], spec) - 1
            span = f"Day {a_day}" if a_day == b_day else f"Days {a_day}\u2013{b_day}"
        else:
            span = f"{d0} to {d1}"
        out.append(
            {
                "kind": "silence",
                "t": _iso(last.get("t_last")),
                "end": _iso(nxt.get("t_next")),
                "agent": who,
                "agent_id": f["e"],
                "channel": None,
                "score": round(f["expected"], 1),
                "why": (
                    f"{who}: no messages on {f['gap']} consecutive active days ({span}) after "
                    f"{f['rate']:.1f}/day in the prior {f['k']} active days (~{f['expected']:.0f} expected); "
                    "ids are its first messages after the silence"
                ),
                "ids": list(nxt.get("ids") or []),
                **_day_fields(last.get("t_last"), spec),
            }
        )
    return out


def _partner_shifts(
    store: Store,
    spec: dict[str, str] | None,
    source: str | None,
    *,
    min_mentions: int,
    lo: datetime | None,
    hi: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    segs = _period_segments(store, source)
    unit = "goal"
    if not segs:
        t0, t1 = _store_span(store, source)
        if t0 is None:
            return []
        segs, unit = _bin_segments(t0, t1, datetime(t0.year, t0.month, t0.day), 14), "14-day bin"
    names = store.display_names()
    mw, mp = _filters("messages", source, None)
    seg_sql, seg_p = _segs_cte([(s["start"], s["end"]) for s in segs])
    m_sql, m_p = _msgs_cte(mw, segs[0]["start"], segs[-1]["end"])
    rows = store.all(
        f"""WITH {seg_sql}, {m_sql},
              e AS (SELECT seg, author_id AS src, unnest(recipient_ids) AS dst FROM m
                    WHERE author_id NOT LIKE 'human:%' AND len(recipient_ids) > 0)
            SELECT seg, src, dst, count(*) AS n FROM e WHERE src <> dst AND dst NOT LIKE 'human:%' GROUP BY 1, 2, 3""",
        seg_p + mp + m_p,
    )
    dist: dict[str, dict[int, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    for r in rows:
        dist[r["src"]][r["seg"]][r["dst"]] += int(r["n"])
    found = []
    for src, by_seg in dist.items():
        prev_i = None
        for i in sorted(by_seg):
            cur = by_seg[i]
            if sum(cur.values()) < min_mentions:
                continue
            if prev_i is not None:
                p = by_seg[prev_i]
                js = _js_distance(p, cur)
                if js is not None:
                    found.append((js, src, prev_i, i))
            prev_i = i
    found.sort(key=lambda f: (-f[0], f[1], f[3]))
    out = []
    for js, src, a, b in found:
        if len(out) >= limit:
            break
        sa, sb = segs[a], segs[b]
        if (lo is not None and sb["start"] < lo) or (hi is not None and sb["start"] >= hi):
            continue
        pa, pb = dist[src][a], dist[src][b]
        ta, tb = pa.most_common(1)[0], pb.most_common(1)[0]
        share = lambda c, k: c[k] / sum(c.values())  # noqa: E731
        ids = (
            store.one(
                f"""SELECT list(evidence_id ORDER BY ts, evidence_id)[1:{MAX_MOMENT_IDS}] AS ids, min(ts) AS t0
                FROM messages WHERE {mw} AND author_id = ? AND list_contains(recipient_ids, ?)
                  AND ts >= CAST(? AS TIMESTAMP) AND ts < CAST(? AS TIMESTAMP)""",
                mp + [src, tb[0], _sql_ts(sb["start"]), _sql_ts(sb["end"])],
            )
            or {}
        )
        la = (sa["label"] or _iso(sa["start"]) or "")[:60]
        lb = (sb["label"] or _iso(sb["start"]) or "")[:60]
        who = label_for(src, names)
        out.append(
            {
                "kind": "partner_shift",
                "t": _iso(ids.get("t0") or sb["start"]),
                "end": _iso(sb["end"]),
                "agent": who,
                "agent_id": src,
                "channel": None,
                "score": round(js, 3),
                "why": (
                    f"{who}'s mentions changed between {unit}s (Jensen-Shannon distance {js:.2f}): top partner "
                    f"{label_for(ta[0], names)} {share(pa, ta[0]):.0%} of {sum(pa.values())} in “{la}” → "
                    f"{label_for(tb[0], names)} {share(pb, tb[0]):.0%} of {sum(pb.values())} in “{lb}”"
                ),
                "ids": list(ids.get("ids") or []),
                **_day_fields(ids.get("t0") or sb["start"], spec),
            }
        )
    return out


def _first_uses(
    store: Store,
    spec: dict[str, str] | None,
    source: str | None,
    *,
    min_msgs: int | None,
    min_agents: int,
    lo: datetime | None,
    hi: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    nt = novel_terms(store, min_msgs=min_msgs, min_agents=min_agents, source=source)
    names = store.display_names()
    items = [t for t in nt["terms"].values() if t["adopters_fast"] >= 2]
    items.sort(key=lambda t: (-t["adopters_fast"], -t["adopters"], -t["n"], t["term"]))
    # one moment per first message: "claude haiku" and "haiku" introduced together count once
    out, seen = [], set()
    for t in items:
        if len(out) >= limit:
            break
        if (lo is not None and t["first_ts"] < lo) or (hi is not None and t["first_ts"] >= hi):
            continue
        k = t["first_id"]
        if k in seen:
            continue
        seen.add(k)
        when = f"Day {day_number(t['first_ts'], spec)}" if spec else _iso(t["first_ts"])
        out.append(
            {
                "kind": "first_use",
                "t": _iso(t["first_ts"]),
                "end": None,
                "agent": label_for(t["coiner"], names),
                "agent_id": t["coiner"],
                "channel": None,
                "term": t["term"],
                "score": t["adopters_fast"],
                "why": (
                    f"“{t['term']}” first used by {label_for(t['coiner'], names)} on {when}; "
                    f"{t['adopters_fast']} other agents used it in >= 2 messages within {FAST_DAYS} days "
                    f"({t['adopters']} in all; {t['n']:,} messages by {t['agents']} agents)"
                ),
                "ids": [],
                **_day_fields(t["first_ts"], spec),
            }
        )
    return out


def _fill_term_ids(store: Store, items: list[dict[str, Any]], source: str | None) -> None:
    for it in items:
        if it["kind"] == "first_use" and not it["ids"]:
            it["ids"] = _term_ids(store, it["term"], source)


def notable_moments(
    store: Store,
    *,
    top: int = 25,
    since: Any = None,
    until: Any = None,
    source: str | None = None,
    baseline_days: int = 14,
    min_burst_msgs: int = 20,
    z_min: float = 4.0,
    min_shift_mentions: int = 20,
    min_term_msgs: int | None = None,
    min_term_agents: int = 3,
    min_silent_days: int = 3,
    min_prior_rate: float = 3.0,
    day_one: Any = None,
) -> list[dict[str, Any]]:
    """Explainable moments worth opening, newest last.

    - ``burst``: an agent's or channel's messages on one day against the prior ``baseline_days``
      active days (days with any message; for an agent, only days since its first message):
      z = (x - mean) / max(sd, sqrt(mean), 1); kept when x >= min_burst_msgs and z >= z_min.
    - ``partner_shift``: Jensen-Shannon distance between an agent's out-mention distributions in
      consecutive periods (village goals, else 14-day bins) with >= min_shift_mentions each.
    - ``silence``: an agent with >= min_prior_rate messages per active day over the prior
      ``baseline_days`` active days posts nothing for >= min_silent_days consecutive active days,
      then posts again; scored by the messages it would have sent at its prior rate.
    - ``first_use``: the first use of a novel term (see ``novel_terms``) that >= 2 other agents used
      in >= 2 messages within 14 days, scored by that number of fast adopters.

    Kinds are interleaved by rank (the best of each kind first) up to ``top`` and then sorted by
    time; ``score`` is kind-specific (z, expected missing messages, JS distance, fast adopters) and
    ``rank`` is the rank within its kind. ``ids`` holds up to 30 evidence ids to open (the day's
    messages, the first messages after a silence, the new partner mentions, or the term's first uses).
    """
    lo, hi = _as_dt(since, field="since"), _as_dt(until, end=True, field="until")
    spec = _days(store, day_one)
    kinds = {
        "burst": _bursts(
            store,
            spec,
            source,
            baseline_days=baseline_days,
            min_msgs=min_burst_msgs,
            z_min=z_min,
            lo=lo,
            hi=hi,
            limit=top,
        ),
        "partner_shift": _partner_shifts(store, spec, source, min_mentions=min_shift_mentions, lo=lo, hi=hi, limit=top),
        "first_use": _first_uses(
            store, spec, source, min_msgs=min_term_msgs, min_agents=min_term_agents, lo=lo, hi=hi, limit=top
        ),
        "silence": _silences(
            store,
            spec,
            source,
            baseline_days=baseline_days,
            min_days=min_silent_days,
            min_rate=min_prior_rate,
            lo=lo,
            hi=hi,
            limit=top,
        ),
    }
    for items in kinds.values():
        for r, it in enumerate(items, 1):
            it["rank"] = r
    picked: list[dict[str, Any]] = []
    depth = 0
    while len(picked) < top and any(depth < len(v) for v in kinds.values()):
        for k in ("burst", "silence", "partner_shift", "first_use"):
            if depth < len(kinds[k]) and len(picked) < top:
                picked.append(kinds[k][depth])
        depth += 1
    _fill_term_ids(store, picked, source)
    picked.sort(key=lambda m: (m["t"] or "", m["kind"], -float(m["score"])))
    return picked
