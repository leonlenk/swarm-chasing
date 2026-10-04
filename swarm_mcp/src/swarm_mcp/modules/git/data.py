"""Read a bare git clone (with GitHub PR refs) into commits, diffs and pull requests.

The clone is expected to carry every PR head, so squash-merged and closed PRs keep their commits:

    git clone --bare https://github.com/<org>/<repo> <data>/<dataset>/repos/<repo>.git
    git -C <repo>.git fetch origin '+refs/pull/*/head:refs/pull/*/head'

Git knows commits, not GitHub state, so a PR's status is inferred from the history of the main
branch: ``merged`` (a merge commit, a squash commit ending in "(#N)", or a fast-forward), ``landed``
(its commits reached main inside another merge, or its changes did under other commits: rebase or
cherry-pick, matched by patch id) or
``unmerged`` (open or closed: git cannot tell which). A PR's ``start`` is its first commit's author
time, which can precede the PR being opened.
"""

from __future__ import annotations

import collections
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from swarm_mcp.toolkit import TS_FORMAT

log = logging.getLogger("swarm_mcp.git")

MAX_PR_COMMITS = 80  # commits kept per PR (newest)
MAX_ADDED_LINES = 600  # added lines kept per file per commit (for content-based signals)
SKIP_FILE = re.compile(r"(package-lock\.json|yarn\.lock|\.min\.js|\.(png|jpe?g|gif|svg|ico|webp|mp3|wav|ogg))$")
IMPORT = re.compile(r"""(?:import\b[^'"]*?from\s*|import\s*\(\s*|require\s*\(\s*)['"]([^'"]+)['"]""")
_SEP, _END = "\x1f", "\x1e"


# --------------------------------------------------------------------------- records


@dataclass
class FileChange:
    path: str
    new: bool = False
    deleted: bool = False
    added: int = 0
    removed: int = 0
    lines: list[str] = field(default_factory=list)  # net-new added lines (not re-adds of removed ones)
    imports: list[str] = field(default_factory=list)  # import targets added, not present in the parent version
    rewrite: bool = False  # big removal, or several imports swapped out and in: likely a pasted older copy


@dataclass
class Commit:
    sha: str
    author: str
    email: str
    time: str  # author time, dataset format (UTC)
    committed: str
    subject: str
    body: str
    parents: list[str]
    files: dict[str, FileChange] | None = None  # filled for PR commits (diffs are parsed on load)


@dataclass
class PullRequest:
    number: int
    head: str
    commits: list[str]  # oldest first
    title: str
    state: str  # merged | landed | unmerged
    merge_kind: str | None  # merge | squash | fast-forward | via-other | rebase | None
    merge_sha: str | None
    merged_at: str | None
    merged_by: str | None  # author of the merge commit (GitHub's merge commits are authored by whoever merged)
    start: str
    end: str | None
    truncated: bool = False
    authors: dict[str, int] = field(default_factory=dict)  # git author name -> commits in this PR

    @property
    def author(self) -> str | None:
        """Who wrote most of the PR's commits."""
        return max(self.authors, key=lambda a: self.authors[a]) if self.authors else None


@dataclass
class Repo:
    name: str
    path: Path
    main: str
    commits: dict[str, Commit]
    prs: dict[int, PullRequest]
    main_line: list[str]  # first-parent history of main, oldest first
    pr_of_commit: dict[str, list[int]]
    main_set: set[str]
    by_prefix: dict[str, str]  # 12-char prefix -> full sha

    def find_commit(self, ref: str) -> Commit | None:
        ref = ref.lower()
        if ref in self.commits:
            return self.commits[ref]
        full = self.by_prefix.get(ref[:12]) if len(ref) >= 7 else None
        if full and full.startswith(ref):
            return self.commits[full]
        if len(ref) >= 7:
            hits = [s for s in self.commits if s.startswith(ref)]
            if len(hits) == 1:
                return self.commits[hits[0]]
        return None


# --------------------------------------------------------------------------- discovery


def find_repos(data_dir: Path, override: str | None = None) -> dict[str, Path]:
    """Bare clones under ``<data>/repos/*.git`` and ``<data>/<dataset>/repos/*.git`` (or ``override``)."""
    roots = [Path(override).expanduser()] if override else [data_dir / "repos", *sorted(data_dir.glob("*/repos"))]
    out: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.iterdir()):
            if p.is_dir() and ((p / "HEAD").exists() or (p / ".git").exists()):
                out.setdefault(p.name.removesuffix(".git"), p)
    return out


