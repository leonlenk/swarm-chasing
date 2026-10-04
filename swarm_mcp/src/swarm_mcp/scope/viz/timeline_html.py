"""``swarm-mcp render timeline``: a self-contained HTML explorer built around an agent timeline.

Figure 1 is the timeline: one row per agent (the ``top`` agents by message count within the
filters, ordered by first message by default) over a shared time axis in UTC, with Village
days (``Day N``) on a second axis when the store has village goals. Zoomed out, each row is a
histogram of that agent's messages per time bin, stacked by channel (counts include every
matching message); zoomed in, each message is a tick. Goals/periods run along the top; click
one to zoom to it. Hovering shows counts or a masked snippet; clicking a message copies its
evidence id and opens the thread reader (the surrounding conversation in that room).
Figure 2 is the mention matrix for the window on screen (who names whom; rows and columns in
row order), which follows zoom and pan. The data behind both comes from the queries in this
module; the page (``assets/timeline.html`` + ``assets/timeline.js``) only draws it, in the
shared paper style (``assets/paper.css``), and can export the current view at 5.5 in as SVG
or PNG (``assets/paperkit.js``). It works offline from ``file://``: no external scripts,
styles or fonts.

Size control. When more than ``max_marks`` messages match, the messages kept for hover and
reading are sampled deterministically: within each lane, messages are ordered by
(ts, evidence_id) and an evenly spaced subset is kept whose size is proportional to the
lane's volume (``floor(n_lane * max_marks / n_all_lanes)``), so the same inputs always give
the same page. The histograms and the mention matrix are binned in SQL over *all* matching
messages, so sampling never hides activity. Both the page and the returned dict say when
sampling happened. When nothing is sampled and the budget allows, messages by humans and by
agents outside the lanes are embedded too, so the thread reader shows whole conversations.

Security. Snippets are untrusted agent output. They are masked with the ``Scrubber`` and
truncated in Python before embedding; the data is embedded as JSON in
``<script type="application/json">`` with ``<``, ``>`` and ``&`` escaped (so no content can
close the tag); header strings go through ``html.escape``; the page's JS only ever writes
data-derived strings with ``textContent`` or canvas ``fillText``, never ``innerHTML``, and the
SVG export serialises DOM text nodes, so labels stay inert there too.
"""

from __future__ import annotations

import html
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from swarm_mcp.scope import db
from swarm_mcp.scope.viz import pagekit
from swarm_mcp.toolkit import Scrubber, ToolInputError

DEFAULT_MAX_MARKS = 30_000
MAX_SNIPPET_CHARS = 2_000
PALETTE_SLOTS = 8  # categorical slots; further channels fold into a neutral "other" colour
DENSITY_BINS = 600  # target max bins for the per-agent histograms and the mention matrix
MAX_PERIODS = 400  # per period kind drawn along the top
LAB_GROUPS = ("Anthropic", "OpenAI", "Google")  # anything else is "Other"
_NICE_BINS_MS = [
    60_000,
    300_000,
    900_000,
    3_600_000,
    3 * 3_600_000,
    6 * 3_600_000,
    12 * 3_600_000,
    86_400_000,
    2 * 86_400_000,
    7 * 86_400_000,
    14 * 86_400_000,
    30 * 86_400_000,
]


