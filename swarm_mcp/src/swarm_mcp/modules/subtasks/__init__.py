"""Subtasks: which work units (pull requests, edit sessions...) belong to the same piece of work, who did
what in it, and the typed handoffs between actors (built on, integrated, tested, fixed, re-submitted,
duplicated).

Works on any *corpus* an adapter in ``sources.py`` can turn into units: each git repo (units = PRs, with
village chat that mentions PR numbers when the village dataset is present), and other corpora as their
adapters are added. Inference runs once per corpus on first use and is cached. Every result cites
event ids that ``core_get`` expands. Subtask ids look like ``<corpus>/<method>/<granularity>/<n>``
and are stable for a given dataset and code version.
"""

from __future__ import annotations

import collections
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from swarm_mcp.events import parse_event_id
from swarm_mcp.modules.git import get_repo, repo_paths
from swarm_mcp.modules.subtasks.infer import (
    LEVELS,
    METHOD_DESCRIPTIONS,
    METHODS,
    Edge,
    Inference,
    cohesion,
    infer,
    score,
    why,
)
from swarm_mcp.modules.subtasks.sources import Corpus, git_units, link_chat_to_prs, wiki_units
from swarm_mcp.modules.wiki import get_wiki, wiki_paths
from swarm_mcp.toolkit import TS_FORMAT, ToolInputError, iso, parse_time, truncate