# --------------------------------------------------------------------------- git plumbing


def _git(path: Path, *args: str, input: str | None = None) -> str:
    res = subprocess.run(["git", "-C", str(path), *args], input=input, capture_output=True, text=True, errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {res.stderr.strip()[:300]}")
    return res.stdout


def _ts(iso_with_tz: str) -> str:
    return datetime.fromisoformat(iso_with_tz).astimezone(timezone.utc).strftime(TS_FORMAT)


_FMT = "%H%x1f%an%x1f%ae%x1f%aI%x1f%cI%x1f%P%x1f%s%x1f%b%x1e"


def _parse_log(text: str) -> list[Commit]:
    out = []
    for rec in text.split(_END):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, an, ae, ai, ci, parents, subj, body = rec.split(_SEP)
        out.append(Commit(sha, an, ae, _ts(ai), _ts(ci), subj, body.strip(), parents.split()))
    return out


def _main_branch(path: Path) -> str:
    for ref in ("main", "master"):
        if (
            subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "-q", ref], capture_output=True).returncode
            == 0
        ):
            return ref
    return "HEAD"


def _is_ancestor(path: Path, a: str, b: str) -> bool:
    return subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor", a, b]).returncode == 0


def _landing(path: Path, sha: str, main_line: list[Commit]) -> Commit:
    """The first commit on main's first-parent line that contains ``sha`` (binary search)."""
    lo, hi = 0, len(main_line) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if _is_ancestor(path, sha, main_line[mid].sha):
            hi = mid
        else:
            lo = mid + 1
    return main_line[lo]


def _patch_ids(path: Path, shas: list[str]) -> dict[str, str]:
    """sha -> stable patch id (content fingerprint that survives rebases)."""
    out: dict[str, str] = {}
    for i in range(0, len(shas), 200):
        shown = _git(path, "show", "--no-color", "--format=commit %H", *shas[i : i + 200])
        pid = subprocess.run(
            ["git", "-C", str(path), "patch-id", "--stable"], input=shown, capture_output=True, text=True
        ).stdout
        for line in pid.splitlines():
            p, s = line.split()
            out[s] = p
    return out


def _parse_diffs(text: str) -> dict[str, dict[str, FileChange]]:
    commits: dict[str, dict[str, FileChange]] = {}
    removed: dict[tuple[str, str], list[str]] = {}
    cur: dict[str, FileChange] | None = None
    sha = ""
    f: FileChange | None = None
    for line in text.split("\n"):
        if line.startswith("\x00C "):
            sha = line.split()[1]
            cur = commits.setdefault(sha, {})
            f = None
            continue
        if cur is None:
            continue
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1]
            f = None if SKIP_FILE.search(path) else cur.setdefault(path, FileChange(path))
        elif f is None:
            continue
        elif line.startswith("new file mode"):
            f.new = True
        elif line.startswith("deleted file mode"):
            f.deleted = True
        elif line.startswith("+") and not line.startswith("+++"):
            f.added += 1
            if len(f.lines) < MAX_ADDED_LINES:
                f.lines.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            f.removed += 1
            rl = removed.setdefault((sha, f.path), [])
            if len(rl) < MAX_ADDED_LINES:
                rl.append(line[1:])
    for (s, p), rl in removed.items():  # a rewrite removes and re-adds lines: keep only the net-new ones
        fc = commits[s][p]
        gone = collections.Counter(x.strip() for x in rl)
        keep = []
        for x in fc.lines:
            if gone[x.strip()] > 0:
                gone[x.strip()] -= 1
            else:
                keep.append(x)
        fc.lines = keep
        n_out = sum(bool(IMPORT.search(x)) for x in rl)
        n_in = sum(bool(IMPORT.search(x)) for x in fc.lines)
        fc.rewrite = (fc.removed >= 40 and fc.removed >= 0.5 * fc.added) or (n_out >= 2 and n_in >= 2)
    return commits


