"""``swarm-mcp render timeline``: a self-contained HTML agent swimlane.

One lane per agent (the ``top`` agents by message count within the filters),
one mark per message at x = time, coloured by channel. Hovering a mark shows
its evidence id, UTC time, author, channel and a short masked snippet;
clicking copies the evidence id. The page draws on a ``<canvas>`` (the full
AI Village store is >100k messages) and works offline from ``file://``: no
external scripts, styles or fonts.

Size control. When more than ``max_marks`` messages match, marks are sampled
deterministically: within each lane, messages are ordered by (ts, evidence_id)
and an evenly spaced subset is kept whose size is proportional to the lane's
volume (``floor(n_lane * max_marks / n_all_lanes)``), so relative density
between lanes and over time is preserved and the same inputs always give the
same page. A thin band under each lane shows the binned density of *all*
matching messages, so sampling never hides activity. Both the page header and
the returned dict say when sampling happened.

Security. Snippets are untrusted agent output. They are masked with the
``Scrubber`` and truncated in Python before embedding; the data is embedded as
JSON in ``<script type="application/json">`` with ``<``, ``>`` and ``&``
escaped (so no content can close the tag); header strings go through
``html.escape``; the page's JS only ever writes data-derived strings with
``textContent`` or canvas ``fillText``, never ``innerHTML``.
"""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from swarm_mcp.scope import db
from swarm_mcp.toolkit import Scrubber, ToolInputError

