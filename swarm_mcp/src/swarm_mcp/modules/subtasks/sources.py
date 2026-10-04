"""Adapters: turn a dataset into ``Unit``s (work episodes) and linked ``ChatMsg``s for ``infer``.

Each adapter maps its dataset's notions onto the generic model:

    corpus     unit             action          artifact     actor
    git repo   pull request     commit          file         commit author (-> village agent)
    wiki       edit session     page revision   wiki page    editor label (or anon@<ip16>)

Add an adapter by writing a function that returns ``(units, chat, notes)`` and registering a
``Corpus`` for it in ``subtasks/__init__.py``.
"""

from __future__ import annotations

import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from swarm_mcp.modules.git import commit_event_id, pr_event_id
from swarm_mcp.modules.git.data import Repo
from swarm_mcp.modules.subtasks.infer import PR_RX, Action, Change, ChatMsg, Unit
from swarm_mcp.modules.wiki import page_event_id, revision_event_id, session_event_id
from swarm_mcp.modules.wiki.data import Wiki


@dataclass
class Corpus:
    name: str
    kind: str  # "git repo", "wiki"...
    unit_noun: str  # "pull request", "edit session"
    action_noun: str  # "commit", "revision"
    artifact_noun: str  # "file", "page"
    load: Callable[[], tuple[list[Unit], list[ChatMsg], list[str]]]
    notes: list[str] = field(default_factory=list)  # standing caveats shown with results
    tag_name: str | None = None  # what Unit.tags hold, e.g. "page_family (publishers' heuristic)"
    dup_min: float = 0.5  # similarity needed to call two units duplicates


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


# --------------------------------------------------------------------------- wiki

_WIKI_LINK = re.compile(r"[?&;]id=([^&\s#\)\]\"'<>]+)|\[\[([^\]|#]{1,120})")


def wiki_units(w: Wiki) -> list[Unit]:
    """One unit per edit session (an actor's revisions with gaps <= 30 min); actions are revisions, artifacts
    are pages. Links to other pages ('...wiki.cgi?id=Page', '[[Page]]') become refs to the session that
    created the linked page. Tags are the publishers' page_family of the pages touched."""
    by_name = {(p.wiki, p.name.replace(" ", "_").lower()): k for k, p in w.pages.items()}
    creator_session = {k: w.session_of[p.revisions[0]] for k, p in w.pages.items() if p.revisions}
    units = []
    for s in w.sessions.values():
        actions, refs, tags, names = [], set(), Counter(), Counter()
        for rid in s.revisions:
            r = w.revisions[rid]
            p = w.pages[r.page_key]
            names[p.name] += 1
            if p.family:
                tags[p.family] += 1
            change = Change(r.page_key, new=r.created, size=sum(len(x) for x in r.added), lines=r.added)
            actions.append(Action(revision_event_id(w, rid), s.actor, r.time, r.summary, [change]))
            for a, b in _WIKI_LINK.findall("\n".join(r.added)):
                target = urllib.parse.unquote_plus(a or b).strip().replace(" ", "_").lower()
                key = by_name.get((p.wiki, target))
                if key and key != r.page_key and creator_session.get(key) not in (None, s.sid):
                    refs.add(session_event_id(w, creator_session[key]))
        units.append(
            Unit(
                event_id=session_event_id(w, s.sid),
                short=f"session {s.sid}",
                title=" ".join(n for n in names) + " " + " ".join(a.text for a in actions),
                start=s.start,
                end=s.end,
                state="created page" if any(w.revisions[r].created for r in s.revisions) else "edited",
                completed=None,
                actions=actions,
                refs=refs,
                tags=dict(tags),
            )
        )
        units[-1].title = units[-1].title[:400]
        units[-1].short = f"session {s.sid}"
    return units


def wiki_page_ids(w: Wiki, keys: list[str]) -> list[str]:
    return [page_event_id(w, k) for k in keys]
