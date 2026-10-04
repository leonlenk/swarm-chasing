"""Git repositories the agents worked in: pull requests and commits, as retrievable events.

Data: bare clones (with PR heads fetched) under ``$SWARM_DATA_DIR/<dataset>/repos/*.git`` or
``$SWARM_DATA_DIR/repos/*.git``; override with ``SWARM_GIT_DIR``. A repo loads on first use
(about 10 s for ~460 PRs) and is cached for the process.

Event ids: ``git:pr:<repo>#<number>`` and ``git:commit:<repo>@<sha>``. ``core_get`` on a PR
returns its commits as context; on a commit, the neighbouring commits of the same PR (or of main).
"""

from __future__ import annotations

import shutil
from typing import Annotated, Any, Literal

from pydantic import Field

from swarm_mcp.events import EventNotFound, event_record
from swarm_mcp.modules.git.data import PullRequest, Repo, file_stats, find_repos, load_commit, load_repo
from swarm_mcp.toolkit import ToolInputError, iso, parse_time, truncate

NAME = "git"
DESCRIPTION = (
    "Git repos the agents built (bare clones with PR refs): list repos and pull requests; PRs and commits are "
    "event ids (git:pr:<repo>#N, git:commit:<repo>@<sha>) that core_get expands. PR state is inferred from "
    "main's history (merged / landed / unmerged)."
)


def repo_paths(ctx) -> dict:
    return find_repos(ctx.data_dir, ctx.setting("dir"))


def requires(ctx) -> list[str]:
    if shutil.which("git") is None:
        return ["git executable not found on PATH"]
    if not repo_paths(ctx):
        return [f"no bare git repos found under {ctx.data_dir}/*/repos/ or {ctx.data_dir}/repos/ (set SWARM_GIT_DIR)"]
    return []


def get_repo(ctx, name: str | None) -> Repo:
    """Load (once) and return a repo; ``name`` may be omitted when there is only one. Shared with other modules."""
    paths = find_repos(ctx.config.data_dir, ctx.config.module_setting("git", "dir"))
    if not paths:
        raise ToolInputError("No git repositories are available.")
    if name is None:
        if len(paths) > 1:
            raise ToolInputError(f"Several repos are loaded; pass repo= one of: {', '.join(sorted(paths))}.")
        name = next(iter(paths))
    if name not in paths:
        raise ToolInputError(f"Unknown repo {name!r}. Repos: {', '.join(sorted(paths))}.")
    return ctx.cache.get(f"git:repo:{name}", lambda: load_repo(name, paths[name]))


def pr_event_id(repo: Repo, n: int) -> str:
    return f"git:pr:{repo.name}#{n}"


def commit_event_id(repo: Repo, sha: str) -> str:
    return f"git:commit:{repo.name}@{sha[:12]}"