DEFAULT_MAX_MARKS = 30_000
MAX_SNIPPET_CHARS = 2_000
PALETTE_SLOTS = 8  # categorical slots; further channels fold into a neutral "other" colour
DENSITY_BINS = 600  # target max bins for the all-message density band
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
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (
        s.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


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
    """Common evidence-id prefix up to the last ':' (e.g. 'village:chat:'), factored out of the page."""
    if not ids:
        return ""
    p = os.path.commonprefix(ids)
    return p[: p.rfind(":") + 1]


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
) -> dict[str, Any]:
    """Render the swimlane HTML to ``out_path`` and return a summary dict.

    ``since``/``until`` are already-parsed UTC strings (``toolkit.parse_time``),
    the window is [since, until). ``scrub=None`` means the default (enabled)
    ``Scrubber``; pass ``Scrubber(enabled=False)`` to disable masking.
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
                       count(*) FILTER (WHERE author_id NOT IN (SELECT agent_id FROM agents)) AS humans,
                       count(*) FILTER (WHERE ts IS NULL) AS undated,
                       count(DISTINCT author_id) FILTER (WHERE author_id IN (SELECT agent_id FROM agents)) AS n_agents,
                       epoch_ms(min(ts)) AS t_min, epoch_ms(max(ts)) AS t_max
                FROM messages WHERE {base}""",
                params,
            )
            or {}
        )
        total = int(agg.get("total") or 0)

        lane_rows = store.all(
            f"""SELECT author_id, count(*) AS n, epoch_ms(min(ts)) AS first_ms
                FROM messages WHERE {dated} AND author_id IN (SELECT agent_id FROM agents)
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
                    SELECT m.evidence_id, m.author_id, m.channel, epoch_ms(m.ts) AS t, m.content
                    FROM messages m
                    WHERE m.evidence_id IN (
                        SELECT evidence_id FROM q WHERE (rn * quota) // n > ((rn - 1) * quota) // n
                    )
                    ORDER BY m.author_id, m.ts, m.evidence_id"""
                rows = store.con.execute(sql, lane_params + [max_marks, lane_total]).fetchall()
            else:
                sql = f"""SELECT evidence_id, author_id, channel, epoch_ms(ts) AS t, content
                          FROM messages WHERE {lane_where} ORDER BY author_id, ts, evidence_id"""
                rows = store.con.execute(sql, lane_params).fetchall()

            t_lo = min(int(r["first_ms"]) for r in lane_rows)
            t_hi = int(store.scalar(f"SELECT epoch_ms(max(ts)) FROM messages WHERE {lane_where}", lane_params))
            span = max(t_hi - t_lo, 1)
            bin_ms = next((b for b in _NICE_BINS_MS if span / b <= DENSITY_BINS), _NICE_BINS_MS[-1])
            bin_base = (t_lo // bin_ms) * bin_ms
            dens_rows = store.all(
                f"""SELECT author_id, (epoch_ms(ts) - CAST(? AS BIGINT)) // CAST(? AS BIGINT) AS b, count(*) AS c
                    FROM messages WHERE {lane_where} GROUP BY 1, 2 ORDER BY 1, 2""",
                [bin_base, bin_ms] + lane_params,
            )
            chan_counts = store.all(
                f"""SELECT coalesce(channel, '(none)') AS ch, count(*) AS n
                    FROM messages WHERE {lane_where} GROUP BY 1 ORDER BY n DESC, ch""",
                lane_params,
            )

    # ---- assemble the compact page payload -------------------------------------------------
    lane_index = {aid: i for i, aid in enumerate(lane_ids)}
    rows.sort(key=lambda r: (lane_index[r[1]], r[3], r[0]))  # lane order, then time
    chan_names = [c["ch"] for c in chan_counts]
    chan_index = {c: i for i, c in enumerate(chan_names)}
    ids = [r[0] for r in rows]
    prefix = _id_prefix(ids)
    t0 = (min((int(r[3]) for r in rows), default=0) // 1000) * 1000

    lanes: list[dict[str, Any]] = [
        {
            "name": db.label_for(r["author_id"], names),
            "id": r["author_id"],
            "n": int(r["n"]),
            "first": int(r["first_ms"]),
            "a": 0,
            "b": 0,
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
        dens[lane_index[d["author_id"]]] += [int(d["b"]), int(d["c"])]

    marks = len(rows)
    payload = {
        "v": 1,
        "t0": t0,
        "t": [int(r[3]) // 1000 - t0 // 1000 for r in rows],  # seconds since t0
        "c": [chan_index[r[2] if r[2] is not None else "(none)"] for r in rows],
        "len": [len(r[4] or "") for r in rows],
        "idp": prefix,
        "id": [i[len(prefix) :] for i in ids],
        "s": [_snippet(r[4], scrub, snippet_chars) for r in rows],
        "lanes": lanes,
        "dens": {"bin": bin_ms, "base": bin_base, "lanes": dens},
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
        "undated": int(agg.get("undated") or 0),
        "n_agents": n_agents,
        "lane_total": lane_total,
        "other_msgs": max(other_msgs, 0),
        "marks": marks,
        "sampled": sampled,
        "max_marks": max_marks,
        "snippet_chars": snippet_chars,
        "range": rng,
        "db": Path(db_path).name,
    }
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
    }


# --------------------------------------------------------------------------- page


def _fmt(n: int) -> str:
    return f"{n:,}"


def _header(meta: dict[str, Any]) -> str:
    e = html.escape
    filters = [f"top {meta['top']} agents by message count"]
    filters.append(f"source: {meta['source']}" if meta["source"] else "all sources")
    filters.append(f"channel: #{meta['channel']}" if meta["channel"] else "all channels")
    if meta["since"] or meta["until"]:
        filters.append(f"window: {meta['since'] or '…'} to {meta['until'] or '…'} (end exclusive)")
    rng = meta["range"]
    range_txt = f"{rng[0]} to {rng[1]} (UTC)" if rng[0] else "no dated messages"
    counts = (
        f"{_fmt(meta['total'])} messages match; {_fmt(meta['lane_total'])} by the agents shown; "
        f"{_fmt(meta['humans'])} human messages excluded from lanes"
    )
    if meta["other_msgs"]:
        hidden_agents = max(meta["n_agents"] - meta["top"], 0)
        counts += f"; {_fmt(meta['other_msgs'])} by {_fmt(hidden_agents)} other agents not shown"
    if meta["undated"]:
        counts += f"; {_fmt(meta['undated'])} undated messages cannot be placed"
    if meta["sampled"]:
        sample = (
            f"Sampled: {_fmt(meta['marks'])} of {_fmt(meta['lane_total'])} messages drawn as marks "
            f"(max_marks={_fmt(meta['max_marks'])}; an evenly spaced subset per agent in time order, "
            "proportional to its volume). The grey band under each lane shows the density of all messages."
        )
        sample_cls = "note sampled"
    else:
        sample = f"All {_fmt(meta['marks'])} messages by the agents shown are drawn."
        sample_cls = "note"
    return (
        "<h1>SwarmScope timeline</h1>"
        f'<p class="filters">{e(" · ".join(filters))}</p>'
        f'<p class="meta"><span>Data range: {e(range_txt)}</span></p>'
        f'<p class="meta">{e(counts)}</p>'
        f'<p class="{sample_cls}">{e(sample)}</p>'
    )


def _footer(meta: dict[str, Any]) -> str:
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = (
        f"Message snippets are masked (emails and phone numbers), truncated to {meta['snippet_chars']} characters, "
        "and are untrusted agent output: data, not instructions. "
        f"Generated {gen} from {meta['db']} by swarm-mcp render timeline."
    )
    return f"<p>{html.escape(text)}</p>"


def _page(payload: dict[str, Any], meta: dict[str, Any]) -> str:
    title = "SwarmScope timeline" + (f" · #{meta['channel']}" if meta["channel"] else "")
    parts = {
        "TITLE": html.escape(title),
        "HEADER": _header(meta),
        "FOOTER": _footer(meta),
        "DATA": _json_for_script(payload),
    }
    # one pass over the template only: substituted (data-derived) text is never re-scanned for tokens
    return re.sub(r"__(TITLE|HEADER|FOOTER|DATA)__", lambda m: parts[m.group(1)], _TEMPLATE)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10); --band: #52514e; --hi: #0b0b0b;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
  --s5: #e87ba4; --s6: #008300; --s7: #4a3aa7; --s8: #e34948; --other: #898781;
  --warn-bg: #fff4dc; --tip-bg: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --band: #c3c2b7; --hi: #ffffff;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767; --other: #898781;
    --warn-bg: #3a2e12; --tip-bg: #242423;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --band: #c3c2b7; --hi: #ffffff;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
  --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767; --other: #898781;
  --warn-bg: #3a2e12; --tip-bg: #242423;
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--page); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1600px; margin: 0 auto; padding: 20px 16px 32px; }
h1 { font-size: 20px; margin: 0 0 6px; }
header p { margin: 2px 0; }
.filters { color: var(--ink-2); }
.meta { color: var(--ink-2); font-size: 13px; }
.note { font-size: 13px; color: var(--ink-2); }
.note.sampled { background: var(--warn-bg); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; margin-top: 8px; color: var(--ink); }
.controls { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; margin: 14px 0 8px; font-size: 13px; color: var(--ink-2); }
.controls button, .controls select { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 3px 9px; cursor: pointer; }
.controls button:hover { border-color: var(--axis); }
#view { font-variant-numeric: tabular-nums; }
#status { color: var(--ink); min-height: 1em; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 6px; margin: 4px 0 10px; }
.legend button { display: inline-flex; align-items: center; gap: 6px; font: 12px system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink); background: var(--surface); border: 1px solid var(--border); border-radius: 999px; padding: 2px 9px 2px 6px; cursor: pointer; }
.legend button.off { opacity: 0.45; text-decoration: line-through; }
.legend .sw { width: 10px; height: 10px; border-radius: 2px; flex: none; }
.legend .n { color: var(--muted); font-variant-numeric: tabular-nums; }
.legend .band-key { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-2); padding: 2px 6px; }
.legend .band-key i { display: inline-block; width: 18px; height: 4px; background: var(--band); opacity: .6; border-radius: 1px; }
#plot { position: relative; width: 100%; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; touch-action: none; }
#plot canvas { position: absolute; left: 0; top: 0; display: block; }
#ov { cursor: crosshair; }
#ov.drag { cursor: grabbing; }
#empty { padding: 28px; color: var(--ink-2); }
#tip { position: fixed; z-index: 10; pointer-events: none; max-width: min(420px, calc(100vw - 32px)); background: var(--tip-bg); color: var(--ink); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 4px 18px rgba(0,0,0,.18); padding: 8px 10px; font-size: 12px; line-height: 1.4; display: none; }
#tip .id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: var(--ink-2); word-break: break-all; }
#tip .who { font-weight: 600; margin-top: 2px; }
#tip .when { color: var(--ink-2); font-variant-numeric: tabular-nums; }
#tip .snip { margin-top: 6px; white-space: pre-wrap; word-break: break-word; border-left: 2px solid var(--axis); padding-left: 7px; }
#tip .foot { margin-top: 6px; color: var(--muted); font-size: 11px; }
footer { margin-top: 16px; font-size: 12px; color: var(--muted); }
</style>
</head>
<body>
<main>
<header>__HEADER__</header>
<div class="controls">
  <button type="button" id="zin" title="Zoom in">Zoom +</button>
  <button type="button" id="zout" title="Zoom out">Zoom −</button>
  <button type="button" id="reset" title="Show the full range (or double-click the plot)">Reset</button>
  <label>Lanes <select id="sort"><option value="n">by message count</option><option value="first">by first message</option></select></label>
  <span id="view"></span>
  <span id="status" aria-live="polite"></span>
