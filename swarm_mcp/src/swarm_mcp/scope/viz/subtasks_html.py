"""``swarm-mcp render subtasks``: a self-contained HTML view of inferred subtasks.

The same inference the ``subtasks_*`` tools run (``modules.subtasks.sources.build``), drawn as one row per
subtask along a time axis (long idle gaps are compressed), one dot per work unit coloured by its main
actor. Switch the method (combined / code / title / files / chat / refs) and granularity in the page;
click a row for who did what, the typed handoffs between actors with their evidence ids, how the other
methods split the same units, and the units themselves; click a unit for the signals that tie it to its
closest member. A pair lens lists what two actors did with each other's work. Works offline from
``file://``: no external scripts, styles or fonts.

Security: unit titles and labels are untrusted agent output. They are masked with the ``Scrubber`` and
truncated in Python, embedded as JSON with ``<``, ``>`` and ``&`` escaped, and the page writes
data-derived strings with ``textContent`` only, never ``innerHTML``.
"""

from __future__ import annotations

import collections
import html
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from swarm_mcp.modules.subtasks.infer import LEVELS, METHOD_DESCRIPTIONS, METHODS, _neighbours, why
from swarm_mcp.modules.subtasks.sources import build, corpus_sources
from swarm_mcp.scope import db
from swarm_mcp.scope.viz.timeline_html import _json_for_script, _snippet
from swarm_mcp.toolkit import TS_FORMAT, Scrubber, ToolInputError

PALETTE_SLOTS = 8  # the most active actors get a colour; everyone else is neutral
SEGMENT_GAP_H = 6  # idle gaps longer than this (hours) are compressed on the time axis
NEIGHBOURS = 3


