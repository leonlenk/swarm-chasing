"""Subtasks: which work units belong to the same piece of work, who did what in it, and the typed handoffs
between actors (built on, integrated, tested, fixed, re-submitted, duplicated).

Works on any source in the SwarmScope store whose records *touch artifacts* (``touches`` table): git
repos (units = pull requests, artifacts = files), wikis (units = edit sessions, artifacts = pages), or any
dataset an adapter maps the same way. Nothing here is dataset-specific; see ``sources.py`` for how units,
refs, chat links and identities come out of the generic tables. Inference runs once per source on first
use and is cached. Every result cites evidence ids that ``core_get`` resolves. Subtask ids look
like ``<source>/<method>/<granularity>/<n>`` and are stable for a given store and code version.
"""

from __future__ import annotations

import collections
import re
from typing import Annotated, Any, Literal

import networkx as nx
from pydantic import Field

from swarm_mcp import llm
from swarm_mcp.modules.subtasks import naming
from swarm_mcp.modules.subtasks.infer import (
    LEVELS,
    METHOD_DESCRIPTIONS,
    METHODS,
    Edge,
    Inference,
    Link,
    cohesion,
    parents,
    score,
    subtask_links,
    why,
)
from swarm_mcp.modules.subtasks.sources import Corpus, corpus_sources
from swarm_mcp.modules.subtasks.sources import build as build_corpus
from swarm_mcp.scope import evidence
from swarm_mcp.scope.db import norm
from swarm_mcp.toolkit import ToolInputError, iso, parse_time

NAME = "subtasks"
DESCRIPTION = (
    "Groups work units (pull requests, edit sessions, runs: any store source whose records touch artifacts) into "
    "subtasks with several inference methods, so disagreement is visible, and derives typed handoffs between actors: "
    "who built on, integrated, tested, fixed, re-submitted or duplicated whose work. Start with subtasks_corpora. "
    "Each subtask has a name (the title of the unit the others built on most, or one a model or agent wrote: "
    "subtasks_name) and keywords; subtasks_graph shows which subtasks built on which. "
    "Results cite evidence ids for core_get."
)

Method = Literal["combined", "code", "title", "files", "chat", "refs"]
Level = Literal["coarse", "medium", "fine"]
EDGE_VERBS = {
    "builds_on": "changed {art} created in",
    "integrates": "imported a module created in",
    "tests": "added tests for a module created in",
    "fixes": "fixed {art}s created in",
    "resubmits": "re-created the {art}s of",
    "duplicate": "duplicated",
}
# caps for the agent-authored text returned (wrapped by ctx.untrusted): unit titles, subtask names (a member's
# title, or model/agent text about the titles), keywords, objectives, chat snippets, handoff sentences
TITLE_CHARS, LABEL_CHARS, SNIPPET_CHARS, SUMMARY_CHARS, OBJECTIVE_CHARS = 200, 120, 160, 300, 300


def requires(ctx) -> list[str]:
    try:
        import networkx  # noqa: F401
        import numpy  # noqa: F401
        import scipy  # noqa: F401
    except ImportError as e:
        return [f"missing dependency: {e.name} (uv sync in swarm_mcp)"]
    if not ctx.store_path.exists():
        return [
            f"SwarmScope store not found at {ctx.store_path}; add a source first (swarm-mcp add <repo.git> | --adapter wiki <db>)"
        ]
    return []