def _iso_ms(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_param(ts: str | None) -> str | None:
    """'2025-10-20 00:00:00.000000' -> '2025-10-20T00:00:00Z' (for the header)."""
    return ts[:19].replace(" ", "T") + "Z" if ts else None


def _json_for_script(obj: Any) -> str:
    """JSON safe to place inside a <script> element: nothing can close the tag."""
    return pagekit.json_for_script(obj)


def _snippet(text: str | None, scrub: Scrubber, max_chars: int) -> str:
    """Mask the FULL text first (so nothing half-cut escapes the masks), collapse
    whitespace, then cut to ``max_chars`` with a trailing ellipsis."""
    if max_chars <= 0 or not text:
        return ""
    clean = " ".join(scrub(text).split())
    if len(clean) <= max_chars:
        return clean
    return clean[:max_chars].rstrip() + "…"


def _id_prefix(ids: list[str]) -> str:
    """Common evidence-id prefix up to the last ':' (e.g. 'village:msg:'), factored out of the page."""
    if not ids:
        return ""
    p = os.path.commonprefix(ids)
    return p[: p.rfind(":") + 1]


def _actor_kind(author_id: str) -> str:
    """Presentation-only actor kind: 'agent', 'human' or 'external' (external:/unknown ids)."""
    if author_id.startswith("human:"):
        return "human"
    if author_id.startswith("external:") or author_id in ("unknown", "") or author_id.startswith("unknown"):
        return "external"
    return "agent"


def _lab_group(lab: str | None) -> str | None:
    if not lab:
        return None
    return next((g for g in LAB_GROUPS if g.lower() in lab.lower()), "Other")


def render_timeline(
    db_path: Path | str,
    out_path: Path | str,
    *,
    top: int = 12,
    since: str | None = None,
    until: str | None = None,
    channel: str | None = None,
    source: str | None = None,
    scrub: Scrubber | None = None,
    snippet_chars: int = 160,
    max_marks: int = DEFAULT_MAX_MARKS,
    day_one: str | bool | None = None,
    annotations: list[dict[str, Any]] | None = None,
    sweeps: list[Path | str] | None = None,
    explore: bool = True,
) -> dict[str, Any]:
    """Render the explorer HTML to ``out_path`` and return a summary dict.

    ``since``/``until`` are already-parsed UTC strings (``toolkit.parse_time``),
    the window is [since, until). ``scrub=None`` means the default (enabled)
    ``Scrubber``; pass ``Scrubber(enabled=False)`` to disable masking.
    ``day_one``: Village day 1 as an ISO date; None derives it from the store's first
    village goal (``pagekit.village_days``), False turns the day axis off.
    ``annotations``: optional extra events drawn along the top, each
    ``{"t": ISO time or epoch ms, "end": optional, "label": str}`` (e.g. planted events).
    ``sweeps``: optional sweep result files (``<sweep_id>.jsonl``); each adds a "share of records
    judged yes" series to the metrics panel. ``explore=False`` skips the linked panels that need
    ``analysis.recap`` / ``analysis.series`` (period recaps, notable moments, agent arcs, metrics).
    """
    top = max(1, int(top))
    max_marks = max(1, int(max_marks))
    snippet_chars = max(0, min(int(snippet_chars), MAX_SNIPPET_CHARS))
    scrub = scrub if scrub is not None else Scrubber()
    out = Path(out_path).expanduser()

    with db.connect(Path(db_path)) as store:
        if source:
            sources = [r["source"] for r in store.all("SELECT DISTINCT source FROM messages ORDER BY 1")]
            if source not in sources:
                raise ToolInputError(f"Unknown source {source!r}. Sources: {', '.join(sources) or '(none)'}")
        ch = store.resolve_channel(channel, source)

        where: list[str] = []
        params: list[Any] = []
        if source:
            where.append("source = ?")
            params.append(source)
        if ch is not None:
            where.append("channel = ?")
            params.append(ch)
        dw, dp = db.date_filters("ts", since, until)
        where += dw
        params += dp
        base = " AND ".join(where) or "TRUE"
        dated = f"({base}) AND ts IS NOT NULL"

        agg = (
            store.one(
                f"""SELECT count(*) AS total,
                       count(*) FILTER (WHERE author_id LIKE 'human:%') AS humans,
                       count(*) FILTER (WHERE ts IS NULL) AS undated,
                       count(DISTINCT author_id) FILTER (WHERE author_id NOT LIKE 'human:%') AS n_agents,
                       epoch_ms(min(ts)) AS t_min, epoch_ms(max(ts)) AS t_max
                FROM messages WHERE {base}""",
                params,
            )
            or {}
        )
        total = int(agg.get("total") or 0)

        lane_rows = store.all(
            f"""SELECT author_id, count(*) AS n, epoch_ms(min(ts)) AS first_ms
                FROM messages WHERE {dated} AND author_id NOT LIKE 'human:%'
                GROUP BY 1 ORDER BY n DESC, author_id LIMIT ?""",
            params + [top],
        )
        names = store.display_names()
        lane_ids = [r["author_id"] for r in lane_rows]
        lane_total = sum(int(r["n"]) for r in lane_rows)
        sampled = lane_total > max_marks

        # channel -> colour slot by GLOBAL volume, so colours are stable across filtered renders
        global_channels = [
            r["ch"]
            for r in store.all(
                "SELECT coalesce(channel, '(none)') AS ch, count(*) AS n FROM messages GROUP BY 1 ORDER BY n DESC, ch"
            )
        ]
        slot_of = {c: (i if i < PALETTE_SLOTS else -1) for i, c in enumerate(global_channels)}

        rows: list[tuple[Any, ...]] = []
        dens_rows: list[dict[str, Any]] = []
        chan_counts: list[dict[str, Any]] = []
        bin_ms = _NICE_BINS_MS[-1]
        bin_base = 0
        t_hi = 0
        extra: dict[str, Any] = {}
        if lane_ids:
            ph = ", ".join("?" for _ in lane_ids)
            lane_where = f"{dated} AND author_id IN ({ph})"
            lane_params = params + lane_ids
            if sampled:
                # evenly spaced, volume-proportional subset per lane (deterministic)
                sql = f"""
                    WITH f AS (
                        SELECT evidence_id,
                               row_number() OVER (PARTITION BY author_id ORDER BY ts, evidence_id) AS rn,
                               count(*) OVER (PARTITION BY author_id) AS n
                        FROM messages WHERE {lane_where}
                    ), q AS (
                        SELECT evidence_id, rn, n, (n * CAST(? AS BIGINT)) // CAST(? AS BIGINT) AS quota FROM f
                    )
                    SELECT m.evidence_id, m.author_id, m.channel, epoch_ms(m.ts) AS t, m.content, m.recipient_ids
                    FROM messages m
                    WHERE m.evidence_id IN (
                        SELECT evidence_id FROM q WHERE (rn * quota) // n > ((rn - 1) * quota) // n
                    )
                    ORDER BY m.author_id, m.ts, m.evidence_id"""
                rows = store.con.execute(sql, lane_params + [max_marks, lane_total]).fetchall()
            else:
                sql = f"""SELECT evidence_id, author_id, channel, epoch_ms(ts) AS t, content, recipient_ids
                          FROM messages WHERE {lane_where} ORDER BY author_id, ts, evidence_id"""
                rows = store.con.execute(sql, lane_params).fetchall()

            t_lo = min(int(r["first_ms"]) for r in lane_rows)
            t_hi = int(store.scalar(f"SELECT epoch_ms(max(ts)) FROM messages WHERE {lane_where}", lane_params))
            span = max(t_hi - t_lo, 1)
            bin_ms = next((b for b in _NICE_BINS_MS if span / b <= DENSITY_BINS), _NICE_BINS_MS[-1])
            bin_base = (t_lo // bin_ms) * bin_ms
            dens_rows = store.all(
                f"""SELECT author_id, coalesce(channel, '(none)') AS ch,
                           (epoch_ms(ts) - CAST(? AS BIGINT)) // CAST(? AS BIGINT) AS b, count(*) AS c
                    FROM messages WHERE {lane_where} GROUP BY 1, 2, 3 ORDER BY 1, 3, 2""",
                [bin_base, bin_ms] + lane_params,
            )
            chan_counts = store.all(
                f"""SELECT coalesce(channel, '(none)') AS ch, count(*) AS n
                    FROM messages WHERE {lane_where} GROUP BY 1 ORDER BY n DESC, ch""",
                lane_params,
            )
            extra = _extras(
                store,
                lane_ids=lane_ids,
                lane_where=lane_where,
                lane_params=lane_params,
                dated=dated,
                params=params,
                source=source,
                since=since,
                until=until,
                bin_base=bin_base,
                bin_ms=bin_ms,
                t_lo=t_lo,
                t_hi=t_hi,
                room_left=max_marks - lane_total if not sampled else 0,
            )
        day_spec = pagekit.village_days(store, day_one)
        explored: dict[str, Any] = {}
        if lane_ids and explore:
            explored = _explore(
                store,
                lane_ids=lane_ids,
                since=since,
                until=until,
                source=source,
                channel=ch,
                day_spec=day_spec,
                periods=extra.get("periods", []),
                sweeps=sweeps or [],
            )
            have = {r[0] for r in rows} | {r[0] for r in extra.get("context", [])}
            extra["excerpts"] = _messages_by_id(
                store, [i for i in explored.pop("ids", []) if i not in have], where=dated, params=params
            )

    # ---- assemble the compact page payload -------------------------------------------------
    lane_index = {aid: i for i, aid in enumerate(lane_ids)}
    rows.sort(key=lambda r: (lane_index[r[1]], r[3], r[0]))  # lane order, then time
    ctx_rows = sorted(extra.get("context", []) + extra.get("excerpts", []), key=lambda r: (r[3], r[0]))
    all_rows = rows + ctx_rows
    chan_names = [c["ch"] for c in chan_counts]
    for r in ctx_rows:  # context may use channels the lanes never posted in
        c = r[2] if r[2] is not None else "(none)"
        if c not in chan_names:
            chan_names.append(c)
            chan_counts.append({"ch": c, "n": 0})
    chan_index = {c: i for i, c in enumerate(chan_names)}
    ids = [r[0] for r in all_rows]
    prefix = _id_prefix(ids)
    t0 = (min((int(r[3]) for r in all_rows), default=0) // 1000) * 1000

    labs = extra.get("labs", {})
    actors: list[dict[str, Any]] = [
        {
            "name": db.label_for(aid, names),
            "id": aid,
            "kind": _actor_kind(aid),
            "lab": labs.get(aid),
            "labg": _lab_group(labs.get(aid)),
        }
        for aid in lane_ids
    ]
    actor_index = dict(lane_index)
    for r in ctx_rows:
        if r[1] not in actor_index:
            actor_index[r[1]] = len(actors)
            actors.append(
                {"name": db.label_for(r[1], names), "id": r[1], "kind": _actor_kind(r[1]), "lab": None, "labg": None}
            )

    lanes: list[dict[str, Any]] = [
        {
            "name": db.label_for(r["author_id"], names),
            "id": r["author_id"],
            "n": int(r["n"]),
            "first": int(r["first_ms"]),
            "a": 0,
            "b": 0,
            "kind": _actor_kind(r["author_id"]),
            "lab": labs.get(r["author_id"]),
            "labg": _lab_group(labs.get(r["author_id"])),
        }
        for r in lane_rows
    ]
    for i, r in enumerate(rows):
        lane = lanes[lane_index[r[1]]]
        if lane["b"] == 0:
            lane["a"] = i
        lane["b"] = i + 1
    for lane in lanes:
        if lane["b"] == 0:
            lane["a"] = lane["b"] = 0
        lane["shown"] = lane["b"] - lane["a"]

    dens: list[list[int]] = [[] for _ in lanes]
    for d in dens_rows:
        dens[lane_index[d["author_id"]]] += [int(d["b"]), chan_index[d["ch"]], int(d["c"])]

    recips = []  # [row, lane, lane, ...] for rows that name agents shown in the lanes
    for i, r in enumerate(all_rows):
        named = [lane_index[x] for x in (r[5] or []) if x in lane_index and x != r[1]]
        if named:
            recips.append([i] + sorted(set(named)))

    marks = len(rows)
    payload = {
        "v": 2,
        "t0": t0,
        "t": [int(r[3]) // 1000 - t0 // 1000 for r in all_rows],  # seconds since t0
        "c": [chan_index[r[2] if r[2] is not None else "(none)"] for r in all_rows],
        "au": [actor_index[r[1]] for r in all_rows],
        "len": [len(r[4] or "") for r in all_rows],
        "idp": prefix,
        "id": [i[len(prefix) :] for i in ids],
        "s": [_snippet(r[4], scrub, snippet_chars) for r in all_rows],
        "rc": recips,
        "actors": actors,
        "lanes": lanes,
        "ctx": {
            "a": marks,
            "n": len(ctx_rows),
            "complete": bool(extra.get("context_complete")),
            "excerpts": len(extra.get("excerpts", [])),
        },
        "dens": {"bin": bin_ms, "base": bin_base, "lanes": dens},
        "acts": extra.get("acts", [[] for _ in lanes]),
        "ment": extra.get("ment", []),
        "periods": extra.get("periods", []),
        "notes": _annotations(annotations),
        "x": explored,
        "days": day_spec,
        "channels": [{"name": c["ch"], "n": int(c["n"]), "slot": slot_of.get(c["ch"], -1)} for c in chan_counts],
        "start": min((ln["first"] for ln in lanes), default=agg.get("t_min") or 0),
        "end": max((int(r[3]) for r in rows), default=agg.get("t_max") or 0),
        "sampled": sampled,
    }
    if lane_ids:
        payload["end"] = max(payload["end"], t_hi)

    rng = [_iso_ms(agg.get("t_min")), _iso_ms(agg.get("t_max"))]
    humans = int(agg.get("humans") or 0)
    n_agents = int(agg.get("n_agents") or 0)
    other_msgs = total - humans - lane_total - int(agg.get("undated") or 0)
    meta = {
        "source": source,
        "channel": ch,
        "since": _iso_param(since),
        "until": _iso_param(until),
        "top": top,
        "total": total,
        "humans": humans,
        "external": 0,
        "undated": int(agg.get("undated") or 0),
        "n_agents": n_agents,
        "n_lanes": len(lanes),
        "lane_total": lane_total,
        "other_msgs": max(other_msgs, 0),
        "marks": marks,
        "context": len(ctx_rows),
        "sampled": sampled,
        "max_marks": max_marks,
        "snippet_chars": snippet_chars,
        "range": rng,
        "days": pagekit.day_range_label(agg.get("t_min"), agg.get("t_max"), day_spec),
        "db": Path(db_path).name,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload["meta"] = meta
    page = _page(payload, meta)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8", errors="replace")

    return {
        "out": str(out),
        "agents": [ln["name"] for ln in lanes],
        "marks": marks,
        "sampled": sampled,
        "total_messages_in_filter": total,
        "range": rng,
        "bytes": out.stat().st_size,
        "messages_in_lanes": lane_total,
        "human_messages_excluded": humans,
        "max_marks": max_marks,
        "snippet_chars": snippet_chars,
        "context_messages": len(ctx_rows),
        "periods": len(payload["periods"]),
        "day_one": day_spec["day_one"] if day_spec else None,
    }


# --------------------------------------------------------------------------- data for the linked views


def _extras(
    store: db.Store,
    *,
    lane_ids: list[str],
    lane_where: str,
    lane_params: list[Any],
    dated: str,
    params: list[Any],
    source: str | None,
    since: str | None,
    until: str | None,
    bin_base: int,
    bin_ms: int,
    t_lo: int,
    t_hi: int,
    room_left: int,
) -> dict[str, Any]:
    """Everything beyond the lanes: labs, mention and action bins, periods, thread context."""
    lane_index = {aid: i for i, aid in enumerate(lane_ids)}
    ph = ", ".join("?" for _ in lane_ids)
    out: dict[str, Any] = {}

    labs = {}
    if store.has_table("agents"):
        for r in store.all(
            f"SELECT agent_id, json_extract_string(meta, '$.lab') AS lab FROM agents WHERE agent_id IN ({ph})", lane_ids
        ):
            if r["lab"]:
                labs[r["agent_id"]] = r["lab"]
    out["labs"] = labs

    # who names whom, binned like the histograms, over ALL lane messages (never sampled)
    ment: list[int] = []
    for r in store.con.execute(
        f"""SELECT author_id, dst, (epoch_ms(ts) - CAST(? AS BIGINT)) // CAST(? AS BIGINT) AS b, count(*) AS c
            FROM (SELECT author_id, ts, unnest(recipient_ids) AS dst FROM messages
                  WHERE {lane_where} AND len(recipient_ids) > 0)
            WHERE dst IN ({ph}) AND dst <> author_id
            GROUP BY 1, 2, 3 ORDER BY 3, 1, 2""",
        [bin_base, bin_ms] + lane_params + lane_ids,
    ).fetchall():
        ment += [int(r[2]), lane_index[r[0]], lane_index[r[1]], int(r[3])]
    out["ment"] = ment

    # agent actions (session goals, summaries, ...) per lane and bin
    acts: list[list[int]] = [[] for _ in lane_ids]
    if store.has_table("actions"):
        aw, ap = db.date_filters("ts", since, until)
        if source:
            aw.append("source = ?")
            ap.append(source)
        cond = " AND ".join(aw + ["ts IS NOT NULL", f"agent_id IN ({ph})"])
        for r in store.con.execute(
            f"""SELECT agent_id, (epoch_ms(ts) - CAST(? AS BIGINT)) // CAST(? AS BIGINT) AS b, count(*) AS c
                FROM actions WHERE {cond} GROUP BY 1, 2 ORDER BY 1, 2""",
            [bin_base, bin_ms] + ap + lane_ids,
        ).fetchall():
            acts[lane_index[r[0]]] += [int(r[1]), int(r[2])]
    out["acts"] = acts

    out["periods"] = _periods(store, source=source, t_lo=t_lo, t_hi=t_hi)

    # thread context: messages by humans and by agents outside the lanes, when they fit the budget
    out["context"] = []
    out["context_complete"] = False
    n_ctx = int(
        store.scalar(f"SELECT count(*) FROM messages WHERE {dated} AND author_id NOT IN ({ph})", params + lane_ids) or 0
    )
    if room_left > 0 and n_ctx <= room_left:
        out["context"] = store.con.execute(
            f"""SELECT evidence_id, author_id, channel, epoch_ms(ts) AS t, content, recipient_ids
                FROM messages WHERE {dated} AND author_id NOT IN ({ph}) ORDER BY ts, evidence_id""",
            params + lane_ids,
        ).fetchall()
        out["context_complete"] = True
    return out


MAX_RECAP_TERMS = 10
MAX_RECAP_BURSTS = 3
MAX_BURST_IDS = 25  # messages per burst embedded for the thread reader
MAX_ARC_TERMS = 20


def _ms(v: Any) -> int | None:
    """ISO string / datetime -> epoch ms (UTC); None passes through."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def _explore(
    store: db.Store,
    *,
    lane_ids: list[str],
    since: str | None,
    until: str | None,
    source: str | None,
    channel: str | None,
    day_spec: dict[str, str] | None,
    periods: list[dict[str, Any]],
    sweeps: list[Path | str],
) -> dict[str, Any]:
    """Precomputed data for the linked panels, trimmed for the page. Each piece is optional: a
    failure is reported in ``errors`` and the page simply leaves that panel out."""
    from swarm_mcp.scope.analysis import recap, series

    lane_index = {aid: i for i, aid in enumerate(lane_ids)}
    day_one = day_spec["day_one"] if day_spec else False
    out: dict[str, Any] = {"errors": []}
    ids: list[str] = []

    def guard(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one panel failing must not sink the page
            out["errors"].append(f"{name}: {type(e).__name__}: {e}")
            return None

    def trim_recap(rc: dict[str, Any]) -> dict[str, Any]:
        terms = [
            {k: t.get(k) for k in ("term", "n", "n_before", "agents", "why", "first_id")}
            for t in (rc.get("rising_terms") or [])[:MAX_RECAP_TERMS]
        ]
        bursts = []
        for b in (rc.get("bursts") or [])[:MAX_RECAP_BURSTS]:
            bids = (b.get("ids") or [])[:MAX_BURST_IDS]
            ids.extend(bids)
            bursts.append(
                {
                    "channel": b.get("channel"),
                    "s": _ms(b.get("start")),
                    "e": _ms(b.get("end")),
                    "n": b.get("n"),
                    "agents": b.get("agents") or [],
                    "ids": bids,
                }
            )
        ids.extend(t["first_id"] for t in terms if t.get("first_id"))
        return {
            "totals": rc.get("totals") or {},
            "terms": terms,
            "bursts": bursts,
            "baseline": bool((rc.get("totals") or {}).get("baseline_agent_messages")),
        }

    # recaps: the whole render window, and every period on the page against the previous one
    whole = guard(
        "window_recap", lambda: recap.window_recap(store, since, until, source=source, channel=channel, day_one=day_one)
    )
    if whole:
        out["recap_all"] = trim_recap(whole)
    if periods:
        wanted = {p["id"] for p in periods}
        pr = (
            guard("period_recaps", lambda: recap.period_recaps(store, source=source, channel=channel, day_one=day_one))
            or []
        )
        out["recaps"] = {r["period_id"]: trim_recap(r.get("recap") or {}) for r in pr if r.get("period_id") in wanted}

    # notable moments inside the render window
    nm = (
        guard(
            "notable_moments",
            lambda: recap.notable_moments(store, since=since, until=until, source=source, day_one=day_one),
        )
        or []
    )
    moments = []
    for m in nm:
        mids = (m.get("ids") or [])[:30]
        ids.extend(mids)
        moments.append(
            {
                "kind": m.get("kind"),
                "t": _ms(m.get("t")),
                "e": _ms(m.get("end")),
                "agent": m.get("agent"),
                "lane": lane_index.get(m.get("agent_id")),
                "channel": m.get("channel"),
                "term": m.get("term"),
                "score": m.get("score"),
                "why": m.get("why"),
                "ids": mids,
            }
        )
    out["moments"] = moments

    # one arc per agent row
    arcs: dict[str, Any] = {}
    for aid in lane_ids:
        arc = guard(
            f"agent_arc {aid}",
            lambda aid=aid: recap.agent_arc(store, aid, top_partners=5, source=source, day_one=day_one),
        )
        if not arc:
            continue
        terms = arc.get("terms") or []
        keep = [t for t in terms if t.get("role") == "coined"][:MAX_ARC_TERMS] + [
            t for t in terms if t.get("role") == "adopted"
        ][:MAX_ARC_TERMS]
        arcs[str(lane_index[aid])] = {
            "bins": [[_ms(b.get("start")), b.get("messages", 0), b.get("actions", 0)] for b in arc.get("bins") or []],
            "bin_days": arc.get("bin_days"),
            "partners": [
                {
                    "id": p.get("period_id"),
                    "label": p.get("label"),
                    "s": _ms(p.get("start")),
                    "e": _ms(p.get("end")),
                    "out": p.get("mentions_out", 0),
                    "in": p.get("mentions_in", 0),
                    "to": [[x.get("name"), x.get("n")] for x in p.get("top_mentioned") or []],
                    "from": [[x.get("name"), x.get("n")] for x in p.get("top_mentioned_by") or []],
                    "js": p.get("change_js"),
                }
                for p in arc.get("partners") or []
            ],
            "terms": [
                {k: t.get(k) for k in ("term", "role", "first_id", "n", "n_total", "agents", "adopters")}
                | {"t": _ms(t.get("first_ts"))}
                for t in keep
            ],
            "term_counts": arc.get("term_counts") or {},
            "notes": arc.get("notes") or [],
        }
    out["arcs"] = arcs

    # metric-over-time series (daily, Village days): store counts and rates, plus any sweeps
    def trim_series(ms: dict[str, Any], label: str) -> dict[str, Any]:
        return {
            "label": label,
            "unit": ms.get("unit"),
            "kind": ms.get("kind"),
            "by": ms.get("by"),
            "window": ms.get("window"),
            "starts": [_ms(x) for x in ms.get("starts") or []],
            "groups": [
                {"key": g.get("key"), "name": g.get("name"), "pts": [p[:5] + p[5:7] for p in g.get("points") or []]}
                for g in ms.get("groups") or []
            ],
            "notes": ms.get("notes") or [],
        }

    specs = [
        ("messages", "lab", "Agent messages per day, by lab"),
        ("mention_rate", "lab", "Share of messages that name another agent, by lab"),
        ("messages", "channel", "Agent messages per day, by channel"),
    ]
    sers = []
    for metric, by, label in specs:
        ms = guard(
            f"metric_series {metric}/{by}",
            lambda metric=metric, by=by: series.metric_series(
                store,
                metric=metric,
                by=by,
                top=6,
                since=since,
                until=until,
                source=source,
                channel=channel,
                day_one=day_one,
            ),
        )
        if ms and ms.get("groups"):
            sers.append(trim_series(ms, label))
    for sp in sweeps:
        ms = guard(
            f"sweep {sp}",
            lambda sp=sp: series.metric_series(
                store,
                metric="sweep",
                by="lab",
                top=6,
                since=since,
                until=until,
                source=source,
                channel=channel,
                sweep_path=sp,
                day_one=day_one,
            ),
        )
        if ms and ms.get("groups"):
            sers.append(trim_series(ms, f"Share of records judged yes ({Path(sp).stem}), by lab"))
    agent_rates = guard(
        "metric_series mention_rate/agent",
        lambda: series.metric_series(
            store,
            metric="mention_rate",
            by="agent",
            top=min(len(lane_ids), 24),
            since=since,
            until=until,
            source=source,
            channel=channel,
            day_one=day_one,
        ),
    )
    if agent_rates:
        tr = trim_series(agent_rates, "Share of messages that name another agent")
        out["agent_rates"] = {
            "starts": tr["starts"],
            "window": tr["window"],
            "notes": tr["notes"],
            "lanes": {str(lane_index[g["key"]]): g["pts"] for g in tr["groups"] if g["key"] in lane_index},
        }
    out["series"] = sers
    out["ids"] = list(dict.fromkeys(i for i in ids if i))
    if not out["errors"]:
        del out["errors"]
    return out


def _messages_by_id(store: db.Store, ids: list[str], *, where: str, params: list[Any]) -> list[tuple[Any, ...]]:
    """Rows (same shape as the lane rows) for the given evidence ids that fall inside the render's
    filters (``where``), e.g. the busiest threads and the messages behind a notable moment."""
    if not ids:
        return []
    return store.con.execute(
        f"""SELECT evidence_id, author_id, channel, epoch_ms(ts) AS t, content, recipient_ids
            FROM messages WHERE ({where}) AND list_contains(?, evidence_id) ORDER BY ts, evidence_id""",
        params + [ids],
    ).fetchall()


def _periods(store: db.Store, *, source: str | None, t_lo: int, t_hi: int) -> list[dict[str, Any]]:
    """Periods (e.g. village goals) overlapping [t_lo, t_hi], at most two kinds of <= MAX_PERIODS each."""
    if not store.has_table("periods"):
        return []
    cond = "start_ts IS NOT NULL AND epoch_ms(start_ts) <= ? AND (end_ts IS NULL OR epoch_ms(end_ts) >= ?)"
    prm: list[Any] = [t_hi, t_lo]
    if source:
        cond += " AND source = ?"
        prm.append(source)
    kinds = store.all(f"SELECT kind, count(*) AS n FROM periods WHERE {cond} GROUP BY 1 ORDER BY 2, 1", prm)
    keep = [k["kind"] for k in kinds if k["n"] <= MAX_PERIODS][:2]
    if not keep:
        return []
    kph = ", ".join("?" for _ in keep)
    return [
        {
            "id": r["evidence_id"],
            "kind": r["kind"],
            "label": " ".join((r["label"] or "").split())[:300],
            "s": int(r["s"]),
            "e": int(r["e"]) if r["e"] is not None else None,
        }
        for r in store.all(
            f"""SELECT evidence_id, kind, label, epoch_ms(start_ts) AS s, epoch_ms(end_ts) AS e
                FROM periods WHERE {cond} AND kind IN ({kph}) ORDER BY start_ts, evidence_id""",
            prm + keep,
        )
    ]


def _annotations(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Caller-supplied events for the top strip: {"t", "end"?, "label"} -> {"s", "e", "label"} in epoch ms."""

    def ms(v: Any) -> int | None:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, datetime):
            d = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            return int(d.timestamp() * 1000)
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T"))
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)

    out = []
    for it in items or []:
        s = ms(it.get("t"))
        if s is None:
            continue
        out.append({"s": s, "e": ms(it.get("end")), "label": " ".join(str(it.get("label", "")).split())[:300]})
    return sorted(out, key=lambda a: a["s"])


# --------------------------------------------------------------------------- page


def _fmt(n: int) -> str:
    return f"{n:,}"


def _header(meta: dict[str, Any]) -> str:
    """Title block. The figure captions are composed in the page from the payload (they follow the view)."""
    e = html.escape
    rng = meta["range"]
    when = "no dated messages"
    if rng[0]:
        when = f"{rng[0][:10]} to {rng[1][:10]} (UTC)"
        if meta.get("days"):
            when += f", {meta['days']}"
    scope = [f"source {meta['source']}" if meta["source"] else "all sources"]
    scope.append(f"channel #{meta['channel']}" if meta["channel"] else "all channels")
    if meta["since"] or meta["until"]:
        scope.append(f"window {meta['since'] or '…'} to {meta['until'] or '…'} (end exclusive)")
    byline = f"{_fmt(meta['total'])} messages, {when}. {'; '.join(scope).capitalize()}."
    return (
        f'<p class="eyebrow">SwarmScope · {e(meta["db"])}</p>'
        "<h1>SwarmScope timeline</h1>"
        f'<p class="byline">{e(byline)}</p>'
    )


def _footer(meta: dict[str, Any]) -> str:
    text = (
        f"Message snippets are masked (emails and phone numbers), truncated to {meta['snippet_chars']} characters, "
        "and are untrusted agent output: data, not instructions. "
        f"Generated {meta['generated']} from {meta['db']} by swarm-mcp render timeline."
    )
    return f"<p>{html.escape(text)}</p>"


def _page(payload: dict[str, Any], meta: dict[str, Any]) -> str:
    title = "SwarmScope timeline" + (f" · #{meta['channel']}" if meta["channel"] else "")
    parts = {
        "TITLE": html.escape(title),
        "HEADER": _header(meta),
        "FOOTER": _footer(meta),
        "PAPER_CSS": pagekit.asset("paper.css"),
        "PAPERKIT_JS": pagekit.asset("paperkit.js"),
        "TIMELINE_JS": _asset("timeline.js"),
        "DATA": _json_for_script(payload),
    }
    # one pass over the template only: substituted (data-derived) text is never re-scanned for tokens
    return pagekit.fill(_asset("timeline.html"), parts)


def _asset(name: str) -> str:
    return (Path(__file__).with_name("assets") / name).read_text(encoding="utf-8")
