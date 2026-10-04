"""Infer subtasks (clusters of pull requests) and typed handoffs between agents. No MCP code here.

A PR is roughly one work episode (in the RPG week: median 1 commit, ~7 min open); a subtask is a
cluster of PRs. Five independent signals each give a PR-by-PR similarity matrix, and Louvain finds
communities in a sparsified graph of it:

    files  TF-IDF over the paths a PR touched (hub files like render.js count for little)
    code   TF-IDF over words in the code a PR *added*: identifiers split camelCase, import targets
    title  TF-IDF over the PR title and commit subjects
    chat   TF-IDF over chat messages that point at exactly this PR
    refs   explicit links only: '#N' in titles/commits, chat messages naming 2-4 PRs at once
    combined  a weighted blend of the five

Handoffs come from git, independently of clustering, between *different* agents (commit authors):

    builds_on   B edits a file A's PR created
    integrates  B adds an import of a module A created
    tests       B adds tests that import A's module (and touches only tests)
    fixes       builds_on/integrates where B's PR title starts with fix/revert/repair...
    resubmits   B's PR creates the same file A's earlier PR created (took over / re-opened A's work)
    duplicate   near-identical title+code, different authors, no link between them, not both merged

Hub files (touched by >8% of PRs and by at least 5, plus package.json/README/index.html/styles.css) never create
handoffs, and imports re-added by pasting an older copy of a file are ignored (see git.data).
"""

from __future__ import annotations

import collections
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import networkx as nx
import numpy as np

from swarm_mcp.modules.git.data import Repo

METHODS = ("combined", "code", "title", "files", "chat", "refs")
LEVELS = {"coarse": 1.0, "medium": 3.0, "fine": 6.0}  # Louvain resolution
BLEND = {"code": 0.35, "title": 0.25, "files": 0.15, "chat": 0.10, "refs": 0.15}
TAU = {"files": 0.2, "code": 0.2, "title": 0.2, "chat": 0.15, "refs": 0.3, "combined": 0.12}
K_NEIGHBOURS = 6
SEED = 7

METHOD_DESCRIPTIONS = {
    "combined": "weighted blend: code 35%, title 25%, files 15%, explicit refs 15%, chat 10%",
    "code": "words in the code each PR added (identifiers split camelCase, import targets)",
    "title": "PR title and commit subjects, generic words removed",
    "files": "paths each PR touched; hub files count for little",
    "chat": "chat messages that point at exactly one PR",
    "refs": "explicit '#N' links in titles/commits and chat messages naming 2-4 PRs",
}

# "PR 12", "PR #12", "pull/12", "#152" (bare "#n" only for n >= 10, to skip "#1 priority")
PR_RX = re.compile(r"(?:\bPRs?\s*#?|pull/)(\d{1,4})\b|(?<![\w&])#(\d{2,4})\b", re.I)
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
FIXRE = re.compile(r"^\s*(?:\w+(?:\([^)]*\))?:\s*)?(fix|hotfix|repair|revert|restore)\b|\brevert\b", re.I)
HUB_NAMES = {"package.json", "README.md", "index.html", "styles.css"}

JS_STOP = set(
    """const let var function return import export from default class this new null undefined true false if else
for while switch case break continue typeof instanceof await async try catch throw finally length push map filter
reduce foreach join slice concat includes keys values entries object array string number math floor round min max
console log document element innerhtml classname style div span button id data type html css px rem test tests
assert equal strict deep describe should expect node the and with that are not has get set""".split()
)
TXT_STOP = set(
    """feat fix fixes chore docs doc test tests add adds added adding update updates updated with and the for into
from to of in on a an by pr prs merge merged pull request requests new system systems module modules integration
integrate integrated integrating wire wired wiring implement implemented implementation support use using via all
its it this that is are be as or vs our we i you please review approve approved approval lgtm thanks thank looks
good can will now just also ready done check checked scan scanned clean egg eggs easter saboteur here there let lets
im ive ill should would could has have had was were been more some any no not yes ok okay main branch commit
commits rebase rebased conflict conflicts day issue issues game rpg comprehensive core basic initial""".split()
)