</div>
<div class="legend" id="legend"></div>
<div id="plot"><canvas id="cv"></canvas><canvas id="ov"></canvas></div>
<p class="meta">Scroll to zoom the time axis, drag to pan, double-click to reset. Hover a mark for details; click it to copy its evidence id. Click a channel to hide or show it.</p>
<footer>__FOOTER__</footer>
</main>
<div id="tip" role="tooltip"><div class="id"></div><div class="who"></div><div class="when"></div><div class="snip"></div><div class="foot"></div></div>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  'use strict';
  var D = JSON.parse(document.getElementById('data').textContent);
  var N = D.t.length, lanes = D.lanes, chans = D.channels;
  var T = new Float64Array(N);
  for (var i = 0; i < N; i++) T[i] = D.t0 + D.t[i] * 1000;
  var hidden = new Uint8Array(chans.length);
  var order = lanes.map(function (_, i) { return i; });
  var AXIS_H = 30, LANE_H = 30, PAD_R = 14, BAND_H = 4;
  var plot = document.getElementById('plot');
  var cv = document.getElementById('cv'), ov = document.getElementById('ov');
  var ctx = cv.getContext('2d'), octx = ov.getContext('2d');
  var tip = document.getElementById('tip'), statusEl = document.getElementById('status');
  var viewEl = document.getElementById('view');
  var W = 0, H = 0, dpr = 1, LABEL_W = 210;
  var span0 = Math.max(D.end - D.start, 60000), pad = span0 * 0.01;
  var FULL0 = D.start - pad, FULL1 = D.end + pad;
  var v0 = FULL0, v1 = FULL1;
  var C = {}, chCol = [];
  var hover = -1, hoverLane = -1;
  var densMax = (D.dens.lanes || []).map(function (a) { var m = 1; for (var j = 1; j < a.length; j += 2) if (a[j] > m) m = a[j]; return m; });

  if (!lanes.length) {
    plot.textContent = '';
    var e = document.createElement('div'); e.id = 'empty';
    e.textContent = 'No agent messages match these filters.';
    plot.appendChild(e); plot.style.height = 'auto';
    return;
  }

  function readColors() {
    var cs = getComputedStyle(document.documentElement);
    function g(n) { return cs.getPropertyValue(n).trim(); }
    C = { ink: g('--ink'), ink2: g('--ink-2'), muted: g('--muted'), grid: g('--grid'), axis: g('--axis'),
          surface: g('--surface'), band: g('--band'), hi: g('--hi'), other: g('--other') };
    var s = []; for (var k = 1; k <= 8; k++) s.push(g('--s' + k));
    chCol = chans.map(function (c) { return c.slot >= 0 ? s[c.slot] : C.other; });
  }

  function lowerBound(a, lo, hi, x) {
    while (lo < hi) { var m = (lo + hi) >> 1; if (a[m] < x) lo = m + 1; else hi = m; }
    return lo;
  }
  function fmtN(n) { return n.toLocaleString('en-US'); }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function isoS(ms) { return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z'); }

  // ---- axis ticks (UTC) ----------------------------------------------------------------
  var STEPS = [1e3, 5e3, 15e3, 3e4, 6e4, 3e5, 9e5, 18e5, 36e5, 108e5, 216e5, 432e5, 864e5, 1728e5, 6048e5, 12096e5];
  var MONTHS = [1, 2, 3, 6, 12, 24, 60];
  function ticks(a, b, pw) {
    var target = Math.max(2, Math.floor(pw / 115)), raw = (b - a) / target, out = [], step, t, d;
    for (var k = 0; k < STEPS.length; k++) {
      step = STEPS[k];
      if (step >= raw) {
        for (t = Math.ceil(a / step) * step; t <= b; t += step) out.push(t);
        return { list: out, fmt: function (t) {
          d = new Date(t);
          var day = d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate());
          if (step >= 864e5) return day;
          var hm = pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes());
          if (step >= 6e4) return (d.getUTCHours() === 0 && d.getUTCMinutes() === 0) ? day : hm;
          return hm + ':' + pad2(d.getUTCSeconds());
        } };
      }
    }
    var rawM = raw / (30.44 * 864e5), m = MONTHS[MONTHS.length - 1];
    for (k = 0; k < MONTHS.length; k++) if (MONTHS[k] >= rawM) { m = MONTHS[k]; break; }
    d = new Date(a);
    var y = d.getUTCFullYear(), mo = d.getUTCMonth();
    for (var guard = 0; guard < 2000; guard++) {
      t = Date.UTC(y, mo, 1);
      if (t > b) break;
      if (t >= a && ((y * 12 + mo) % m === 0)) out.push(t);
      mo++; if (mo === 12) { mo = 0; y++; }
    }
    return { list: out, fmt: function (t) {
      var dd = new Date(t);
      return m >= 12 ? String(dd.getUTCFullYear()) : dd.getUTCFullYear() + '-' + pad2(dd.getUTCMonth() + 1);
    } };
  }

  // ---- layout & drawing -----------------------------------------------------------------
  function resize() {
    dpr = window.devicePixelRatio || 1;
    W = Math.max(320, plot.clientWidth);
    H = AXIS_H + lanes.length * LANE_H + 6;
    LABEL_W = Math.min(210, Math.round(W * 0.34));
    plot.style.height = H + 'px';
    [cv, ov].forEach(function (c) {
      c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
      c.style.width = W + 'px'; c.style.height = H + 'px';
    });
    draw(); drawHover();
  }
  function plotW() { return Math.max(10, W - LABEL_W - PAD_R); }
  function xOf(t) { return LABEL_W + (t - v0) * plotW() / (v1 - v0); }

  function fitText(s, maxW) {
    if (ctx.measureText(s).width <= maxW) return s;
    while (s.length > 1 && ctx.measureText(s + '…').width > maxW) s = s.slice(0, -1);
    return s + '…';
  }

  function draw() {
    var pw = plotW(), k = pw / (v1 - v0), p, li, L, y, j;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalAlpha = 1;
    ctx.fillStyle = C.surface; ctx.fillRect(0, 0, W, H);
    ctx.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
    ctx.textBaseline = 'middle';
    // gridlines + tick labels
    var tk = ticks(v0, v1, pw);
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1; ctx.fillStyle = C.muted; ctx.textAlign = 'center';
    for (j = 0; j < tk.list.length; j++) {
      var x = Math.round(LABEL_W + (tk.list[j] - v0) * k) + 0.5;
      if (x < LABEL_W || x > W - PAD_R) continue;
      ctx.beginPath(); ctx.moveTo(x, AXIS_H - 6); ctx.lineTo(x, H); ctx.stroke();
      ctx.fillText(tk.fmt(tk.list[j]), Math.min(Math.max(x, LABEL_W + 30), W - PAD_R - 30), AXIS_H / 2 - 1);
    }
    ctx.strokeStyle = C.axis;
    ctx.beginPath(); ctx.moveTo(LABEL_W, AXIS_H - 0.5); ctx.lineTo(W - PAD_R, AXIS_H - 0.5); ctx.stroke();
    // lanes
    ctx.save();
    ctx.beginPath(); ctx.rect(LABEL_W, AXIS_H, pw, H - AXIS_H); ctx.clip();
    var markH = LANE_H - BAND_H - 10;
    for (p = 0; p < order.length; p++) {
      li = order[p]; L = lanes[li]; y = AXIS_H + p * LANE_H;
      ctx.globalAlpha = 1; ctx.strokeStyle = C.grid;
      ctx.beginPath(); ctx.moveTo(LABEL_W, y + LANE_H - 0.5); ctx.lineTo(W, y + LANE_H - 0.5); ctx.stroke();
      // density band: all messages (not sampled), binned
      var dl = D.dens.lanes[li] || [], dm = densMax[li], bw = D.dens.bin * k;
      ctx.fillStyle = C.band;
      for (j = 0; j < dl.length; j += 2) {
        var bx = LABEL_W + (D.dens.base + dl[j] * D.dens.bin - v0) * k;
        if (bx + bw < LABEL_W || bx > W) continue;
        ctx.globalAlpha = 0.12 + 0.68 * Math.sqrt(dl[j + 1] / dm);
        ctx.fillRect(bx, y + LANE_H - BAND_H - 3, Math.max(1, bw), BAND_H);
      }
      // marks: skip duplicates that land on the same pixel with the same colour
      ctx.globalAlpha = 0.85;
      var lo = lowerBound(T, L.a, L.b, v0 - 3 / k), hi = lowerBound(T, lo, L.b, v1 + 3 / k);
      var lastX = -1e9, lastC = -1;
      for (j = lo; j < hi; j++) {
        var c = D.c[j];
        if (hidden[c]) continue;
        var mx = Math.floor(LABEL_W + (T[j] - v0) * k);
        if (mx === lastX && c === lastC) continue;
        if (c !== lastC) ctx.fillStyle = chCol[c];
        lastX = mx; lastC = c;
        ctx.fillRect(mx - 1, y + 4, 2, markH);
      }
    }
    ctx.restore();
    // lane labels
    ctx.globalAlpha = 1;
    ctx.fillStyle = C.surface; ctx.fillRect(0, AXIS_H, LABEL_W - 1, H - AXIS_H);
    ctx.strokeStyle = C.axis;
    ctx.beginPath(); ctx.moveTo(LABEL_W - 0.5, AXIS_H); ctx.lineTo(LABEL_W - 0.5, H); ctx.stroke();
    for (p = 0; p < order.length; p++) {
      L = lanes[order[p]]; y = AXIS_H + p * LANE_H + LANE_H / 2;
      ctx.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
      ctx.textAlign = 'right'; ctx.fillStyle = C.muted;
      var cnt = fmtN(L.n);
      ctx.fillText(cnt, LABEL_W - 8, y);
      var cw = ctx.measureText(cnt).width;
      ctx.font = '600 12px system-ui, -apple-system, "Segoe UI", sans-serif';
      ctx.textAlign = 'left'; ctx.fillStyle = C.ink;
      ctx.fillText(fitText(L.name, LABEL_W - 24 - cw), 8, y);
    }
    viewEl.textContent = 'View: ' + isoS(Math.max(v0, D.start)).slice(0, 16).replace('T', ' ') + ' → ' +
      isoS(Math.min(v1, D.end)).slice(0, 16).replace('T', ' ') + ' UTC';
  }

  function drawHover() {
    octx.setTransform(dpr, 0, 0, dpr, 0, 0);
    octx.clearRect(0, 0, W, H);
    if (hover < 0) return;
    var p = order.indexOf(hoverLane); if (p < 0) return;
    var x = Math.floor(xOf(T[hover])), y = AXIS_H + p * LANE_H;
    octx.strokeStyle = C.hi; octx.lineWidth = 1.5;
    octx.strokeRect(x - 3.5, y + 1.5, 7, LANE_H - BAND_H - 5);
  }

  // ---- hit testing & tooltip ------------------------------------------------------------
  function hit(mx, my) {
    if (mx < LABEL_W || mx > W - PAD_R || my < AXIS_H) return null;
    var p = Math.floor((my - AXIS_H) / LANE_H);
    if (p < 0 || p >= order.length) return null;
    var li = order[p], L = lanes[li], k = plotW() / (v1 - v0);
    var t = v0 + (mx - LABEL_W) / k, r = 5 / k, best = -1, bd = Infinity, j;
    var i0 = lowerBound(T, L.a, L.b, t);
    for (j = i0; j < L.b && T[j] <= t + r; j++) if (!hidden[D.c[j]]) { bd = T[j] - t; best = j; break; }
    for (j = i0 - 1; j >= L.a && T[j] >= t - r; j--) if (!hidden[D.c[j]]) { if (t - T[j] < bd) best = j; break; }
    return best < 0 ? null : { i: best, lane: li };
  }

  var tipParts = tip.children;
  function showTip(h, cx, cy) {
    var i = h.i, L = lanes[h.lane], ch = chans[D.c[i]];
    tipParts[0].textContent = D.idp + D.id[i];
    tipParts[1].textContent = L.name + '  ·  #' + ch.name;
    tipParts[2].textContent = isoS(T[i]);
    tipParts[3].textContent = D.s[i] || '(no snippet)';
    tipParts[3].style.display = D.s[i] ? '' : 'none';
    tipParts[4].textContent = fmtN(D.len[i]) + ' chars · masked, truncated, untrusted agent output · click to copy id';
    tip.style.display = 'block';
    var tw = tip.offsetWidth, th = tip.offsetHeight;
    var x = cx + 14, y = cy + 14;
    if (x + tw > window.innerWidth - 8) x = Math.max(8, cx - tw - 14);
    if (y + th > window.innerHeight - 8) y = Math.max(8, cy - th - 14);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  function hideTip() { tip.style.display = 'none'; }

  function copy(text) {
    function done(ok) { statusEl.textContent = ok ? 'Copied ' + text : 'Copy blocked; evidence id: ' + text; }
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      var ok = false; try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(ta); done(ok);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, fallback);
    } else fallback();
  }

  // ---- interaction ----------------------------------------------------------------------
  var MIN_SPAN = 10e3, MAX_SPAN = (FULL1 - FULL0) * 1.5;
  function clampView() {
    var s = v1 - v0;
    if (s < MIN_SPAN) { var m = (v0 + v1) / 2; v0 = m - MIN_SPAN / 2; v1 = m + MIN_SPAN / 2; s = MIN_SPAN; }
    if (s > MAX_SPAN) { v0 = FULL0 - (MAX_SPAN - (FULL1 - FULL0)) / 2; v1 = v0 + MAX_SPAN; return; }
    var lo = FULL0 - s * 0.5, hi = FULL1 + s * 0.5;
    if (v0 < lo) { v0 = lo; v1 = lo + s; }
    if (v1 > hi) { v1 = hi; v0 = hi - s; }
  }
  var raf = 0;
  function redraw() { if (!raf) raf = requestAnimationFrame(function () { raf = 0; draw(); drawHover(); }); }
  function zoomAt(px, f) {
    var t = v0 + (px - LABEL_W) * (v1 - v0) / plotW();
    v0 = t - (t - v0) * f; v1 = t + (v1 - t) * f; clampView(); redraw();
  }
  function local(ev) { var r = ov.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; }

  ov.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    var pt = local(ev), dy = ev.deltaMode === 1 ? ev.deltaY * 16 : ev.deltaY;
    if (Math.abs(ev.deltaX) > Math.abs(dy)) {  // horizontal trackpad scroll pans
      var sh = ev.deltaX * (v1 - v0) / plotW(); v0 += sh; v1 += sh; clampView(); redraw(); return;
    }
    zoomAt(Math.max(LABEL_W, pt[0]), Math.exp(dy * 0.0015));
  }, { passive: false });

  var drag = null;
  ov.addEventListener('pointerdown', function (ev) {
    if (ev.button !== 0) return;
    var pt = local(ev);
    drag = { x: pt[0], y: pt[1], v0: v0, v1: v1, moved: false };
    ov.setPointerCapture(ev.pointerId);
  });
  ov.addEventListener('pointermove', function (ev) {
    var pt = local(ev);
    if (drag) {
      var dx = pt[0] - drag.x;
      if (!drag.moved && Math.abs(dx) > 3) { drag.moved = true; ov.classList.add('drag'); hideTip(); hover = -1; }
      if (drag.moved) {
        var sh = dx * (drag.v1 - drag.v0) / plotW();
        v0 = drag.v0 - sh; v1 = drag.v1 - sh; clampView(); redraw();
      }
      return;
    }
    var h = hit(pt[0], pt[1]);
    if (h) { hover = h.i; hoverLane = h.lane; showTip(h, ev.clientX, ev.clientY); }
    else { hover = -1; hideTip(); }
    drawHover();
  });
  ov.addEventListener('pointerup', function (ev) {
    if (!drag) return;
    var moved = drag.moved; drag = null; ov.classList.remove('drag');
    if (!moved) {
      var pt = local(ev), h = hit(pt[0], pt[1]);
      if (h) copy(D.idp + D.id[h.i]);
    }
  });
  ov.addEventListener('pointercancel', function () { drag = null; ov.classList.remove('drag'); });
  ov.addEventListener('pointerleave', function () { if (!drag) { hover = -1; hideTip(); drawHover(); } });
  ov.addEventListener('dblclick', function () { v0 = FULL0; v1 = FULL1; redraw(); });
  document.getElementById('zin').onclick = function () { zoomAt(LABEL_W + plotW() / 2, 0.5); };
  document.getElementById('zout').onclick = function () { zoomAt(LABEL_W + plotW() / 2, 2); };
  document.getElementById('reset').onclick = function () { v0 = FULL0; v1 = FULL1; redraw(); };
  document.getElementById('sort').onchange = function (ev) {
    var by = ev.target.value;
    order = lanes.map(function (_, i) { return i; });
    if (by === 'first') order.sort(function (a, b) { return lanes[a].first - lanes[b].first; });
    hover = -1; hideTip(); redraw();
  };

  // ---- legend ---------------------------------------------------------------------------
  var legend = document.getElementById('legend');
  function buildLegend() {
    legend.textContent = '';
    chans.forEach(function (c, i) {
      var b = document.createElement('button');
      b.type = 'button'; b.title = 'Hide or show #' + c.name;
      if (hidden[i]) b.className = 'off';
      var sw = document.createElement('span'); sw.className = 'sw'; sw.style.background = chCol[i];
      var name = document.createElement('span'); name.textContent = '#' + c.name;
      var n = document.createElement('span'); n.className = 'n'; n.textContent = fmtN(c.n);
      b.appendChild(sw); b.appendChild(name); b.appendChild(n);
      b.onclick = function () { hidden[i] = hidden[i] ? 0 : 1; b.classList.toggle('off', !!hidden[i]); hover = -1; hideTip(); redraw(); };
      legend.appendChild(b);
    });
    var key = document.createElement('span'); key.className = 'band-key';
    key.appendChild(document.createElement('i'));
    key.appendChild(document.createTextNode('density of all messages (binned)'));
    legend.appendChild(key);
  }

  readColors(); buildLegend(); resize();
  if (window.ResizeObserver) new ResizeObserver(function () { resize(); }).observe(plot);
  else window.addEventListener('resize', resize);
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    var onScheme = function () { readColors(); buildLegend(); draw(); drawHover(); };
    if (mq.addEventListener) mq.addEventListener('change', onScheme); else if (mq.addListener) mq.addListener(onScheme);
  }
})();
</script>
</body>
</html>
"""
