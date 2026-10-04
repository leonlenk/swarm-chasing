"""Subtasks: which pull requests belong to the same piece of work, who did what in it, and the typed
handoffs between agents (built on, integrated, tested, fixed, re-submitted, duplicated).

Built from the ``git`` module's repos plus, when the village dataset is present, chat messages that
mention PRs ("PR #12", "pull/12", "#152"). Inference runs once per repo on first use (~15 s) and is
cached. Every result cites event ids (``git:pr:…``, ``git:commit:…``, ``village:chat:…``) that
``core_get_event`` expands. Subtask ids look like ``<repo>/<method>/<granularity>/<n>`` and are stable
for a given dataset and code version.
"""

from __future__ import annotations

import collections
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from swarm_mcp.modules.git import commit_event_id, get_repo, pr_event_id, repo_paths
from swarm_mcp.modules.subtasks.infer import (
    LEVELS,
    METHOD_DESCRIPTIONS,
    METHODS,
    PR_RX,
    ChatMsg,
    Edge,
    Inference,
    cohesion,
    infer,
    why,
)
from swarm_mcp.toolkit import ToolInputError, iso, parse_time, truncate

NAME = "subtasks"
DESCRIPTION = (
    "Groups a repo's pull requests into subtasks (several inference methods, so disagreement is visible) and "
    "derives typed handoffs between agents from git: who built on, integrated, tested, fixed, re-submitted or "
    "duplicated whose work. Results cite event ids for core_get_event."
)

Method = Literal["combined", "code", "title", "files", "chat", "refs"]
Level = Literal["coarse", "medium", "fine"]
EDGE_VERBS = {
    "builds_on": "edited a file created in",
    "integrates": "imported a module created in",
    "tests": "added tests for a module created in",
    "fixes": "fixed files created in",
    "resubmits": "re-created the files of",
    "duplicate": "duplicated",
}


def requires(ctx) -> list[str]:
    try:
        import networkx  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as e:
        return [f"missing dependency: {e.name} (uv sync in swarm_mcp)"]
    if not repo_paths(ctx):
        return ["no git repos available (the git module finds bare clones under <data>/*/repos/)"]
    return []


def _village_dir(ctx) -> Path:
    override = ctx.config.module_setting("village", "dir")
    return Path(override).expanduser() if override else ctx.data_dir / "ai-village"