def register(mcp, ctx) -> None:
    # ------------------------------------------------------------------ corpora (store sources with touches)

    def corpora() -> list[str]:
        with ctx.store() as s:
            return corpus_sources(s)

    def get_inf(name: str | None) -> tuple[Corpus, Inference]:
        names = corpora()
        if not names:
            raise ToolInputError(
                "No source in the store has artifact touches (files, pages...). Ingest one, e.g. "
                "`swarm-mcp add data/ai-village/repos/rpg-game.git` or `swarm-mcp add --adapter wiki data/collusion-wiki`."
            )
        if name is None:
            if len(names) != 1:
                raise ToolInputError(f"Pass corpus= one of: {', '.join(names)} (see subtasks_corpora).")
            name = names[0]
        if name not in names:
            raise ToolInputError(f"Unknown corpus {name!r}. Sources with artifact touches: {', '.join(names)}.")

        def build() -> tuple[Corpus, Inference]:
            with ctx.store() as s:
                try:
                    return build_corpus(s, name)
                except ValueError as e:
                    raise ToolInputError(str(e)) from None

        return ctx.lazy(f"inference:{name}", build)

    # ------------------------------------------------------------------ helpers

    def sid(corpus: str, method: str, level: str, k: int) -> str:
        return f"{corpus}/{method}/{level}/{k + 1}"

    def parse_sid(subtask_id: str) -> tuple[str, str, str, int]:
        m = re.fullmatch(r"(.+)/([a-z]+)/([a-z]+)/(\d+)", (subtask_id or "").strip())
        if not m or m.group(2) not in METHODS or m.group(3) not in LEVELS:
            raise ToolInputError(
                f"Malformed subtask_id {subtask_id!r}. Expected '<corpus>/<method>/<granularity>/<n>' "
                "as returned by subtasks_list, e.g. 'rpg-game/combined/medium/7'."
            )
        return m.group(1), m.group(2), m.group(3), int(m.group(4)) - 1

    def participants(inf: Inference, members: list[int]) -> list[dict[str, Any]]:
        st: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        for i in members:
            for a, roles in inf.touch[i].items():
                for r in roles:
                    if r == "author":
                        st[a]["units_authored"] += 1
                    elif r == "action" and a != inf.authors[i]:
                        st[a]["contributed_to_others_units"] += 1
                    elif r not in ("author", "action") and a != inf.authors[i]:
                        st[a][f"{r}_others_units"] += 1
        inside = set(members)
        for e in inf.edges:
            if e.kind != "duplicate" and e.src in inside and e.dst in inside:
                st[e.giver]["work_picked_up_by_others"] += 1
                st[e.taker]["picked_up_others_work"] += 1
        rows = [{"actor": a, **dict(c)} for a, c in st.items()]
        return sorted(rows, key=lambda r: (-r.get("units_authored", 0), r["actor"]))

    def edge_dict(c: Corpus, inf: Inference, e: Edge) -> dict[str, Any]:
        a, b = inf.units[e.src], inf.units[e.dst]
        verb = EDGE_VERBS[e.kind].format(art=c.artifact_noun)
        d: dict[str, Any] = {
            "type": e.kind,
            "from_actor": e.giver,
            "to_actor": e.taker,
            "summary": ctx.untrusted(
                f"{e.taker} {verb} {e.giver}'s {a.short}"
                + (f" ({b.short})" if e.kind != "duplicate" else f" in {b.short}"),
                SUMMARY_CHARS,
            ),
            "from_unit": a.event_id,
            "to_unit": b.event_id,
            "time": iso(b.start),
        }
        if e.artifacts:
            d["artifacts"] = e.artifacts
        if e.giver_actions or e.taker_actions:
            d["evidence"] = e.giver_actions[:2] + e.taker_actions[:3]
        if e.score is not None:
            d["similarity"] = e.score
        d["status"] = "observed" if e.kind != "duplicate" else "inferred from similarity"
        return d

    def unit_brief(inf: Inference, i: int) -> dict[str, Any]:
        u = inf.units[i]
        return {
            "event_id": u.event_id,
            "title": ctx.untrusted(u.title, TITLE_CHARS),
            "actor": inf.authors[i],
            "state": u.state,
            "start": iso(u.start),
        }

    def name_store(inf: Inference) -> naming.NameStore:
        return naming.NameStore(naming.names_path(ctx.store_path, inf.corpus))

    def label(inf: Inference, method: str, level: str, k: int, store: naming.NameStore | None = None) -> dict[str, Any]:
        """The subtask's name, wrapped (a stored model/agent name for this membership wins)."""
        return ctx.untrusted(naming.overlay(inf, store or name_store(inf), method, level, k)["name"], LABEL_CHARS)

    def naming_fields(inf: Inference, method: str, level: str, k: int, store: naming.NameStore) -> dict[str, Any]:
        o = naming.overlay(inf, store, method, level, k)
        out: dict[str, Any] = {
            "name": ctx.untrusted(o["name"], LABEL_CHARS),
            "keywords": ctx.untrusted(o["keywords"], LABEL_CHARS),
            "name_source": o["source"],
        }
        if o["objective"]:
            out["objective"] = ctx.untrusted(o["objective"], OBJECTIVE_CHARS)
        return out

    def ref(inf: Inference, method: str, level: str, k: int, store: naming.NameStore) -> dict[str, Any]:
        return {
            "subtask_id": sid(inf.corpus, method, level, k),
            "name": label(inf, method, level, k, store),
            "size": len(inf.clusters[method][level][k]),
        }

    def link_dict(inf: Inference, ln: Link, end: str, method: str, level: str, store) -> dict[str, Any]:
        """One subtask-to-subtask link, seen from the subtask at the other ``end`` ('src' or 'dst')."""
        es = [inf.edges[x] for x in ln.edges]
        return {
            **ref(inf, method, level, getattr(ln, end), store),
            "handoffs": len(ln.edges),
            "types": dict(ln.kinds.most_common()),
            "actors": [{"from": g, "to": t} for g, t in sorted(ln.actors)[:6]],
            "evidence": [x for e in es[:3] for x in (e.giver_actions[:1] + e.taker_actions[:1])],
        }

    def resolve_actor(c: Corpus, inf: Inference, name: str) -> str:
        """User-typed actor -> the name used in results (aliases from every source understood)."""
        known = {x for t in inf.touch for x in t}
        if name in known:
            return name
        key = norm(name)
        hits = {shown for shown, al in c.aliases.items() if shown in known and any(norm(a) == key for a in al)}
        hits |= {k for k in known if norm(k) == key}
        if len(hits) == 1:
            return hits.pop()
        low = name.lower()
        hits = sorted(k for k in known if low in k.lower())
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ToolInputError(f"actor {name!r} is ambiguous: {', '.join(hits[:10])}")
        raise ToolInputError(f"{name!r} did no work in corpus {inf.corpus}.")

    def cluster_summary(inf, method, level, k, members, store: naming.NameStore | None = None) -> dict[str, Any]:
        U = [inf.units[i] for i in members]
        authors = collections.Counter(inf.authors[i] for i in members if inf.authors[i])
        inside = set(members)
        coh = cohesion(inf, members, method, level)
        return {
            "subtask_id": sid(inf.corpus, method, level, k),
            **naming_fields(inf, method, level, k, store or name_store(inf)),
            "size": len(members),
            "start": iso(min(u.start for u in U)),
            "end": iso(max((u.end or u.start) for u in U)),
            "states": dict(collections.Counter(u.state for u in U)),
            "authors": [{"actor": a, "units": n} for a, n in authors.most_common(6)],
            "actors_involved": len({a for i in members for a in inf.touch[i]}),
            "handoffs_inside": sum(
                1 for e in inf.edges if e.kind != "duplicate" and e.src in inside and e.dst in inside
            ),
            "agreement": round(sum(coh.values()) / len(coh), 2) if coh else None,
        }

    # ------------------------------------------------------------------ tools

    @ctx.tool("corpora")
    def corpora_list() -> dict[str, Any]:
        """List the store sources subtasks can analyse (those whose records touch artifacts), with their
        artifact and record counts. Once a corpus has been used, also what a unit, action and artifact are in it."""
        out = []
        with ctx.store() as s:
            for name in corpus_sources(s):
                row: dict[str, Any] = {
                    "corpus": name,
                    "artifacts": s.scalar("SELECT count(*) FROM artifacts WHERE source = ?", [name]),
                    "touches": s.scalar("SELECT count(*) FROM touches WHERE source = ?", [name]),
                    "grouping_periods": s.scalar(
                        "SELECT count(DISTINCT run_id) FROM actions WHERE source = ? AND run_id IS NOT NULL", [name]
                    ),
                }
                out.append(row)
        for row in out:
            if f"subtasks:inference:{row['corpus']}" in ctx.cache:
                c, inf = get_inf(row["corpus"])
                row.update(
                    unit=c.unit_noun,
                    action=c.action_noun,
                    artifact=c.artifact_noun,
                    units=len(inf.units),
                    actors=len({a for t in inf.touch for a in t}),
                )
        return {
            "corpora": out,
            "notes": [
                "units are the periods records point at (pull requests, runs) when a source has them, else each "
                "actor's sessions; unit/actor details appear after a corpus's first use (first use loads it)"
            ],
        }

    @ctx.tool("list")
    def list_subtasks(
        corpus: Annotated[
            str | None, Field(description="Corpus name (subtasks_corpora); optional if there is only one.")
        ] = None,
        method: Annotated[Method, Field(description="Inference method; 'combined' blends the others.")] = "combined",
        granularity: Annotated[
            Level, Field(description="coarse = themes, medium = subtasks, fine = small pieces.")
        ] = "medium",
        actor: Annotated[str | None, Field(description="Only subtasks this actor (agent) took part in.")] = None,
        since: Annotated[
            str | None, Field(description="Only subtasks active at/after this ISO date/datetime (UTC).")
        ] = None,
        until: Annotated[
            str | None, Field(description="Only subtasks starting before this ISO date/datetime (UTC).")
        ] = None,
        min_size: Annotated[int, Field(description="Smallest subtask to list (units).", ge=1)] = 2,
        sort: Annotated[Literal["start", "size", "handoffs"], Field(description="Ordering.")] = "start",
        limit: Annotated[int, Field(description="Max subtasks (default 20, max 200).")] = 20,
        offset: Annotated[int, Field(description="Skip this many (paging).", ge=0)] = 0,
    ) -> dict[str, Any]:
        """List inferred subtasks (clusters of work units) with a label, size, time span, unit states, main
        actors, handoff count and `agreement`: the share of member pairs the other methods also group together
        (low = the grouping is method-dependent; check it). Use subtasks_get for members and evidence."""
        c, inf = get_inf(corpus)
        lim, note = ctx.limit(limit)
        s, u = parse_time(since, field="since"), parse_time(until, end=True, field="until")
        who = resolve_actor(c, inf, actor) if actor else None
        store = name_store(inf)
        rows = []
        for k, members in enumerate(inf.clusters[method][granularity]):
            if len(members) < min_size or (who and not any(who in inf.touch[i] for i in members)):
                continue
            row = cluster_summary(inf, method, granularity, k, members, store)
            if (s and row["end"] < iso(s)) or (u and row["start"] >= iso(u)):
                continue
            rows.append(row)
        if sort == "size":
            rows.sort(key=lambda x: -x["size"])
        elif sort == "handoffs":
            rows.sort(key=lambda x: -x["handoffs_inside"])
        page = rows[offset : offset + lim]
        cl = inf.clusters[method][granularity]
        return {
            "corpus": c.name,
            "unit": c.unit_noun,
            "method": method,
            "method_description": METHOD_DESCRIPTIONS[method],
            "granularity": granularity,
            "filters": {k: v for k, v in (("actor", who), ("since", iso(s)), ("until", iso(u))) if v},
            "total_units": len(inf.units),
            "total_subtasks": len(cl),
            "single_unit_subtasks": sum(1 for x in cl if len(x) == 1),
            "total_matches": len(rows),
            "returned": len(page),
            "has_more": offset + len(page) < len(rows),
            "subtasks": page,
            "notes": [
                n
                for n in [
                    note,
                    "name = the cleaned title of the member the others built on most (else the most central one), "
                    "unless a model or agent named this exact group (name_source; subtasks_name); keywords = the most "
                    "distinctive terms in member titles, content and artifact names. Both echo the actors' own "
                    "wording, not a verified objective",
                    *inf.notes,
                ]
                if n
            ],
        }

    @ctx.tool()
    def get(
        subtask_id: Annotated[
            str, Field(description="A subtask_id from subtasks_list, e.g. 'rpg-game/combined/medium/7'.")
        ],
        max_members: Annotated[int, Field(description="Max member units to list (default 40).", ge=1, le=200)] = 40,
        max_chat: Annotated[int, Field(description="Max chat messages to cite (default 10).", ge=0, le=100)] = 10,
    ) -> dict[str, Any]:
        """One subtask in detail: its name, keywords and (if named by a model/agent) objective; member units
        (event ids) with the signals that tie each to the group, who did what, typed handoffs between actors with
        action-level evidence, duplicates, the subtasks it built on and that built on it, the coarser subtask it is
        part of and its finer parts, how the other methods split these units, and the chat that discusses them."""
        name, method, level, k = parse_sid(subtask_id)
        c, inf = get_inf(name)
        cl = inf.clusters[method][level]
        if not 0 <= k < len(cl):
            raise ToolInputError(f"{subtask_id!r}: there are {len(cl)} subtasks for {method}/{level}.")
        members = cl[k]
        store = name_store(inf)
        inside = set(members)
        rows = []
        for i in members[:max_members]:
            row = unit_brief(inf, i)
            others = [j for j in members if j != i]
            if others:
                j = max(others, key=lambda j: score(inf, method, i, j))
                row["closest_member"] = inf.units[j].event_id
                row["signals"] = why(inf, i, j)
            if inf.rewrites[i]:
                row["rewrote"] = inf.rewrites[i]
            rows.append(row)
        internal = [edge_dict(c, inf, e) for e in inf.edges if e.src in inside and e.dst in inside]
        links = subtask_links(inf, method, level)
        builds_on = [link_dict(inf, ln, "src", method, level, store) for ln in links if ln.dst == k][:8]
        built_on_by = [link_dict(inf, ln, "dst", method, level, store) for ln in links if ln.src == k][:8]
        lv = list(LEVELS)
        up = parents(inf, method, level)
        part_of = None
        if up:
            p, share = up[k]
            part_of = {**ref(inf, method, lv[lv.index(level) - 1], p, store), "share_of_members": share}
        finer_parts = []
        if level != lv[-1]:
            finer = lv[lv.index(level) + 1]
            for g, (p, share) in enumerate(parents(inf, method, finer) or []):
                if p == k:
                    finer_parts.append({**ref(inf, method, finer, g, store), "share_inside": share})
            finer_parts.sort(key=lambda x: -x["size"])

        splits = []
        for m in METHODS:
            if m == method:
                continue
            parts = collections.Counter(int(inf.label_of[m][level][i]) for i in members)
            big, n = parts.most_common(1)[0]
            splits.append(
                {
                    "method": m,
                    "pieces": len(parts),
                    "largest_piece": {
                        "subtask_id": sid(inf.corpus, m, level, big),
                        "name": label(inf, m, level, big, store),
                        "members": n,
                    },
                }
            )
        msgs = sorted({x for i in members for x in inf.chat_of_unit.get(i, [])})
        cited = []
        for x in msgs[:max_chat]:
            m = inf.chat[x]
            cited.append(
                {
                    "event_id": m.event_id,
                    "time": iso(m.time),
                    "actor": m.actor,
                    "units": m.units,
                    "snippet": ctx.untrusted(m.text, SNIPPET_CHARS),
                }
            )
        unresolved = []
        n_open = sum(1 for i in members if inf.units[i].completed is False)
        if n_open:
            unresolved.append(f"{n_open} member {c.unit_noun}(s) did not complete (state: not merged/published)")
        coh = cohesion(inf, members, method, level)
        weak = [m for m, v in coh.items() if v < 0.3]
        if weak:
            unresolved.append(
                f"methods {', '.join(weak)} mostly split these units apart: the grouping may be method-dependent"
            )
        if not internal:
            unresolved.append(
                "no cross-actor handoffs inside: actors may have worked in parallel, or links went through hub artifacts"
            )
        return {
            **cluster_summary(inf, method, level, k, members, store),
            "corpus": c.name,
            "unit": c.unit_noun,
            "method": method,
            "granularity": level,
            "members": rows,
            "members_omitted": max(0, len(members) - max_members),
            "participants": participants(inf, members),
            "handoffs": internal,
            "builds_on": builds_on,
            "built_on_by": built_on_by,
            "part_of": part_of,
            "parts": finer_parts[:12],
            "parts_omitted": max(0, len(finer_parts) - 12),
            "other_methods": splits,
            "agreement_by_method": coh,
            **(
                {
                    "dataset_labels": {
                        "about": c.tag_name,
                        "counts": dict(
                            sum(
                                (collections.Counter(inf.units[i].tags) for i in members), collections.Counter()
                            ).most_common(6)
                        ),
                    }
                }
                if c.tag_name
                else {}
            ),
            "chat": {"messages_mentioning_members": len(msgs), "cited": cited},
            "unresolved": unresolved,
            "notes": [
                "signals compare each unit with its closest member: shared terms/artifacts explain the grouping",
                "builds_on / built_on_by: other subtasks linked by handoffs (this one used their work / they used "
                "this one's); part_of / parts: the subtask one granularity coarser holding most of these units, and "
                "the finer subtasks mostly inside this one (granularities are clustered independently)",
                f"handoffs are between different actors and come from shared {c.artifact_noun}s; 'duplicate' is inferred",
                *inf.notes,
            ],
        }

    @ctx.tool()
    def trace_pair(
        actor_a: Annotated[str, Field(description="First actor (agent name or short form, e.g. 'Opus 4.5').")],
        actor_b: Annotated[str, Field(description="Second actor.")],
        corpus: Annotated[str | None, Field(description="Corpus name; optional if there is only one.")] = None,
        method: Annotated[Method, Field(description="Method used to name shared subtasks.")] = "combined",
        granularity: Annotated[Level, Field(description="Granularity used for shared subtasks.")] = "medium",
        limit: Annotated[int, Field(description="Max handoffs to list (default 30).", ge=1, le=200)] = 30,
    ) -> dict[str, Any]:
        """What two actors did with each other's work: direct handoffs in both directions (with action-level
        evidence), duplicated work, units one finalised (e.g. merged) for the other, and the subtasks both
        took part in (with roles)."""
        c, inf = get_inf(corpus)
        a, b = resolve_actor(c, inf, actor_a), resolve_actor(c, inf, actor_b)
        if a == b:
            raise ToolInputError("actor_a and actor_b resolve to the same actor")
        pair = {a, b}
        direct = [e for e in inf.edges if {e.giver, e.taker} == pair]
        finalised = [
            i
            for i in range(len(inf.units))
            if inf.authors[i] in pair
            and any(r not in ("author", "action") for r in inf.touch[i].get((pair - {inf.authors[i]}).pop(), ()))
        ]
        lab = inf.label_of[method][granularity]
        store = name_store(inf)
        shared = []
        for k, members in enumerate(inf.clusters[method][granularity]):
            ra = sorted({x for i in members for x in inf.touch[i].get(a, ())})
            rb = sorted({x for i in members for x in inf.touch[i].get(b, ())})
            if ra and rb:
                n = sum(1 for e in direct if lab[e.dst] == k and e.kind != "duplicate")
                shared.append(
                    {
                        "subtask_id": sid(inf.corpus, method, granularity, k),
                        "name": label(inf, method, granularity, k, store),
                        "size": len(members),
                        "roles": {a: ra, b: rb},
                        "handoffs_between_them": n,
                    }
                )
        shared.sort(key=lambda x: (-x["handoffs_between_them"], -x["size"]))
        by_dir = collections.Counter((e.giver, e.kind) for e in direct)
        return {
            "corpus": c.name,
            "actors": [a, b],
            "summary": {
                f"{a} -> {b}": {k: n for (g, k), n in by_dir.items() if g == a},
                f"{b} -> {a}": {k: n for (g, k), n in by_dir.items() if g == b},
                "finalised_the_others_unit": len(finalised),
                "shared_subtasks": len(shared),
            },
            "handoffs": [edge_dict(c, inf, e) for e in direct[:limit]],
            "handoffs_omitted": max(0, len(direct) - limit),
            "finalised_for_each_other": [unit_brief(inf, i) for i in finalised[:20]],
            "shared_subtasks": shared[:30],
            "notes": [
                "'A -> B' counts work by A that B picked up (B built on, integrated, tested, fixed or re-submitted it)",
                "messages between them are not included here; search them with the dataset's own tools",
                *inf.notes,
            ],
        }

    @ctx.tool()
    def locate(
        event_id: Annotated[
            str,
            Field(
                description="An evidence id: a unit (e.g. a pull request period), a record in one, or a message from another source that names a unit."
            ),
        ],
        method: Annotated[Method, Field(description="Inference method.")] = "combined",
        granularity: Annotated[Level, Field(description="Granularity.")] = "medium",
    ) -> dict[str, Any]:
        """Which subtask(s) an event belongs to: a unit's subtask, an action's unit's subtask, or the subtasks
        of the units a chat message points at. Bridges search results to subtasks."""
        ref = evidence.parse(event_id)
        cs = corpora()
        # only load the corpora this id can belong to (each corpus loads on first use): its own source, or,
        # for a message from another source, the corpora whose units have numbers chat can name
        if ref.source in cs:
            names = [ref.source]
        else:
            with ctx.store() as s:
                names = [
                    n
                    for n in cs
                    if s.scalar(
                        "SELECT count(*) FROM periods WHERE source = ? AND json_extract(meta, '$.number') IS NOT NULL",
                        [n],
                    )
                ]
        found: list[tuple[Inference, int]] = []
        for name in names:
            inf = get_inf(name)[1]
            if event_id in inf.index:
                found.append((inf, inf.index[event_id]))
            found += [(inf, i) for i in inf.unit_of_action.get(event_id, [])]
            for m in inf.chat:
                if m.event_id == event_id:
                    found += [(inf, inf.index[e]) for e in m.units if e in inf.index]
                    break
        out = []
        for inf, i in found:
            k = int(inf.label_of[method][granularity][i])
            out.append(
                {
                    "unit": unit_brief(inf, i),
                    "subtask": cluster_summary(inf, method, granularity, k, inf.clusters[method][granularity][k]),
                }
            )
        return {
            "event_id": event_id,
            "matches": out,
            "notes": [] if out else ["not part of any unit (a message must name a unit's number, e.g. 'PR #12')"],
        }

    @ctx.tool()
    def graph(
        corpus: Annotated[str | None, Field(description="Corpus name; optional if there is only one.")] = None,
        method: Annotated[Method, Field(description="Inference method.")] = "combined",
        granularity: Annotated[Level, Field(description="Granularity.")] = "medium",
        min_size: Annotated[int, Field(description="Ignore subtasks smaller than this (units).", ge=1)] = 2,
        min_handoffs: Annotated[int, Field(description="Ignore links with fewer handoffs.", ge=1)] = 1,
        duplicates: Annotated[
            bool, Field(description="True: show 'duplicate' links (similar work, no handoff) instead of handoffs.")
        ] = False,
        limit: Annotated[int, Field(description="Max links (default 40, max 200).", ge=1, le=200)] = 40,
    ) -> dict[str, Any]:
        """Structure over subtasks: which subtasks built on which. A link A -> B aggregates the handoffs from
        units of A to units of B (B's actors built on, integrated, tested, fixed or re-submitted A's work), with
        counts by type, the actor pairs and sample evidence ids. Also orders the subtasks into stages (stage 0
        = built on nothing else shown; mutual dependencies share a stage) and lists foundations (others built
        on them, they built on none) and the most-built-on subtasks."""
        c, inf = get_inf(corpus)
        store = name_store(inf)
        cl = inf.clusters[method][granularity]
        links = [
            ln
            for ln in subtask_links(inf, method, granularity, duplicates=duplicates)
            if len(ln.edges) >= min_handoffs and len(cl[ln.src]) >= min_size and len(cl[ln.dst]) >= min_size
        ]
        shown = links[:limit]
        G = nx.DiGraph()
        G.add_edges_from((ln.src, ln.dst) for ln in links)
        C = nx.condensation(G)
        stage_of_comp: dict[int, int] = {}
        for comp in nx.topological_sort(C):
            preds = list(C.predecessors(comp))
            stage_of_comp[comp] = 1 + max(stage_of_comp[p] for p in preds) if preds else 0
        stage = {n: stage_of_comp[C.graph["mapping"][n]] for n in G.nodes}
        cycles = [sorted(C.nodes[x]["members"]) for x in C.nodes if len(C.nodes[x]["members"]) > 1]

        def node(k: int) -> dict[str, Any]:
            return {
                **ref(inf, method, granularity, k, store),
                "stage": stage.get(k),
                "built_on": G.in_degree(k) if k in G else 0,
                "built_on_by": G.out_degree(k) if k in G else 0,
            }

        nodes = sorted({k for ln in shown for k in (ln.src, ln.dst)}, key=lambda k: (stage.get(k, 0), k))
        out_weight = collections.Counter()
        for ln in links:
            out_weight[ln.src] += len(ln.edges)
        return {
            "corpus": c.name,
            "method": method,
            "granularity": granularity,
            "kind": "duplicate" if duplicates else "handoff",
            "total_links": len(links),
            "returned": len(shown),
            "links": [
                {
                    "from": sid(inf.corpus, method, granularity, ln.src),
                    "to": sid(inf.corpus, method, granularity, ln.dst),
                    "handoffs": len(ln.edges),
                    "types": dict(ln.kinds.most_common()),
                    "actor_pairs": len(ln.actors),
                    "evidence": [
                        x for i in ln.edges[:2] for x in inf.edges[i].giver_actions[:1] + inf.edges[i].taker_actions[:1]
                    ],
                }
                for ln in shown
            ],
            "subtasks": [node(k) for k in nodes],
            "foundations": [node(k) for k, _ in out_weight.most_common() if G.in_degree(k) == 0][:8],
            "most_built_on": [{**node(k), "handoffs_out": n} for k, n in out_weight.most_common(8)],
            "mutual_dependencies": [[sid(inf.corpus, method, granularity, k) for k in cyc] for cyc in cycles[:10]],
            "notes": [
                "a link A -> B means actors in B used work from A; 'stage' is the longest chain of such links "
                "leading into a subtask (subtasks that depend on each other share a stage)",
                "links come from shared artifacts between different actors (see subtasks_get for unit-level handoffs); "
                "hub artifacts never create them",
                *inf.notes,
            ],
        }

    @ctx.tool(read_only=False)
    def name(
        subtask_id: Annotated[
            str, Field(description="A subtask_id from subtasks_list, e.g. 'rpg-game/combined/medium/7'.")
        ],
        name: Annotated[
            str | None, Field(description="Your name for this subtask (3-8 words); stored and shown from now on.")
        ] = None,
        objective: Annotated[
            str | None, Field(description="With name: one sentence on what the work aimed at.")
        ] = None,
        generate: Annotated[
            bool, Field(description="Without name: ask the configured model to name it (needs ANTHROPIC_API_KEY).")
        ] = False,
    ) -> dict[str, Any]:
        """Name a subtask. Without arguments: its current name, keywords and the titles of its most central units,
        to name it from. With name (and objective): store your name. With generate=true: ask the configured model.
        Names attach to this exact set of units, so they show under every method/granularity that finds the same
        group, and stop applying if a rerun changes the group. A name you write replaces a model's."""
        corpus_name, method, level, k = parse_sid(subtask_id)
        c, inf = get_inf(corpus_name)
        cl = inf.clusters[method][level]
        if not 0 <= k < len(cl):
            raise ToolInputError(f"{subtask_id!r}: there are {len(cl)} subtasks for {method}/{level}.")
        store = name_store(inf)
        key = naming.member_key(inf, cl[k])
        action = "read"
        if name is not None:
            clean_name = naming.clean(name, naming.NAME_CHARS)
            if not clean_name:
                raise ToolInputError("name is empty")
            store.put(
                key,
                {
                    "name": clean_name,
                    "objective": naming.clean(objective, naming.OBJECTIVE_CHARS),
                    "source": "agent",
                    "size": len(cl[k]),
                },
            )
            action = "stored"
        elif generate:
            try:
                client = llm.get_client(ctx.config)
            except llm.LLMUnavailable as e:
                raise ToolInputError(
                    f"{e} Without a key, name it yourself: subtasks_name(subtask_id, name=...)."
                ) from None
            res = naming.generate(
                client, inf, [(method, level, k)], store, unit_noun=c.unit_noun, scrub=ctx.scrub, cap=1, force=True
            )
            if res["failed"]:
                raise ToolInputError(f"the model call failed: {'; '.join(res['errors'])}")
            action = "generated" if res["named"] else "kept (an agent already named it)"
        out = {
            "subtask_id": subtask_id,
            "action": action,
            **naming_fields(inf, method, level, k, store),
            "size": len(cl[k]),
        }
        if action == "read":
            out["central_titles"] = [
                ctx.untrusted(inf.units[i].title, TITLE_CHARS) for i in inf.exemplars[method][level][k]
            ]
        out["notes"] = [
            "names attach to this exact set of units (every method/granularity that finds it); they are untrusted "
            "text like the titles they describe"
        ]
        return out