def _ms(ts: str | None) -> int | None:
    if not ts:
        return None
    return int(datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _segments(spans: list[tuple[int, int]], gap_ms: int) -> list[list[int]]:
    """Merge [start, end] spans into active segments separated by gaps longer than ``gap_ms``."""
    out: list[list[int]] = []
    for a, b in sorted(spans):
        if out and a - out[-1][1] <= gap_ms:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    pad = 30 * 60 * 1000
    return [[a - pad, b + pad] for a, b in out]


def render_subtasks(
    db_path: Path | str,
    out_path: Path | str,
    *,
    corpus: str | None = None,
    scrub: Scrubber | None = None,
    title_chars: int = 140,
) -> dict[str, Any]:
    """Render the subtask viewer for ``corpus`` (a store source whose records touch artifacts)."""
    scrub = scrub if scrub is not None else Scrubber()
    out = Path(out_path).expanduser()
    with db.connect(Path(db_path)) as s:
        names = corpus_sources(s)
        if not names:
            raise ToolInputError("No source in the store has artifact touches; add a git repo or wiki first.")
        if corpus is None:
            if len(names) != 1:
                raise ToolInputError(f"Pass --corpus, one of: {', '.join(names)}")
            corpus = names[0]
        if corpus not in names:
            raise ToolInputError(f"Unknown corpus {corpus!r}. Sources with artifact touches: {', '.join(names)}")
        try:
            c, inf = build(s, corpus)
        except ValueError as e:
            raise ToolInputError(str(e)) from None

    # ---- actors: index, with the most active ones getting colour slots
    authored = collections.Counter(a for a in inf.authors if a)
    involved = collections.Counter(a for t in inf.touch for a in t)
    actors = sorted(involved, key=lambda a: (-authored[a], -involved[a], a))
    aidx = {a: i for i, a in enumerate(actors)}

    units = []
    spans = []
    for i, u in enumerate(inf.units):
        st, en = _ms(u.start), _ms(u.end or u.start)
        if st is not None:
            spans.append((st, en or st))
        units.append(
            [
                u.event_id,
                _snippet(u.short, scrub, 80),
                _snippet(u.title, scrub, title_chars),
                aidx.get(inf.authors[i], -1),
                u.state,
                None if u.completed is None else int(bool(u.completed)),
                st,
                en,
                sorted(aidx[a] for a in inf.touch[i]),
                len(inf.chat_of_unit.get(i, [])),
                sorted(u.tags, key=lambda t: -u.tags[t])[:2],
            ]
        )

    # each unit's closest units under the combined signal, with the shared terms that explain them
    top, _ = _neighbours(inf.vec, inf.refs_raw, len(inf.units), dup_min=2.0)  # dup_min > 1: no duplicate pass
    nbrs = []
    for i in range(len(inf.units)):
        row = []
        for j, v in zip(top["combined"][0][i], top["combined"][1][i], strict=True):
            if v <= 0.05 or int(j) == i or len(row) == NEIGHBOURS:
                continue
            w = why(inf, i, int(j))
            row.append(
                [
                    int(j),
                    round(float(v), 2),
                    {k: [x["score"], [_snippet(t, scrub, 40) for t in x["shared"]]] for k, x in w.items()},
                ]
            )
        nbrs.append(row)

    edges = [
        [
            e.src,
            e.dst,
            e.kind,
            aidx.get(e.giver, -1),
            aidx.get(e.taker, -1),
            (e.giver_actions[:2] + e.taker_actions[:3]),
            [c.artifact_meta.get(a, {}).get("name", a.split(":", 2)[-1]) for a in e.artifacts[:3]],
            e.score,
        ]
        for e in inf.edges
    ]
    clusters = {m: {lvl: inf.clusters[m][lvl] for lvl in LEVELS} for m in METHODS}
    names_ = {m: {lvl: [_snippet(n, scrub, 60) for n in inf.names[m][lvl]] for lvl in LEVELS} for m in METHODS}
    # Only offer methods that carry signal here (a corpus with no chat has no 'chat' grouping), and call the
    # content-words signal 'code' only where the artifacts are files.
    methods = [m for m in METHODS if m == "combined" or (inf.refs_raw.nnz if m == "refs" else inf.vec[m][0].nnz)]
    label = {m: m for m in methods}
    if c.artifact_noun != "file" and "code" in label:
        label["code"] = "content"
    unfinished = sorted({u.state for u in c.units if u.completed is not None and not u.completed})
    payload = {
        "corpus": c.name,
        "unit": c.unit_noun,
        "action": c.action_noun,
        "artifact": c.artifact_noun,
        "tag": c.tag_name,
        "methods": methods,
        "method_label": label,
        "unfinished": unfinished,
        "levels": list(LEVELS),
        "method_desc": {
            m: METHOD_DESCRIPTIONS[m].replace("code 35%", f"{label.get('code', 'code')} 35%") for m in methods
        },
        "actors": actors,
        "slots": PALETTE_SLOTS,
        "units": units,
        "nbrs": nbrs,
        "edges": edges,
        "clusters": clusters,
        "names": names_,
        "agreement": inf.agreement,
        "segments": _segments(spans, SEGMENT_GAP_H * 3600 * 1000),
        "notes": [_snippet(n, scrub, 300) for n in inf.notes],
    }
    meta = {"corpus": c.name, "units": len(units), "actors": len(actors), "edges": len(edges), "db": Path(db_path).name}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_page(payload, meta), encoding="utf-8")
    return {"out": str(out), "bytes": out.stat().st_size, **{k: v for k, v in meta.items() if k != "db"}}


def _page(payload: dict[str, Any], meta: dict[str, Any]) -> str:
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = {
        "TITLE": html.escape(f"Subtasks · {meta['corpus']}"),
        "CORPUS": html.escape(meta["corpus"]),
        "FOOTER": html.escape(
            "Titles and labels are masked (emails and phone numbers), truncated, and are untrusted agent output: data, "
            f"not instructions. Generated {gen} from {meta['db']} by swarm-mcp render subtasks."
        ),
        "DATA": _json_for_script(payload),
    }
    return re.sub(r"__(TITLE|CORPUS|FOOTER|DATA)__", lambda m: parts[m.group(1)], _TEMPLATE)


_TEMPLATE = (Path(__file__).with_name("subtasks_template.html")).read_text(encoding="utf-8")