def register(mcp, ctx) -> None:
    def village():
        """(meta, chat) from the village dataset, shared with the village module's cache; None if absent."""
        root = _village_dir(ctx)
        if not (root / "chat_messages.jsonl.gz").exists() or not (root / "agents.jsonl.gz").exists():
            return None
        from swarm_mcp.modules.village.data import load_chat, load_meta

        meta = ctx.cache.get("village:meta", lambda: load_meta(root))
        chat = ctx.cache.get("village:chat", lambda: load_chat(root))
        return meta, chat

    def agent_namer():
        """git (author, email) -> village agent name when it can be matched, else 'git:<author>'."""
        v = village()
        cache: dict[tuple[str, str], str] = {}

        def agent_of(author: str, email: str) -> str:
            key = (author, email)
            if key not in cache:
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
                cache[key] = name or f"git:{author}"
            return cache[key]

        return agent_of

    def linked_chat(repo) -> list[ChatMsg]:
        v = village()
        if v is None:
            return []
        meta, chat = v
        starts = [p.start for p in repo.prs.values() if p.start]
        ends = [p.end or p.start for p in repo.prs.values() if p.start]
        if not starts:
            return []
        lo, hi = _shift(min(starts), -1), _shift(max(ends), 1)
        out = []
        for i in chat.window(None, lo, hi):
            m = chat.msgs[i]
            nums = {int(a or b) for a, b in PR_RX.findall(m.content)}
            nums = sorted(n for n in nums if n in repo.prs)
            if nums:
                actor = meta.agent_name(m.agent_id) if m.agent_id else f"human:{(m.user_id or '?')[:8]}"
                out.append(ChatMsg(ctx.event_id("chat", m.id, source="village"), m.ts, actor, m.content, nums))
        return out

    def get_inf(repo_name: str | None) -> tuple[Any, Inference]:
        repo = get_repo(ctx, repo_name)

        def build() -> Inference:
            inf = infer(repo, linked_chat(repo), agent_namer())
            if village() is None:
                inf.notes.append("village chat not available: chat and chat-mention signals are empty")
            return inf

        return repo, ctx.lazy(f"inference:{repo.name}", build)

    # ------------------------------------------------------------------ helpers

    def sid(repo: str, method: str, level: str, k: int) -> str:
        return f"{repo}/{method}/{level}/{k + 1}"

    def parse_sid(subtask_id: str) -> tuple[str, str, str, int]:
        m = re.fullmatch(r"(.+)/([a-z]+)/([a-z]+)/(\d+)", (subtask_id or "").strip())
        if not m or m.group(2) not in METHODS or m.group(3) not in LEVELS:
            raise ToolInputError(
                f"Malformed subtask_id {subtask_id!r}. Expected '<repo>/<method>/<granularity>/<n>' "
                "as returned by subtasks_list, e.g. 'rpg-game/combined/medium/7'."
            )
        return m.group(1), m.group(2), m.group(3), int(m.group(4)) - 1

    def participants(inf: Inference, members: list[int]) -> list[dict[str, Any]]:
        st: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        for i in members:
            for a, roles in inf.touch[i].items():
                for r in roles:
                    if r == "author":
                        st[a]["prs_authored"] += 1
                    elif r == "commit" and a != inf.authors[i]:
                        st[a]["commits_on_others_prs"] += 1
                    elif r == "merge" and a != inf.authors[i]:
                        st[a]["merged_others_prs"] += 1
        inside = set(members)
        for e in inf.edges:
            if e.kind != "duplicate" and e.src in inside and e.dst in inside:
                st[e.giver]["work_picked_up_by_others"] += 1
                st[e.taker]["picked_up_others_work"] += 1
        rows = [{"agent": a, **dict(c)} for a, c in st.items()]
        return sorted(rows, key=lambda r: (-r.get("prs_authored", 0), r["agent"]))

    def edge_dict(repo, inf: Inference, e: Edge) -> dict[str, Any]:
        a, b = inf.prs[e.src], inf.prs[e.dst]
        d: dict[str, Any] = {
            "type": e.kind,
            "from_agent": e.giver,
            "to_agent": e.taker,
            "summary": f"{e.taker} {EDGE_VERBS[e.kind]} {e.giver}'s PR #{a}"
            + (f" (PR #{b})" if e.kind != "duplicate" else f" in PR #{b}"),
            "from_pr": pr_event_id(repo, a),
            "to_pr": pr_event_id(repo, b),
            "time": iso(repo.prs[b].start),
        }
        if e.files:
            d["files"] = e.files
        if e.giver_commits or e.taker_commits:
            d["evidence"] = [commit_event_id(repo, s) for s in e.giver_commits[:2] + e.taker_commits[:3]]
        if e.score is not None:
            d["similarity"] = e.score
        d["status"] = "observed in git" if e.kind != "duplicate" else "inferred from similarity"
        return d

    def pr_brief(repo, inf: Inference, i: int) -> dict[str, Any]:
        p = repo.prs[inf.prs[i]]
        return {
            "event_id": pr_event_id(repo, p.number),
            "title": ctx.scrub(p.title),
            "author": inf.authors[i],
            "state": p.state,
            "start": iso(p.start),
        }

    def resolve_agent(name: str) -> str:
        """User-typed agent -> the display name used in inference results."""
        v = village()
        if v is not None:
            try:
                aid = v[0].matcher.resolve(name)
                if aid in v[0].agents_by_id:
                    return v[0].agent_name(aid)
            except ToolInputError:
                pass
        return name if name.startswith("git:") else f"git:{name}" if v is None else name

    def cluster_summary(repo, inf, method, level, k, members) -> dict[str, Any]:
        P = [repo.prs[inf.prs[i]] for i in members]
        authors = collections.Counter(inf.authors[i] for i in members if inf.authors[i])
        inside = set(members)
        coh = cohesion(inf, members, method, level)
        states = collections.Counter(p.state for p in P)
        return {
            "subtask_id": sid(repo.name, method, level, k),
            "label": inf.names[method][level][k],
            "size": len(members),
            "start": iso(min(p.start for p in P)),
            "end": iso(max((p.end or p.start) for p in P)),
            "states": dict(states),
            "authors": [{"agent": a, "prs": n} for a, n in authors.most_common(6)],
            "agents_involved": len({a for i in members for a in inf.touch[i]}),
            "handoffs_inside": sum(
                1 for e in inf.edges if e.kind != "duplicate" and e.src in inside and e.dst in inside
            ),
            "agreement": round(sum(coh.values()) / len(coh), 2) if coh else None,
        }

    # ------------------------------------------------------------------ tools

    @ctx.tool("list")
    def list_subtasks(
        repo: Annotated[str | None, Field(description="Repo name (git_repos); optional if there is only one.")] = None,
        method: Annotated[Method, Field(description="Inference method; 'combined' blends the others.")] = "combined",
        granularity: Annotated[
            Level, Field(description="coarse = themes, medium = subtasks, fine = small pieces.")
        ] = "medium",
        agent: Annotated[
            str | None, Field(description="Only subtasks this agent touched (authored, committed or merged).")
        ] = None,
        since: Annotated[
            str | None, Field(description="Only subtasks active at/after this ISO date/datetime (UTC).")
        ] = None,
        until: Annotated[
            str | None, Field(description="Only subtasks starting before this ISO date/datetime (UTC).")
        ] = None,
        min_size: Annotated[int, Field(description="Smallest subtask to list (PRs).", ge=1)] = 2,
        sort: Annotated[Literal["start", "size", "handoffs"], Field(description="Ordering.")] = "start",
        limit: Annotated[int, Field(description="Max subtasks (default 20, max 200).")] = 20,
        offset: Annotated[int, Field(description="Skip this many (paging).", ge=0)] = 0,
    ) -> dict[str, Any]:
        """List inferred subtasks (clusters of PRs) with a label, size, time span, PR states, main authors,
        handoff count and `agreement`: the share of member pairs that the other methods also group together
        (low = the grouping is method-dependent; check it). Use subtasks_get for members and evidence."""
        r, inf = get_inf(repo)
        lim, note = ctx.limit(limit)
        s, u = parse_time(since, field="since"), parse_time(until, end=True, field="until")
        who = resolve_agent(agent) if agent else None
        rows = []
        for k, members in enumerate(inf.clusters[method][granularity]):
            if len(members) < min_size:
                continue
            if who and not any(who in inf.touch[i] for i in members):
                continue
            row = cluster_summary(r, inf, method, granularity, k, members)
            if (s and row["end"] < iso(s)) or (u and row["start"] >= iso(u)):
                continue
            rows.append(row)
        if sort == "size":
            rows.sort(key=lambda x: -x["size"])
        elif sort == "handoffs":
            rows.sort(key=lambda x: -x["handoffs_inside"])
        page = rows[offset : offset + lim]
        total = len(inf.clusters[method][granularity])
        singles = sum(1 for c in inf.clusters[method][granularity] if len(c) == 1)
        return {
            "repo": r.name,
            "method": method,
            "method_description": METHOD_DESCRIPTIONS[method],
            "granularity": granularity,
            "filters": {k: v for k, v in (("agent", who), ("since", iso(s)), ("until", iso(u))) if v},
            "total_prs": len(inf.prs),
            "total_subtasks": total,
            "single_pr_subtasks": singles,
            "total_matches": len(rows),
            "returned": len(page),
            "has_more": offset + len(page) < len(rows),
            "subtasks": page,
            "notes": [
                n
                for n in [
                    note,
                    "labels are the most distinctive terms in member PR titles, code and file names: they describe the "
                    "PRs' own wording, not a verified objective",
                    "reviews and PR open/close events are not used yet (git only); start = first commit time",
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
        max_members: Annotated[int, Field(description="Max member PRs to list (default 40).", ge=1, le=200)] = 40,
        max_chat: Annotated[int, Field(description="Max chat messages to cite (default 10).", ge=0, le=100)] = 10,
    ) -> dict[str, Any]:
        """One subtask in detail: member PRs (event ids) with the signals that tie each to the group, who did
        what, typed handoffs between agents with commit evidence, duplicates, handoffs to/from other subtasks,
        how the other methods split these PRs, and the chat messages that discuss them."""
        repo_name, method, level, k = parse_sid(subtask_id)
        r, inf = get_inf(repo_name)
        cl = inf.clusters[method][level]
        if not 0 <= k < len(cl):
            raise ToolInputError(f"{subtask_id!r}: there are {len(cl)} subtasks for {method}/{level}.")
        members = cl[k]
        inside = set(members)
        S = inf.sim[method]
        rows = []
        for i in members[:max_members]:
            row = pr_brief(r, inf, i)
            others = [j for j in members if j != i]
            if others:
                j = max(others, key=lambda j: S[i, j])
                row["closest_member"] = pr_event_id(r, inf.prs[j])
                row["signals"] = why(inf, i, j)
            if inf.rewrites[i]:
                row["rewrote"] = inf.rewrites[i]
            rows.append(row)
        internal = [edge_dict(r, inf, e) for e in inf.edges if e.src in inside and e.dst in inside]
        incoming = [e for e in inf.edges if e.kind != "duplicate" and e.dst in inside and e.src not in inside]
        outgoing = [e for e in inf.edges if e.kind != "duplicate" and e.src in inside and e.dst not in inside]

        def other_groups(es: list[Edge], end: str) -> list[dict[str, Any]]:
            lab = inf.label_of[method][level]
            c = collections.Counter(int(lab[getattr(e, end)]) for e in es)
            return [
                {"subtask_id": sid(r.name, method, level, g), "label": inf.names[method][level][g], "handoffs": n}
                for g, n in c.most_common(5)
            ]

        splits = []
        for m in METHODS:
            if m == method:
                continue
            lab = inf.label_of[m][level]
            parts = collections.Counter(int(lab[i]) for i in members)
            big, n = parts.most_common(1)[0]
            splits.append(
                {
                    "method": m,
                    "pieces": len(parts),
                    "largest_piece": {
                        "subtask_id": sid(r.name, m, level, big),
                        "label": inf.names[m][level][big],
                        "members": n,
                    },
                }
            )
        msgs = sorted({c for i in members for c in inf.chat_of_pr.get(i, [])})
        cited = []
        for c in msgs[:max_chat]:
            m = inf.chat[c]
            text, _ = truncate(ctx.scrub(m.text), 160)
            cited.append({"event_id": m.event_id, "time": iso(m.time), "actor": m.actor, "prs": m.prs, "snippet": text})
        P = [r.prs[inf.prs[i]] for i in members]
        unresolved = []
        n_un = sum(1 for p in P if p.state == "unmerged")
        if n_un:
            unresolved.append(f"{n_un} member PR(s) never reached main (open or closed; git cannot tell)")
        coh = cohesion(inf, members, method, level)
        weak = [m for m, v in coh.items() if v < 0.3]
        if weak:
            unresolved.append(
                f"methods {', '.join(weak)} mostly split these PRs apart: the grouping may be method-dependent"
            )
        if not internal:
            unresolved.append(
                "no cross-agent handoffs inside: agents may have worked in parallel, or links went through hub files"
            )
        return {
            **cluster_summary(r, inf, method, level, k, members),
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
            "chat": {"messages_mentioning_members": len(msgs), "cited": cited},
            "unresolved": unresolved,
            "notes": [
                "signals compare each PR with its closest member: shared terms/files explain the grouping",
                "handoffs are between different commit authors and come from git; 'duplicate' is inferred",
                *inf.notes,
            ],
        }

    @ctx.tool()
    def trace_pair(
        agent_a: Annotated[str, Field(description="First agent (name or short form, e.g. 'Opus 4.5').")],
        agent_b: Annotated[str, Field(description="Second agent.")],
        repo: Annotated[str | None, Field(description="Repo name; optional if there is only one.")] = None,
        method: Annotated[Method, Field(description="Method used to name shared subtasks.")] = "combined",
        granularity: Annotated[Level, Field(description="Granularity used for shared subtasks.")] = "medium",
        limit: Annotated[int, Field(description="Max handoffs to list (default 30).", ge=1, le=200)] = 30,
    ) -> dict[str, Any]:
        """What two agents did with each other's work: direct handoffs in both directions (with commit
        evidence), duplicated work, PRs one merged for the other, and the subtasks both touched (with roles)."""
        r, inf = get_inf(repo)
        a, b = resolve_agent(agent_a), resolve_agent(agent_b)
        if a == b:
            raise ToolInputError("agent_a and agent_b resolve to the same agent")
        known = {x for t in inf.touch for x in t}
        missing = [x for x in (a, b) if x not in known]
        if missing:
            raise ToolInputError(f"{', '.join(missing)} made no commits or merges in {r.name}.")
        pair = {a, b}
        direct = [e for e in inf.edges if {e.giver, e.taker} == pair]
        merges = [
            i
            for i in range(len(inf.prs))
            if {inf.authors[i]} | {x for x, roles in inf.touch[i].items() if "merge" in roles} == pair
            and "merge" in inf.touch[i].get(a if inf.authors[i] == b else b, set())
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
                        "subtask_id": sid(r.name, method, granularity, k),
                        "label": inf.names[method][granularity][k],
                        "size": len(members),
                        "roles": {a: ra, b: rb},
                        "handoffs_between_them": n,
                    }
                )
        shared.sort(key=lambda x: (-x["handoffs_between_them"], -x["size"]))
        by_dir = collections.Counter((e.giver, e.kind) for e in direct)
        return {
            "repo": r.name,
            "agents": [a, b],
            "summary": {
                f"{a} -> {b}": {k: n for (g, k), n in by_dir.items() if g == a},
                f"{b} -> {a}": {k: n for (g, k), n in by_dir.items() if g == b},
                "merged_the_others_pr": len(merges),
                "shared_subtasks": len(shared),
            },
            "handoffs": [edge_dict(r, inf, e) for e in direct[:limit]],
            "handoffs_omitted": max(0, len(direct) - limit),
            "merged_for_each_other": [pr_brief(r, inf, i) for i in merges[:20]],
            "shared_subtasks": shared[:30],
            "notes": [
                "'A -> B' counts work by A that B picked up (B built on, integrated, tested, fixed or re-submitted it)",
                "chat between them is not included here; search it with village_search_chat",
                *inf.notes,
            ],
        }

    @ctx.tool()
    def locate(
        event_id: Annotated[str, Field(description="A git:pr, git:commit or village:chat event id.")],
        method: Annotated[Method, Field(description="Inference method.")] = "combined",
        granularity: Annotated[Level, Field(description="Granularity.")] = "medium",
    ) -> dict[str, Any]:
        """Which subtask(s) an event belongs to: a PR's subtask, a commit's PR's subtask, or the subtasks of the
        PRs a chat message mentions. Bridges search results (village_search_chat, git_prs) to subtasks."""
        from swarm_mcp.events import parse_event_id

        e = parse_event_id(event_id)
        found: list[tuple[Any, Inference, int]] = []
        if e.source == "git" and e.kind in ("pr", "commit"):
            name, _, ref = e.local_id.rpartition("#" if e.kind == "pr" else "@")
            r, inf = get_inf(name or None)
            if e.kind == "pr":
                if not ref.isdigit() or int(ref) not in r.prs:
                    raise ToolInputError(f"No PR {ref!r} in {r.name}.")
                nums = [int(ref)]
            else:
                c = r.find_commit(ref)
                nums = r.pr_of_commit.get(c.sha, []) if c else []
            found = [(r, inf, inf.prs.index(n)) for n in nums]
        elif e.source == "village" and e.kind == "chat":
            for name in sorted(repo_paths(ctx)):
                r, inf = get_inf(name)
                for m in inf.chat:
                    if m.event_id == event_id:
                        found += [(r, inf, inf.prs.index(n)) for n in m.prs]
                        break
        else:
            raise ToolInputError("locate takes git:pr, git:commit or village:chat event ids.")
        out = []
        for r, inf, i in found:
            k = int(inf.label_of[method][granularity][i])
            members = inf.clusters[method][granularity][k]
            out.append({"pr": pr_brief(r, inf, i), "subtask": cluster_summary(r, inf, method, granularity, k, members)})
        return {
            "event_id": event_id,
            "matches": out,
            "notes": []
            if out
            else ["no PR found for this event (a chat message must mention a PR number of a loaded repo)"],
        }


def _shift(ts: str, days: int) -> str:
    from datetime import datetime, timedelta

    from swarm_mcp.toolkit import TS_FORMAT

    return (datetime.strptime(ts, TS_FORMAT) + timedelta(days=days)).strftime(TS_FORMAT)
