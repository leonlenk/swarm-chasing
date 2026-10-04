"""Git repository adapter: a bare clone with GitHub PR heads fetched.

    git clone --bare https://github.com/<org>/<repo> data/<dataset>/repos/<repo>.git
    git -C <repo>.git fetch origin '+refs/pull/*/head:refs/pull/*/head'
    swarm-mcp add data/<dataset>/repos/<repo>.git --adapter git   # source defaults to the repo name (--name)

Mapping (source = repo name, e.g. ``rpg-game``):
  commit authors -> agents    (agent_id = <src>:agent:<email local part for agentvillage.org addresses,
                                 else a short hash of the email>; aliases = every name used + local part)
  commits        -> actions   (kind 'commit' or 'merge'; <src>:event:<sha12>; content = message;
                                 run_id = the first PR containing it; meta: sha, parents, prs, files)
  pull requests  -> periods   (kind 'pull_request'; <src>:period:pr-<n>; label = title; meta: number,
                                 short 'PR #n', state merged / landed / unmerged, completed, roles
                                 {merger: ['merge']}, members = its commits)
  files          -> artifacts (kind 'file'; meta.role 'test' for test files, meta.hub for package.json,
                                 README.md, index.html, styles.css)
  file changes   -> touches   (create / modify / delete; meta: added, removed, net-new lines, new imports,
                                 rewrite = looks like a pasted older copy)

PR state comes from main's history (git cannot tell open from closed). A PR's start is its first
commit's author time, which can precede the PR being opened.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from swarm_mcp.scope.adapters.gitdata import load_repo
from swarm_mcp.toolkit import TS_FORMAT

HUB_NAMES = {"package.json", "package-lock.json", "README.md", "index.html", "styles.css"}
MAX_LINES = 300  # net-new lines kept per file change (they feed content-based subtask signals)
NOTES = [
    "pull request state is inferred from main's history: merged, landed (reached main another way) or "
    "unmerged (open or closed: git cannot tell)",
    "a pull request's start is its first commit's author time; reviews and comments are not in git",
    "commit authors are git identities; link them to other sources' agents by name/alias",
]


def _dt(ts: str | None) -> datetime | None:
    return datetime.strptime(ts, TS_FORMAT) if ts else None


def _role(path: str) -> str | None:
    p = path.lower()
    if p.startswith(("test/", "tests/", "__tests__/")) or ".test." in p or ".spec." in p or "/tests/" in p:
        return "test"
    return None


class GitRepoAdapter:
    name = "git"

    def __init__(self, source: str | None = None):
        self.source = source or "git"
        self._explicit = source is not None

    def _source_for(self, path: Path) -> str:
        return self.source if self._explicit else Path(path).name.removesuffix(".git")

    def inspect(self, path: Path) -> dict[str, Any]:
        path = Path(path)
        refs = subprocess.run(
            ["git", "-C", str(path), "for-each-ref", "--format=%(refname)"], capture_output=True, text=True
        )
        if refs.returncode != 0:
            raise FileNotFoundError(f"not a git repository: {path}")
        names = refs.stdout.split()
        return {
            "adapter": self.name,
            "path": str(path),
            "source": self._source_for(path),
            "branches": [r for r in names if r.startswith("refs/heads/")][:20],
            "pull_request_heads": sum(1 for r in names if r.startswith("refs/pull/")),
        }

    def load(self, path: Path, **_: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        path = Path(path)
        src = self.source = self._source_for(path)
        repo = load_repo(src, path)

        # ---- agents: one per email
        names: dict[str, Counter] = {}
        for c in repo.commits.values():
            names.setdefault(c.email.lower(), Counter())[c.author] += 1

        def agent_key(email: str) -> str:
            e = email.lower()
            if e.endswith("@agentvillage.org"):
                return e.split("@", 1)[0]
            return "h" + hashlib.sha1(e.encode()).hexdigest()[:10]

        def agent_id(email: str) -> str:
            return f"{src}:agent:{agent_key(email)}"

        first_last: dict[str, list[datetime]] = {}
        for c in repo.commits.values():
            first_last.setdefault(c.email.lower(), []).append(_dt(c.time))
        for email, cnt in names.items():
            aliases = sorted(set(cnt))
            if email.endswith("@agentvillage.org"):
                aliases.append(email.split("@", 1)[0])
            ts = first_last[email]
            yield (
                "agents",
                {
                    "agent_id": agent_id(email),
                    "source": src,
                    "display_name": cnt.most_common(1)[0][0],
                    "aliases": sorted(set(aliases)),
                    "first_seen": min(ts),
                    "last_seen": max(ts),
                    "meta": {"commits": sum(cnt.values()), "kind": "git_author"},
                },
            )
        by_name = {}
        for c in repo.commits.values():
            by_name.setdefault(c.author, c.email)

        def pr_id(n: int) -> str:
            return f"{src}:period:pr-{n}"

        def commit_id(sha: str) -> str:
            return f"{src}:event:{sha[:12]}"

        # ---- pull requests
        for pr in repo.prs.values():
            last = max((repo.commits[s].time for s in pr.commits), default=pr.start)
            merger = agent_id(by_name[pr.merged_by]) if pr.merged_by in by_name else None
            yield (
                "periods",
                {
                    "evidence_id": pr_id(pr.number),
                    "source": src,
                    "kind": "pull_request",
                    "label": pr.title,
                    "start_ts": _dt(pr.start),
                    "end_ts": _dt(pr.end or last),
                    "meta": {
                        "number": pr.number,
                        "short": f"PR #{pr.number}",
                        "state": pr.state,
                        "completed": pr.state != "unmerged",
                        "merge_kind": pr.merge_kind,
                        "merged_by": merger,
                        "merged_at": pr.merged_at,
                        "roles": {merger: ["merge"]} if merger else {},
                        "members": [commit_id(s) for s in pr.commits],
                        "members_truncated": pr.truncated,
                        "end_is": "merge time" if pr.end else "last commit time",
                    },
                },
            )

        # ---- commits, files, file changes
        seen_files: dict[str, str] = {}
        for sha, c in repo.commits.items():
            prs = repo.pr_of_commit.get(sha, [])
            seq = repo.prs[prs[0]].commits.index(sha) if prs else None
            files = c.files or {}
            yield (
                "actions",
                {
                    "evidence_id": commit_id(sha),
                    "source": src,
                    "agent_id": agent_id(c.email),
                    "run_id": pr_id(prs[0]) if prs else None,
                    "seq": seq,
                    "ts": _dt(c.time),
                    "ts_quality": "exact",
                    "kind": "merge" if len(c.parents) > 1 else "commit",
                    "content": (c.subject + ("\n\n" + c.body if c.body else "")).strip(),
                    "meta": {
                        "sha": sha,
                        "parents": c.parents,
                        "prs": [pr_id(n) for n in prs],
                        "on_main": sha in repo.main_set,
                        "committed": c.committed,
                        "files": [{"path": p, "added": f.added, "removed": f.removed} for p, f in files.items()],
                        "diff_loaded": c.files is not None,
                    },
                },
            )
            for p, f in files.items():
                aid = f"{src}:artifact:{p}"
                if p not in seen_files:
                    seen_files[p] = aid
                    meta: dict[str, Any] = {}
                    if _role(p):
                        meta["role"] = _role(p)
                    if p.rsplit("/", 1)[-1] in HUB_NAMES:
                        meta["hub"] = True
                    yield "artifacts", {"artifact_id": aid, "source": src, "kind": "file", "name": p, "meta": meta}
                op = "create" if f.new else "delete" if f.deleted else "modify"
                yield (
                    "touches",
                    {
                        "touch_id": f"{commit_id(sha)}|{op}|{aid}",
                        "source": src,
                        "record_id": commit_id(sha),
                        "artifact_id": aid,
                        "op": op,
                        "ts": _dt(c.time),
                        "meta": {
                            "added": f.added,
                            "removed": f.removed,
                            "lines": f.lines[:MAX_LINES],
                            "imports": f.imports,
                            "rewrite": f.rewrite,
                        },
                    },
                )

    notes = NOTES