def register(mcp, ctx) -> None:
    def pr_record(repo: Repo, pr: PullRequest, max_chars: int) -> dict[str, Any]:
        lines = [pr.title, "", f"state: {pr.state}" + (f" ({pr.merge_kind})" if pr.merge_kind else "")]
        if pr.merged_by:
            lines.append(f"merged by: {pr.merged_by}")
        lines.append(f"commits ({len(pr.commits)}{', older ones omitted' if pr.truncated else ''}):")
        lines += [f"  {repo.commits[s].author}: {repo.commits[s].subject}" for s in pr.commits]
        text, cut = truncate(ctx.scrub("\n".join(lines)), max_chars)
        return event_record(
            pr_event_id(repo, pr.number),
            time=iso(pr.start),
            actor=pr.author,
            actor_type="git_author",
            location=repo.name,
            text=text,
            truncated=cut,
            number=pr.number,
            title=pr.title,
            state=pr.state,
            merge_kind=pr.merge_kind,
            merged_at=iso(pr.merged_at),
            merged_by=pr.merged_by,
            authors=pr.authors,
            commit_count=len(pr.commits),
        )

    def commit_record(repo: Repo, sha: str, max_chars: int) -> dict[str, Any]:
        c = load_commit(repo, sha)
        if c is None:
            raise EventNotFound(sha)
        stats = file_stats(repo, c.sha)
        body = "\n".join(
            [c.subject, *([""] + [c.body] if c.body else []), "", "files:"]
            + [f"  {p} (+{a} -{d})" for p, a, d in stats[:40]]
            + ([f"  … {len(stats) - 40} more files"] if len(stats) > 40 else [])
        )
        text, cut = truncate(ctx.scrub(body), max_chars)
        return event_record(
            commit_event_id(repo, c.sha),
            time=iso(c.time),
            actor=c.author,
            actor_type="git_author",
            location=repo.name,
            text=text,
            truncated=cut,
            sha=c.sha,
            on_main=c.sha in repo.main_set or None,
            prs=[pr_event_id(repo, n) for n in repo.pr_of_commit.get(c.sha, [])] or None,
        )

    @ctx.event_source(
        kinds={
            "pr": "a pull request, id '<repo>#<number>'; context = its commits, oldest first",
            "commit": "a commit, id '<repo>@<sha>'; context = neighbouring commits in the same PR, else on main",
        },
        description="Git repositories (bare clones with PR refs).",
    )
    def resolve(kind: str, local_id: str, *, before: int, after: int, max_chars: int) -> dict[str, Any]:
        sep = "#" if kind == "pr" else "@"
        name, _, ref = local_id.rpartition(sep)
        if not name or not ref:
            raise ToolInputError(
                f"git {kind} ids look like '{name or '<repo>'}{sep}<{'number' if kind == 'pr' else 'sha'}>'"
            )
        try:
            repo = get_repo(ctx, name)
        except ToolInputError:
            raise EventNotFound(local_id) from None
        if kind == "pr":
            if not ref.isdigit() or int(ref) not in repo.prs:
                raise EventNotFound(local_id)
            pr = repo.prs[int(ref)]
            commits = pr.commits[: before + after] if (before or after) else []
            return {
                "event": pr_record(repo, pr, max_chars),
                "before": [],
                "after": [commit_record(repo, s, max_chars) for s in commits],
                "context": f"the PR's commits, oldest first (up to before+after={before + after})",
            }
        c = load_commit(repo, ref)
        if c is None:
            raise EventNotFound(local_id)
        prs = repo.pr_of_commit.get(c.sha, [])
        if prs:
            seq, where = repo.prs[prs[0]].commits, f"commits of PR #{prs[0]}"
        elif c.sha in repo.main_set:
            seq, where = repo.main_line, f"first-parent history of {repo.main}"
        else:
            seq, where = [c.sha], "none (commit is not on main or in a PR)"
        i = seq.index(c.sha)
        return {
            "event": commit_record(repo, c.sha, max_chars),
            "before": [commit_record(repo, s, max_chars) for s in seq[max(0, i - before) : i]],
            "after": [commit_record(repo, s, max_chars) for s in seq[i + 1 : i + 1 + after]],
            "context": where,
        }

    @ctx.tool()
    def repos() -> dict[str, Any]:
        """List the git repositories available, with PR counts by inferred state once a repo has been loaded."""
        out = []
        for name, path in sorted(repo_paths(ctx).items()):
            row: dict[str, Any] = {"repo": name, "path": str(path)}
            if f"git:repo:{name}" in ctx.cache:
                r = get_repo(ctx, name)
                states: dict[str, int] = {}
                for p in r.prs.values():
                    states[p.state] = states.get(p.state, 0) + 1
                row.update(prs=len(r.prs), pr_states=states, main=r.main, main_commits=len(r.main_line))
            out.append(row)
        return {
            "repos": out,
            "notes": ["PR counts appear after a repo's first use (git_prs or any git event id loads it, ~10 s)"],
        }

    @ctx.tool()
    def prs(
        repo: Annotated[
            str | None, Field(description="Repo name (from git_repos); optional if there is only one.")
        ] = None,
        query: Annotated[
            str | None, Field(description="Case-insensitive substring of the PR title or commit subjects.")
        ] = None,
        author: Annotated[
            str | None, Field(description="Substring of a commit author name (git author, e.g. 'Opus 4.6').")
        ] = None,
        state: Annotated[Literal["merged", "landed", "unmerged"] | None, Field(description="Inferred state.")] = None,
        since: Annotated[str | None, Field(description="PR started at/after this ISO date/datetime (UTC).")] = None,
        until: Annotated[str | None, Field(description="PR started before this ISO date/datetime (UTC).")] = None,
        limit: Annotated[int, Field(description="Max PRs (default 20, max 200).")] = 20,
        offset: Annotated[int, Field(description="Skip this many (paging).", ge=0)] = 0,
    ) -> dict[str, Any]:
        """List pull requests in a repo, oldest first, with event ids, title, main author, inferred state and
        commit count. Expand one with core_get to see its commits."""
        r = get_repo(ctx, repo)
        lim, note = ctx.limit(limit)
        s, u = parse_time(since, field="since"), parse_time(until, end=True, field="until")
        q, a = (query or "").lower(), (author or "").lower()
        rows = []
        for pr in sorted(r.prs.values(), key=lambda p: (p.start, p.number)):
            if state and pr.state != state:
                continue
            if (s and pr.start < s) or (u and pr.start >= u):
                continue
            if a and not any(a in x.lower() for x in pr.authors):
                continue
            if q and q not in pr.title.lower() and not any(q in r.commits[c].subject.lower() for c in pr.commits):
                continue
            rows.append(pr)
        page = rows[offset : offset + lim]
        return {
            "repo": r.name,
            "total_matches": len(rows),
            "returned": len(page),
            "has_more": offset + len(page) < len(rows),
            "prs": [
                {
                    "event_id": pr_event_id(r, p.number),
                    "number": p.number,
                    "title": ctx.scrub(p.title),
                    "author": p.author,
                    "state": p.state,
                    "start": iso(p.start),
                    "merged_at": iso(p.merged_at),
                    "commits": len(p.commits),
                }
                for p in page
            ],
            "notes": [
                n
                for n in (
                    note,
                    "start = first commit's author time (the PR may have been opened later); "
                    "state is inferred from main's history, so open vs closed is unknown",
                )
                if n
            ],
        }