def _parent_imports(path: Path, specs: list[tuple[str, str]]) -> dict[tuple[str, str], set[str]]:
    """Import targets already present in the parent version of each (sha, file)."""
    if not specs:
        return {}
    blob = subprocess.run(
        ["git", "-C", str(path), "cat-file", "--batch"],
        input="".join(f"{s}^:{p}\n" for s, p in specs).encode(),
        capture_output=True,
    ).stdout
    out: dict[tuple[str, str], set[str]] = {}
    pos = 0
    for spec in specs:
        nl = blob.index(b"\n", pos)
        head = blob[pos:nl].split()
        if len(head) == 3 and head[1] == b"blob":
            size = int(head[2])
            body = blob[nl + 1 : nl + 1 + size].decode("utf-8", "replace")
            out[spec] = {m.group(1) for m in IMPORT.finditer(body)}
            pos = nl + 1 + size + 1
        else:
            pos = nl + 1
    return out


# --------------------------------------------------------------------------- load


def load_repo(name: str, path: Path) -> Repo:
    main = _main_branch(path)
    main_line = _parse_log(_git(path, "log", "--first-parent", "--reverse", f"--format={_FMT}", main))
    commits = {c.sha: c for c in main_line}
    main_set = set(commits)
    main_idx = {c.sha: i for i, c in enumerate(main_line)}

    heads: dict[int, str] = {}
    for line in _git(path, "for-each-ref", "--format=%(refname) %(objectname)", "refs/pull").splitlines():
        ref, sha = line.split()
        parts = ref.split("/")
        if len(parts) >= 4 and parts[3] == "head" and parts[2].isdigit():
            heads[int(parts[2])] = sha

    # how each PR reached main, from main's first-parent history
    merged: dict[int, tuple[str, str]] = {}  # pr -> (kind, main sha)
    for c in main_line:
        m = re.match(r"Merge pull request #(\d+) ", c.subject)
        if m:
            merged.setdefault(int(m.group(1)), ("merge", c.sha))
            continue
        m = re.search(r"\(#(\d+)\)$", c.subject)
        if m:
            merged.setdefault(int(m.group(1)), ("squash", c.sha))
    for n, h in heads.items():
        if n not in merged and h in main_set:
            merged[n] = ("fast-forward", h)
    other_heads = {h: n for n, h in heads.items()}

    pr_shas: dict[int, list[str]] = {}
    for n, h in heads.items():
        kind, at = merged.get(n, (None, None))
        if kind in ("merge", "squash") and commits[at].parents:
            base = commits[at].parents[0]
            shas = _git(path, "rev-list", "--reverse", "--no-merges", h, f"^{base}").split()
        elif kind == "fast-forward":
            # the head sits on main: take it and the run of same-author commits just before it
            i = main_idx[h]
            shas = [h]
            while i > 0 and len(shas) < 20:
                prev = main_line[i - 1]
                if len(prev.parents) > 1 or prev.sha in other_heads or prev.email != main_line[main_idx[h]].email:
                    break
                shas.insert(0, prev.sha)
                i -= 1
        elif _is_ancestor(path, h, main):
            # reached main inside some other merge (e.g. merged into another PR's branch first)
            land = _landing(path, h, main_line)
            merged[n] = ("via-other", land.sha)
            base = land.parents[0] if land.parents else None
            shas = _git(path, "rev-list", "--reverse", "--no-merges", h, *([f"^{base}"] if base else [])).split()
        else:
            mb = _git(path, "merge-base", h, main).strip()
            shas = _git(path, "rev-list", "--reverse", "--no-merges", h, f"^{mb}").split() if mb else [h]
        pr_shas[n] = shas

    # metadata for every PR commit not already loaded
    need = sorted({s for shas in pr_shas.values() for s in shas} - set(commits))
    for i in range(0, len(need), 200):
        for c in _parse_log(_git(path, "show", "-s", f"--format={_FMT}", *need[i : i + 200])):
            commits[c.sha] = c

    # rebased / cherry-picked PRs: their patches show up on main under other shas
    unmerged = [n for n in heads if n not in merged and pr_shas[n]]
    if unmerged:
        main_plain = [c.sha for c in main_line if len(c.parents) == 1]
        main_pids = set(_patch_ids(path, main_plain).values())
        pr_pids = _patch_ids(path, sorted({s for n in unmerged for s in pr_shas[n]}))
        for n in unmerged:
            if any(pr_pids.get(s) in main_pids for s in pr_shas[n]):
                merged[n] = ("rebase", "")

    # diffs for PR commits
    diff_shas = sorted({s for shas in pr_shas.values() for s in shas[-MAX_PR_COMMITS:]})
    for i in range(0, len(diff_shas), 80):
        parsed = _parse_diffs(
            _git(path, "show", "-U0", "--no-color", "--no-renames", "--format=%x00C %H", *diff_shas[i : i + 80])
        )
        for sha, files in parsed.items():
            commits[sha].files = files
    specs = [
        (sha, p)
        for sha in diff_shas
        for p, fc in (commits[sha].files or {}).items()
        if not fc.new and any(IMPORT.search(x) for x in fc.lines)
    ]
    parent_imp = _parent_imports(path, specs)
    for sha in diff_shas:
        for p, fc in (commits[sha].files or {}).items():
            if fc.rewrite:
                continue
            before = parent_imp.get((sha, p), set())
            seen: list[str] = []
            for x in fc.lines:
                for m in IMPORT.finditer(x):
                    if m.group(1) not in before and m.group(1) not in seen:
                        seen.append(m.group(1))
            fc.imports = seen

    prs: dict[int, PullRequest] = {}
    pr_of_commit: dict[str, list[int]] = collections.defaultdict(list)
    for n in sorted(heads):
        shas = pr_shas[n]
        kind, at = merged.get(n, (None, None))
        mc = commits.get(at) if at else None
        if kind == "merge" and mc:
            title = (mc.body.split("\n")[0] if mc.body else "") or (commits[shas[-1]].subject if shas else "")
        elif kind == "squash" and mc:
            title = re.sub(r"\s*\(#\d+\)$", "", mc.subject)
        else:
            title = (
                commits[shas[-1]].subject
                if shas
                else commits.get(heads[n], Commit("", "", "", "", "", "", "", [])).subject
            )
        state = "merged" if kind in ("merge", "squash", "fast-forward") else "landed" if kind else "unmerged"
        kept = shas[-MAX_PR_COMMITS:]
        times = [commits[s].time for s in kept]
        pr = PullRequest(
            number=n,
            head=heads[n],
            commits=kept,
            title=title[:200],
            state=state,
            merge_kind=kind,
            merge_sha=mc.sha if mc else None,
            merged_at=mc.committed if mc else None,
            merged_by=mc.author if (mc and kind in ("merge", "via-other") and len(mc.parents) > 1) else None,
            start=min(times) if times else (mc.time if mc else ""),
            end=mc.committed if mc else None,
            truncated=len(shas) > MAX_PR_COMMITS,
        )
        pr.authors = dict(collections.Counter(commits[s].author for s in kept).most_common())
        prs[n] = pr
        for s in kept:
            pr_of_commit[s].append(n)
    log.info(
        "git %s: %d commits loaded, %d PRs (%s)",
        name,
        len(commits),
        len(prs),
        dict(collections.Counter(p.state for p in prs.values())),
    )
    return Repo(
        name=name,
        path=path,
        main=main,
        commits=commits,
        prs=prs,
        main_line=[c.sha for c in main_line],
        pr_of_commit=dict(pr_of_commit),
        main_set=set(c.sha for c in main_line),
        by_prefix={s[:12]: s for s in commits},
    )


def load_commit(repo: Repo, ref: str) -> Commit | None:
    """A commit by sha prefix, from the loaded set or straight from git (any commit in the repo)."""
    c = repo.find_commit(ref)
    if c is not None:
        return c
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        return None
    try:
        got = _parse_log(_git(repo.path, "show", "-s", f"--format={_FMT}", ref))
    except RuntimeError:
        return None
    return got[0] if got else None


def file_stats(repo: Repo, sha: str) -> list[tuple[str, int, int]]:
    """(path, added, removed) for a commit, from git (works for commits whose diff was not preloaded)."""
    c = repo.commits.get(sha)
    if c is not None and c.files is not None:
        return [(p, f.added, f.removed) for p, f in c.files.items()]
    out = []
    for line in _git(repo.path, "show", "--numstat", "--format=", sha).splitlines():
        a, d, p = line.split("\t", 2)
        out.append((p, int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0))
    return out
