"""Adapters: turn a dataset into ``Unit``s (work episodes) and linked ``ChatMsg``s for ``infer``.

Each adapter maps its dataset's notions onto the generic model:

    corpus     unit             action          artifact     actor
    git repo   pull request     commit          file         commit author (-> village agent)
    wiki       edit session     page revision   wiki page    editor handle

Add an adapter by writing a function that returns ``(units, chat, notes)`` and registering a
``Corpus`` for it in ``subtasks/__init__.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from swarm_mcp.modules.git import commit_event_id, pr_event_id
from swarm_mcp.modules.git.data import Repo
from swarm_mcp.modules.subtasks.infer import PR_RX, Action, Change, ChatMsg, Unit


@dataclass
class Corpus:
    name: str
    kind: str  # "git repo", "wiki"...
    unit_noun: str  # "pull request", "edit session"
    action_noun: str  # "commit", "revision"
    artifact_noun: str  # "file", "page"
    load: Callable[[], tuple[list[Unit], list[ChatMsg], list[str]]]
    notes: list[str] = field(default_factory=list)  # standing caveats shown with results


# --------------------------------------------------------------------------- git


def git_units(repo: Repo, agent_of: Callable[[str, str], str]) -> list[Unit]:
    """One unit per pull request; actions are its commits, artifacts are file paths."""
    units = []
    for pr in repo.prs.values():
        actions = []
        for sha in pr.commits:
            c = repo.commits[sha]
            changes = [
                Change(p, new=f.new, size=f.added + f.removed, lines=f.lines, imports=f.imports, rewrite=f.rewrite)
                for p, f in (c.files or {}).items()
            ]
            actions.append(Action(commit_event_id(repo, sha), agent_of(c.author, c.email), c.time, c.subject, changes))
        text = " ".join([pr.title] + [repo.commits[s].subject for s in pr.commits])
        refs = {pr_event_id(repo, int(n)) for n in re.findall(r"#(\d+)", text) if int(n) in repo.prs}
        units.append(
            Unit(
                event_id=pr_event_id(repo, pr.number),
                short=f"PR #{pr.number}",
                title=pr.title,
                start=pr.start,
                end=pr.end,
                state=pr.state,
                completed=pr.state != "unmerged",
                actions=actions,
                refs=refs,
                roles={agent_of(pr.merged_by, ""): {"merge"}} if pr.merged_by else {},
            )
        )
    return units


def link_chat_to_prs(repo: Repo, messages: list[tuple[str, str, str, str]]) -> list[ChatMsg]:
    """``messages``: (event_id, time, actor, text) already restricted to the repo's active window.
    Keeps those naming PR numbers of this repo ('PR #12', 'pull/12', '#152')."""
    out = []
    for eid, ts, actor, text in messages:
        nums = sorted({int(a or b) for a, b in PR_RX.findall(text)} & set(repo.prs))
        if nums:
            out.append(ChatMsg(eid, ts, actor, text, [pr_event_id(repo, n) for n in nums]))
    return out