def words(s: str) -> list[str]:
    """camelCase / snake / kebab -> lowercase words of 3+ letters."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return [w for w in re.split(r"[^A-Za-z]+", s.lower()) if len(w) > 2]


def stem(w: str) -> str:
    for suf, rep in (("ies", "y"), ("ses", "s"), ("s", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + rep
    return w


def text_words(s: str) -> list[str]:
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"#\d+", " ", s)
    s = re.sub(r"^\s*\w+(\([^)]*\))?:\s*", "", s)  # conventional-commit prefix
    return [stem(w) for w in words(s) if w not in TXT_STOP]


def file_words(path: str) -> list[str]:
    base = re.sub(r"\.(m?js|ts|json|md|css|html)$", "", path.rsplit("/", 1)[-1])
    return [stem(w) for w in words(base) if w not in ("test", "tests", "index", "spec")]


def module_stem(path: str) -> str:
    return re.sub(r"\.(m?js|ts)$", "", path.rsplit("/", 1)[-1])


@dataclass
class ChatMsg:
    event_id: str
    time: str
    actor: str
    text: str
    prs: list[int]


@dataclass
class Edge:
    kind: str
    src: int  # PR index of the giver (A)
    dst: int  # PR index of the taker (B)
    giver: str
    taker: str
    files: list[str]
    giver_commits: list[str]  # shas
    taker_commits: list[str]
    score: float | None = None  # duplicates only


@dataclass
class Inference:
    repo: str
    prs: list[int]  # PR numbers; list index = PR index used everywhere below
    authors: list[str | None]  # main author (agent display name) per PR
    touch: list[dict[str, set[str]]]  # agent -> roles ("author", "commit", "merge") per PR
    terms: list[list[str]]  # top code terms per PR
    files: list[list[tuple[str, int]]]
    vec: dict[str, tuple[np.ndarray, dict[int, str]]]
    sim: dict[str, np.ndarray]
    refs_raw: np.ndarray
    clusters: dict[str, dict[str, list[list[int]]]]  # method -> level -> clusters (sorted by start)
    names: dict[str, dict[str, list[str]]]
    label_of: dict[str, dict[str, np.ndarray]]  # method -> level -> cluster index per PR
    agreement: dict[str, dict[str, float]]  # ARI between methods (medium level)
    edges: list[Edge]
    chat: list[ChatMsg]
    chat_of_pr: dict[int, list[int]]  # PR index -> indices into chat
    rewrites: list[list[str]]
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- similarity helpers


def _tfidf(bags: list[collections.Counter]) -> tuple[np.ndarray, dict[int, str]]:
    n = len(bags)
    vocab: dict[str, int] = {}
    rows = [{vocab.setdefault(w, len(vocab)): c for w, c in b.items()} for b in bags]
    df = np.zeros(len(vocab))
    for r in rows:
        for j in r:
            df[j] += 1
    idf = np.log((1 + n) / (1 + df)) + 1
    idf[df > 0.30 * n] *= 0.2  # terms in >30% of PRs carry ~no subtask signal
    X = np.zeros((n, len(vocab)))
    for i, r in enumerate(rows):
        for j, c in r.items():
            X[i, j] = (1 + math.log(c)) * idf[j]
    nrm = np.linalg.norm(X, axis=1, keepdims=True)
    nrm[nrm == 0] = 1
    return X / nrm, {j: w for w, j in vocab.items()}


def _cluster(S: np.ndarray, tau: float, res: float) -> list[list[int]]:
    n = len(S)
    G = nx.Graph()
    G.add_nodes_from(range(n))
    for i in range(n):
        for j in np.argsort(-S[i])[:K_NEIGHBOURS]:
            if S[i, j] >= tau:
                G.add_edge(i, int(j), weight=float(S[i, j]))
    comms = nx.community.louvain_communities(G, weight="weight", resolution=res, seed=SEED)
    return [sorted(c) for c in comms]


def _ari(a: np.ndarray, b: np.ndarray) -> float:
    ct = collections.Counter(zip(a.tolist(), b.tolist(), strict=True))

    def comb(x: int) -> float:
        return x * (x - 1) / 2

    s = sum(comb(v) for v in ct.values())
    sa = sum(comb(v) for v in collections.Counter(a.tolist()).values())
    sb = sum(comb(v) for v in collections.Counter(b.tolist()).values())
    e = sa * sb / comb(len(a)) if len(a) > 1 else 0
    return 0.0 if (sa + sb) / 2 == e else (s - e) / ((sa + sb) / 2 - e)


# --------------------------------------------------------------------------- main entry


def infer(repo: Repo, chat: list[ChatMsg], agent_of: Callable[[str, str], str]) -> Inference:
    """``chat``: messages already linked to PR numbers of this repo. ``agent_of(git_name, email)`` -> display name."""
    order = sorted(repo.prs.values(), key=lambda p: (p.start, p.number))
    prs = [p.number for p in order]
    idx = {n: i for i, n in enumerate(prs)}
    N = len(prs)

    # ---- per-PR features
    creator: dict[str, tuple[int, str, str, str]] = {}  # path -> (pr idx, agent, time, sha)
    F = []
    authors: list[str | None] = []
    touch: list[dict[str, set[str]]] = []
    for i, p in enumerate(order):
        files: collections.Counter = collections.Counter()
        created: set[str] = set()
        code: collections.Counter = collections.Counter()
        imports: dict[str, tuple[str, str]] = {}  # target -> (agent, sha)
        file_first: dict[str, tuple[str, str]] = {}  # path -> (agent, sha) of the PR's first commit touching it
        rewrites: set[str] = set()
        roles: dict[str, set[str]] = collections.defaultdict(set)
        counts: collections.Counter = collections.Counter()
        for sha in p.commits:
            c = repo.commits[sha]
            a = agent_of(c.author, c.email)
            roles[a].add("commit")
            counts[a] += 1
            for path, f in (c.files or {}).items():
                files[path] += f.added + f.removed
                file_first.setdefault(path, (a, sha))
                if f.rewrite:
                    rewrites.add(path)
                if f.new:
                    created.add(path)
                    if path not in creator or creator[path][2] > c.time:
                        creator[path] = (i, a, c.time, sha)
                for t in f.imports:
                    imports.setdefault(t, (a, sha))
                    for w in file_words(t):
                        code[w] += 3
                for line in f.lines:
                    for ident in IDENT.findall(line):
                        for w in words(ident):
                            if w not in JS_STOP:
                                code[stem(w)] += 1
                for w in file_words(path):
                    code[w] += 2
        author = counts.most_common(1)[0][0] if counts else None
        if author:
            roles[author].add("author")
        if p.merged_by:
            m = agent_of(p.merged_by, "")
            roles[m].add("merge")
        authors.append(author)
        touch.append(dict(roles))
        title_txt = " ".join([p.title] + [repo.commits[s].subject for s in p.commits])
        F.append(
            {
                "files": files,
                "created": created,
                "code": code,
                "imports": imports,
                "file_first": file_first,
                "rewrites": rewrites,
                "title": collections.Counter(text_words(title_txt)),
                "refs": {int(x) for x in re.findall(r"#(\d+)", title_txt)} - {p.number},
                "chat": collections.Counter(),
            }
        )

    # ---- chat
    chat_of_pr: dict[int, list[int]] = collections.defaultdict(list)
    comention: collections.Counter = collections.Counter()
    for k, msg in enumerate(chat):
        ix = sorted({idx[n] for n in msg.prs if n in idx})
        for i in ix:
            chat_of_pr[i].append(k)
        if len(ix) == 1:
            F[ix[0]]["chat"].update(text_words(msg.text))
        elif 2 <= len(ix) <= 4:
            for x in range(len(ix)):
                for y in range(x + 1, len(ix)):
                    comention[(ix[x], ix[y])] += 1

    # ---- similarity
    vec, sim = {}, {}
    for key in ("files", "code", "title", "chat"):
        X, inv = _tfidf([f[key] for f in F])
        vec[key] = (X, inv)
        S = X @ X.T
        np.fill_diagonal(S, 0)
        sim[key] = S
    R = np.zeros((N, N))
    for i, f in enumerate(F):
        for n in f["refs"]:
            if n in idx:
                R[i, idx[n]] += 3
                R[idx[n], i] += 3
    for (a, b), c in comention.items():
        R[a, b] += c
        R[b, a] += c
    sim["refs"] = R / (R + 2)  # saturating: one explicit ref -> .6
    sim["combined"] = sum(w * sim[k] for k, w in BLEND.items())

    # ---- clusters, sorted by first PR start so ids read chronologically
    clusters: dict[str, dict[str, list[list[int]]]] = {}
    label_of: dict[str, dict[str, np.ndarray]] = {}
    for m in METHODS:
        clusters[m], label_of[m] = {}, {}
        for lvl, res in LEVELS.items():
            cl = sorted(_cluster(sim[m], TAU[m], res), key=lambda c: (min(c), -len(c)))
            clusters[m][lvl] = cl
            lab = np.zeros(N, int)
            for ci, c in enumerate(cl):
                lab[c] = ci
            label_of[m][lvl] = lab
    agreement = {a: {b: round(_ari(label_of[a]["medium"], label_of[b]["medium"]), 3) for b in METHODS} for a in METHODS}

    # ---- names: distinctive terms across title, code and file names
    def name(c: list[int]) -> str:
        sc: collections.Counter = collections.Counter()
        for key, w in (("title", 1.0), ("code", 0.6), ("files", 0.4)):
            X, inv = vec[key]
            v = X[c].sum(0)
            for t in np.argsort(-v)[:8]:
                if v[t] > 0:
                    term = inv[t] if key != "files" else " ".join(file_words(inv[t]))
                    if term:
                        sc[term] += w * v[t]
        out: list[str] = []
        for t, _ in sc.most_common(12):
            if not any(t in o or o in t for o in out):
                out.append(t)
            if len(out) == 3:
                break
        return " · ".join(out)

    names = {
        m: {lvl: [name(c) if len(c) > 1 else order[c[0]].title[:60] for c in cl] for lvl, cl in clusters[m].items()}
        for m in METHODS
    }

    # ---- handoffs
    touch_ct = collections.Counter(path for f in F for path in f["files"])
    hub = {p for p, c in touch_ct.items() if c > max(0.08 * N, 4) or p.rsplit("/", 1)[-1] in HUB_NAMES}
    stem_of: dict[str, list[str]] = collections.defaultdict(list)
    for path in creator:
        stem_of[module_stem(path)].append(path)
    raw: dict[tuple[int, int, str], Edge] = {}

    def add(i: int, j: int, kind: str, path: str, a: str, b: str, sha_a: str, sha_b: str) -> None:
        e = raw.setdefault((i, j, kind), Edge(kind, i, j, a, b, [], [], []))
        if path not in e.files:
            e.files.append(path)
        if sha_a not in e.giver_commits:
            e.giver_commits.append(sha_a)
        if sha_b not in e.taker_commits:
            e.taker_commits.append(sha_b)

    for j, f in enumerate(F):
        for path in f["files"]:
            if path in creator and path not in hub:
                i, a, _, sha_a = creator[path]
                b, sha_b = f["file_first"][path]
                if i == j or a == b or order[i].start > order[j].start:
                    continue
                add(i, j, "resubmits" if path in f["created"] else "builds_on", path, a, b, sha_a, sha_b)
        only_tests = all(q.startswith("test") or q in hub for q in f["files"])
        for target, (b, sha_b) in f["imports"].items():
            for path in stem_of.get(module_stem(target), []):
                i, a, _, sha_a = creator[path]
                if i == j or a == b or order[i].start > order[j].start:
                    continue
                add(i, j, "tests" if only_tests else "integrates", path, a, b, sha_a, sha_b)
    edges: list[Edge] = []
    for (i, j, kind), e in raw.items():
        if kind == "builds_on" and any((i, j, k) in raw for k in ("integrates", "tests", "resubmits")):
            continue
        if kind in ("integrates", "builds_on") and (i, j, "resubmits") in raw:
            continue
        if kind in ("integrates", "builds_on") and FIXRE.search(order[j].title):
            e.kind = "fixes"
        e.files = e.files[:6]
        edges.append(e)
    linked = {(e.src, e.dst) for e in edges} | {(e.dst, e.src) for e in edges}
    dup = sim["title"] * 0.6 + sim["code"] * 0.4
    for i in range(N):
        for j in range(i + 1, N):
            if (
                dup[i, j] > 0.5
                and authors[i] != authors[j]
                and (i, j) not in linked
                and not (order[i].state != "unmerged" and order[j].state != "unmerged")
                and abs(_days(order[i].start, order[j].start)) < 3
            ):
                edges.append(
                    Edge(
                        "duplicate", i, j, authors[i] or "?", authors[j] or "?", [], [], [], round(float(dup[i, j]), 2)
                    )
                )
    edges.sort(key=lambda e: (order[e.dst].start, e.kind))

    return Inference(
        repo=repo.name,
        prs=prs,
        authors=authors,
        touch=touch,
        terms=[[t for t, _ in f["code"].most_common(8)] for f in F],
        files=[f["files"].most_common(8) for f in F],
        vec=vec,
        sim=sim,
        refs_raw=R,
        clusters=clusters,
        names=names,
        label_of=label_of,
        agreement=agreement,
        edges=edges,
        chat=chat,
        chat_of_pr=dict(chat_of_pr),
        rewrites=[sorted(f["rewrites"]) for f in F],
    )


def _days(a: str, b: str) -> float:
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    return (datetime.strptime(a, fmt) - datetime.strptime(b, fmt)).total_seconds() / 86400


def shared_terms(inf: Inference, key: str, i: int, j: int, n: int = 4) -> list[str]:
    X, inv = inf.vec[key]
    prod = X[i] * X[j]
    return [inv[int(t)] for t in np.argsort(-prod)[:n] if prod[t] > 0]


def why(inf: Inference, i: int, j: int) -> dict[str, dict]:
    """Per-signal similarity between two PRs, with the terms they share."""
    out: dict[str, dict] = {}
    for key in ("code", "title", "files", "chat"):
        s = float(inf.sim[key][i, j])
        if s > 0.05:
            out[key] = {"score": round(s, 2), "shared": shared_terms(inf, key, i, j)}
    if inf.refs_raw[i, j]:
        out["refs"] = {
            "score": round(float(inf.sim["refs"][i, j]), 2),
            "shared": [f"{int(inf.refs_raw[i, j])} link weight"],
        }
    return out


def cohesion(inf: Inference, members: list[int], method: str, level: str) -> dict[str, float]:
    """For each other method: the share of member pairs it also puts in one cluster (1 = kept whole)."""
    out = {}
    if len(members) < 2:
        return {m: 1.0 for m in METHODS if m != method}
    for m in METHODS:
        if m == method:
            continue
        lab = inf.label_of[m][level][members]
        same = sum(int(a == b) for x, a in enumerate(lab) for b in lab[x + 1 :])
        out[m] = round(same / (len(members) * (len(members) - 1) / 2), 2)
    return out