NAME = "subtasks"
DESCRIPTION = (
    "Groups work units (git PRs, and other corpora with adapters) into subtasks with several inference methods, so "
    "disagreement is visible, and derives typed handoffs between actors: who built on, integrated, tested, fixed, "
    "re-submitted or duplicated whose work. subtasks_corpora lists what is available. Results cite event ids."
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


def requires(ctx) -> list[str]:
    try:
        import networkx  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as e:
        return [f"missing dependency: {e.name} (uv sync in swarm_mcp)"]
    if not repo_paths(ctx) and not wiki_paths(ctx):
        return ["no corpora available: no git repos (<data>/*/repos/*.git) and no wiki databases (<data>/*/*.db)"]
    return []


def _village_dir(ctx) -> Path:
    override = ctx.config.module_setting("village", "dir")
    return Path(override).expanduser() if override else ctx.data_dir / "ai-village"


def _shift(ts: str, days: int) -> str:
    return (datetime.strptime(ts, TS_FORMAT) + timedelta(days=days)).strftime(TS_FORMAT)


def register(mcp, ctx) -> None:
    # ------------------------------------------------------------------ village helpers (optional)

    def village():
        """(meta, chat) from the village dataset, shared with the village module's cache; None if absent."""
        root = _village_dir(ctx)
        if not (root / "chat_messages.jsonl.gz").exists() or not (root / "agents.jsonl.gz").exists():
            return None
        from swarm_mcp.modules.village.data import load_chat, load_meta

        return ctx.cache.get("village:meta", lambda: load_meta(root)), ctx.cache.get(
            "village:chat", lambda: load_chat(root)
        )

    def agent_namer():
        """git (author, email) -> village agent name when it can be matched, else 'git:<author>'."""
        v = village()
        cache: dict[tuple[str, str], str] = {}

        def agent_of(author: str, email: str) -> str:
            if (author, email) not in cache:
                name = None
                if v is not None:
                    local = email.split("@", 1)[0] if email.endswith("@agentvillage.org") else None
                    for cand in (local, author):
                        if not cand:
                            continue
                        try:
                            aid = v[0].matcher.resolve(cand)
                        except ToolInputError:
                            continue
                        if aid in v[0].agents_by_id:
                            name = v[0].agent_name(aid)
                            break
                cache[(author, email)] = name or f"git:{author}"
            return cache[(author, email)]

        return agent_of

    # ------------------------------------------------------------------ corpora

    def git_corpus(name: str) -> Corpus:
        def load():
            repo = get_repo(ctx, name)
            units = git_units(repo, agent_namer())
            v = village()
            notes = ["reviews and PR open/close events are not used yet (git only); start = first commit time"]
            if v is None:
                return units, [], notes + ["village chat not available: chat and chat-mention signals are empty"]
            meta, chat = v
            lo, hi = _shift(min(u.start for u in units), -1), _shift(max(u.end or u.start for u in units), 1)
            msgs = []
            for i in chat.window(None, lo, hi):
                m = chat.msgs[i]
                actor = meta.agent_name(m.agent_id) if m.agent_id else f"human:{(m.user_id or '?')[:8]}"
                msgs.append((ctx.event_id("chat", m.id, source="village"), m.ts, actor, m.content))
            return units, link_chat_to_prs(repo, msgs), notes

        return Corpus(name, "git repo", "pull request", "commit", "file", load)

    def wiki_corpus(name: str) -> Corpus:
        def load():
            w = get_wiki(ctx, name)
            return (
                wiki_units(w),
                [],
                [
                    "units are edit sessions: one actor label's revisions with gaps <= 30 min; a label may be many agents",
                    "the wiki itself is the message channel, so the chat signal is empty; links between pages are refs",
                    "'builds_on' here means editing (posting to) a page another actor's session created",
                    "'duplicate' here means near-identical posts (similarity > 0.8) by different actors within 3 days "
                    "(copying or echoing); only each session's 3 closest are kept",
                ],
            )

        return Corpus(
            name,
            "wiki",
            "edit session",
            "revision",
            "page",
            load,
            tag_name="page_family (publishers' heuristic label)",
            dup_min=0.8,  # wiki posts are heavily templated
        )

    def corpora() -> dict[str, Corpus]:
        out = {n: git_corpus(n) for n in sorted(repo_paths(ctx))}
        for n in sorted(wiki_paths(ctx)):
            out.setdefault(n, wiki_corpus(n))
        return out

    def get_corpus(name: str | None) -> Corpus:
        cs = corpora()
        if name is None:
            if len(cs) != 1:
                raise ToolInputError(f"Pass corpus= one of: {', '.join(cs)} (see subtasks_corpora).")
            return next(iter(cs.values()))
        if name not in cs:
            raise ToolInputError(f"Unknown corpus {name!r}. Corpora: {', '.join(cs)}.")
        return cs[name]

    def get_inf(name: str | None) -> tuple[Corpus, Inference]:
        c = get_corpus(name)

        def build() -> Inference:
            units, chat, notes = c.load()
            inf = infer(c.name, units, chat, dup_min=c.dup_min)
            inf.notes.extend(notes)
            return inf

        return c, ctx.lazy(f"inference:{c.name}", build)

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
            "summary": f"{e.taker} {verb} {e.giver}'s {a.short}"
            + (f" ({b.short})" if e.kind != "duplicate" else f" in {b.short}"),
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
            "title": ctx.scrub(u.title),
            "actor": inf.authors[i],
            "state": u.state,
            "start": iso(u.start),
        }

    def resolve_actor(inf: Inference, name: str) -> str:
        """User-typed actor -> the name used in inference results (village aliases understood)."""
        known = {x for t in inf.touch for x in t}
        if name in known:
            return name
        v = village()
        if v is not None:
            try:
                aid = v[0].matcher.resolve(name)
                if aid in v[0].agents_by_id and v[0].agent_name(aid) in known:
                    return v[0].agent_name(aid)
            except ToolInputError:
                pass
        low = name.lower()
        hits = sorted(k for k in known if low in k.lower())
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ToolInputError(f"actor {name!r} is ambiguous: {', '.join(hits[:10])}")
        raise ToolInputError(f"{name!r} did no work in corpus {inf.corpus}.")

    def cluster_summary(inf, method, level, k, members) -> dict[str, Any]:
        U = [inf.units[i] for i in members]
        authors = collections.Counter(inf.authors[i] for i in members if inf.authors[i])
        inside = set(members)
        coh = cohesion(inf, members, method, level)
        return {
            "subtask_id": sid(inf.corpus, method, level, k),
            "label": inf.names[method][level][k],
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
        """List the corpora subtasks can analyse, with what a unit, action and artifact are in each."""
        out = []
        for c in corpora().values():
            row = {
                "corpus": c.name,
                "kind": c.kind,
                "unit": c.unit_noun,
                "action": c.action_noun,
                "artifact": c.artifact_noun,
            }
            if f"subtasks:inference:{c.name}" in ctx.cache:
                inf = get_inf(c.name)[1]
                row.update(units=len(inf.units), actors=len({a for t in inf.touch for a in t}))
            out.append(row)
        return {"corpora": out, "notes": ["unit/actor counts appear after a corpus's first use"]}

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
        who = resolve_actor(inf, actor) if actor else None
        rows = []
        for k, members in enumerate(inf.clusters[method][granularity]):
            if len(members) < min_size or (who and not any(who in inf.touch[i] for i in members)):
                continue
            row = cluster_summary(inf, method, granularity, k, members)
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
                    "labels are the most distinctive terms in member titles, content and artifact names: they echo "
                    "the actors' own wording, not a verified objective",
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
        """One subtask in detail: member units (event ids) with the signals that tie each to the group, who did
        what, typed handoffs between actors with action-level evidence, duplicates, handoffs to/from other
        subtasks, how the other methods split these units, and the chat messages that discuss them."""
        name, method, level, k = parse_sid(subtask_id)
        c, inf = get_inf(name)
        cl = inf.clusters[method][level]
        if not 0 <= k < len(cl):
            raise ToolInputError(f"{subtask_id!r}: there are {len(cl)} subtasks for {method}/{level}.")
        members = cl[k]
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
        incoming = [e for e in inf.edges if e.kind != "duplicate" and e.dst in inside and e.src not in inside]
        outgoing = [e for e in inf.edges if e.kind != "duplicate" and e.src in inside and e.dst not in inside]

        def other_groups(es: list[Edge], end: str) -> list[dict[str, Any]]:
            lab = inf.label_of[method][level]
            cnt = collections.Counter(int(lab[getattr(e, end)]) for e in es)
            return [
                {"subtask_id": sid(inf.corpus, method, level, g), "label": inf.names[method][level][g], "handoffs": n}
                for g, n in cnt.most_common(5)
            ]

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
                        "label": inf.names[m][level][big],
                        "members": n,
                    },
                }
            )
        msgs = sorted({x for i in members for x in inf.chat_of_unit.get(i, [])})
        cited = []
        for x in msgs[:max_chat]:
            m = inf.chat[x]
            text, _ = truncate(ctx.scrub(m.text), 160)
            cited.append(
                {"event_id": m.event_id, "time": iso(m.time), "actor": m.actor, "units": m.units, "snippet": text}
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
            **cluster_summary(inf, method, level, k, members),
            "corpus": c.name,
            "unit": c.unit_noun,
            "method": method,
            "granularity": level,
            "members": rows,
            "members_omitted": max(0, len(members) - max_members),
            "participants": participants(inf, members),
            "handoffs": internal,
            "handoffs_from_other_subtasks": other_groups(incoming, "src"),
            "handoffs_to_other_subtasks": other_groups(outgoing, "dst"),
            "other_methods": splits,
            "agreement_by_method": coh,
            **(
                {
                    "dataset_labels": {
                        "name": c.tag_name,
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
        a, b = resolve_actor(inf, actor_a), resolve_actor(inf, actor_b)
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
        shared = []
        for k, members in enumerate(inf.clusters[method][granularity]):
            ra = sorted({x for i in members for x in inf.touch[i].get(a, ())})
            rb = sorted({x for i in members for x in inf.touch[i].get(b, ())})
            if ra and rb:
                n = sum(1 for e in direct if lab[e.dst] == k and e.kind != "duplicate")
                shared.append(
                    {
                        "subtask_id": sid(inf.corpus, method, granularity, k),
                        "label": inf.names[method][granularity][k],
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
            str, Field(description="An event id: a unit (git:pr…), an action (git:commit…) or a message.")
        ],
        method: Annotated[Method, Field(description="Inference method.")] = "combined",
        granularity: Annotated[Level, Field(description="Granularity.")] = "medium",
    ) -> dict[str, Any]:
        """Which subtask(s) an event belongs to: a unit's subtask, an action's unit's subtask, or the subtasks
        of the units a chat message points at. Bridges search results to subtasks."""
        eid = parse_event_id(event_id)
        cs = corpora()
        # only load the corpora this id can belong to (each corpus loads on first use)
        if eid.source == "wiki":
            names = [n for n in cs if n == eid.local_id.split("/", 1)[0]]
        elif eid.source == "git":
            names = [n for n in cs if n == re.split(r"[#@]", eid.local_id, maxsplit=1)[0]]
        else:
            names = [n for n, c in cs.items() if c.kind == "git repo"]  # chat messages point at PRs
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
            "notes": [] if out else ["not part of any unit (a message must point at a unit of a loaded corpus)"],
        }
