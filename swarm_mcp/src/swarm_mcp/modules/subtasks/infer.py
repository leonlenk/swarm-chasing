"""Infer subtasks (clusters of work units) and typed handoffs between actors. No MCP code here.

The input is dataset-agnostic: a list of ``Unit``s (one episode of work: a pull request, an agent's
edit session on a wiki...), each made of ``Action``s (commits, revisions) that ``Change`` named
artifacts (files, wiki pages). Adapters in ``sources.py`` build units from git repos and other corpora.
In the RPG week a unit (a PR) is small: median 1 commit, ~7 min open. A subtask is a cluster of units.

Five independent signals each give a unit-by-unit similarity matrix, and Louvain finds communities in
a sparsified graph of it:

    files  TF-IDF over the artifacts a unit touched (hub artifacts like render.js count for little)
    code   TF-IDF over words in the content a unit *added*: identifiers split camelCase, import targets
    title  TF-IDF over the unit's title and action texts (PR title + commit subjects, edit summaries)
    chat   TF-IDF over messages that point at exactly this unit
    refs   explicit links only: unit-to-unit references, messages naming 2-4 units at once
    combined  a weighted blend of the five

Handoffs are derived from the artifacts, independently of clustering, between *different* actors:

    builds_on   B changes an artifact A's unit created
    integrates  B adds an import of a module A created
    tests       B adds tests that import A's module (and touches only tests)
    fixes       builds_on/integrates where B's unit title starts with fix/revert/repair...
    resubmits   B's unit creates the same artifact A's earlier unit created (took over / re-opened A's work)
    duplicate   near-identical title+content, different actors, no link between them, not both completed

Hub artifacts (touched by >8% of units and by at least 5, or flagged ``hub`` by the adapter) never create
handoffs; ``tests`` needs the adapter to flag test artifacts (``role: test``). Artifact *names* (not ids) feed
the text signals. Imports re-added by pasting an older copy of a file are ignored (see the git adapter).
"""

from __future__ import annotations

import collections
import math
import re
from dataclasses import dataclass, field
from datetime import datetime

import networkx as nx
import numpy as np
from scipy import sparse

METHODS = ("combined", "code", "title", "files", "chat", "refs")
LEVELS = {"coarse": 1.0, "medium": 3.0, "fine": 6.0}  # Louvain resolution
BLEND = {"code": 0.35, "title": 0.25, "files": 0.15, "chat": 0.10, "refs": 0.15}
TAU = {"files": 0.2, "code": 0.2, "title": 0.2, "chat": 0.15, "refs": 0.3, "combined": 0.12}
K_NEIGHBOURS = 6
SEED = 7
CHUNK = 256  # rows per block when computing nearest neighbours (memory ~ CHUNK x units)
DUP_MIN = 0.5  # default similarity for a 'duplicate' edge (corpora with templated content use higher)
DUP_PER_UNIT = 3  # keep only the closest few duplicates of each unit

METHOD_DESCRIPTIONS = {
    "combined": "weighted blend: code 35%, title 25%, files 15%, explicit refs 15%, chat 10%",
    "code": "words in the content each unit added (identifiers split camelCase, import targets)",
    "title": "unit title and action texts (PR title + commit subjects), generic words removed",
    "files": "artifacts (files, pages) each unit touched; hub artifacts count for little",
    "chat": "messages that point at exactly one unit",
    "refs": "explicit links between units and messages naming 2-4 units",
}

# "PR 12", "PR #12", "pull/12", "#152" (bare "#n" only for n >= 10, to skip "#1 priority")
PR_RX = re.compile(r"(?:\bPRs?\s*#?|pull/)(\d{1,4})\b|(?<![\w&])#(\d{2,4})\b", re.I)
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
FIXRE = re.compile(r"^\s*(?:\w+(?:\([^)]*\))?:\s*)?(fix|hotfix|repair|revert|restore)\b|\brevert\b", re.I)

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
good can will now just also ready done check checked scan scanned clean here there let lets
im ive ill should would could has have had was were been more some any no not yes ok okay main branch commit
commits rebase rebased conflict conflicts day issue issues comprehensive core basic initial""".split()
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
class Change:
    """What one action did to one artifact."""

    artifact: str
    new: bool = False
    size: int = 0  # lines/characters changed: weights the 'files' signal
    lines: list[str] = field(default_factory=list)  # net-new content, for the 'code' signal
    imports: list[str] = field(default_factory=list)  # newly imported module paths (code only)
    rewrite: bool = False


@dataclass
class Action:
    event_id: str
    actor: str
    time: str  # dataset timestamp format, UTC
    text: str  # commit subject, edit summary...
    changes: list[Change] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)  # artifacts it links to without changing them


@dataclass
class Unit:
    event_id: str
    short: str  # how to name it in a sentence: "PR #109", "edit session 412"
    title: str
    start: str
    end: str | None
    state: str  # dataset-specific: merged / landed / unmerged, or "n/a"
    completed: bool | None  # reached its goal (merged, published...); None = unknown
    actions: list[Action]
    refs: set[str] = field(default_factory=set)  # event ids of other units it links to explicitly
    roles: dict[str, set[str]] = field(default_factory=dict)  # extra actor roles, e.g. {"GPT-5.2": {"merge"}}
    tags: dict[str, int] = field(default_factory=dict)  # dataset's own labels for the unit, e.g. page families


@dataclass
class ChatMsg:
    event_id: str
    time: str
    actor: str
    text: str
    units: list[str]  # event ids of the units it points at


@dataclass
class Edge:
    kind: str
    src: int  # unit index of the giver (A)
    dst: int  # unit index of the taker (B)
    giver: str
    taker: str
    artifacts: list[str]
    giver_actions: list[str]  # action event ids
    taker_actions: list[str]
    score: float | None = None  # duplicates only


@dataclass
class Inference:
    corpus: str
    units: list[Unit]  # sorted by start; list index = unit index used everywhere below
    authors: list[str | None]  # main actor per unit
    touch: list[dict[str, set[str]]]  # actor -> roles ("author", "action", "merge"...) per unit
    terms: list[list[str]]  # top content terms per unit
    artifacts: list[list[tuple[str, int]]]
    vec: dict[str, tuple[sparse.csr_matrix, dict[int, str]]]  # L2-normalised TF-IDF rows per signal
    refs_raw: sparse.csr_matrix  # explicit link weights between units
    clusters: dict[str, dict[str, list[list[int]]]]  # method -> level -> clusters (sorted by start)
    names: dict[str, dict[str, list[str]]]  # the cleaned title of the cluster's most central unit
    keywords: dict[str, dict[str, list[str]]]  # the cluster's most distinctive terms ("talent · tree · rank")
    exemplars: dict[str, dict[str, list[list[int]]]]  # members, best founder first (up to EXEMPLARS)
    name_unit: dict[str, dict[str, list[int | None]]]  # the unit whose title is the name (None: keywords)
    label_of: dict[str, dict[str, np.ndarray]]  # method -> level -> cluster index per unit
    agreement: dict[str, dict[str, float]]  # ARI between methods (medium level)
    edges: list[Edge]
    chat: list[ChatMsg]
    chat_of_unit: dict[int, list[int]]  # unit index -> indices into chat
    rewrites: list[list[str]]
    index: dict[str, int]  # unit event id -> unit index
    unit_of_action: dict[str, list[int]]  # action event id -> unit indices
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- similarity helpers


def _tfidf(bags: list[collections.Counter]) -> tuple[sparse.csr_matrix, dict[int, str]]:
    n = len(bags)
    vocab: dict[str, int] = {}
    rows = [{vocab.setdefault(w, len(vocab)): c for w, c in b.items()} for b in bags]
    df = np.zeros(max(1, len(vocab)))
    for r in rows:
        for j in r:
            df[j] += 1
    idf = np.log((1 + n) / (1 + df)) + 1
    idf[df > 0.30 * n] *= 0.2  # terms in >30% of units carry ~no subtask signal
    data, indices, indptr = [], [], [0]
    for r in rows:
        vals = [(1 + math.log(c)) * idf[j] for j, c in r.items()]
        norm = math.sqrt(sum(v * v for v in vals)) or 1.0
        indices.extend(r.keys())
        data.extend(v / norm for v in vals)
        indptr.append(len(indices))
    X = sparse.csr_matrix((data, indices, indptr), shape=(n, max(1, len(vocab))))
    return X, {j: w for w, j in vocab.items()}


def _neighbours(
    vec: dict[str, tuple[sparse.csr_matrix, dict[int, str]]], R: sparse.csr_matrix, n: int, dup_min: float
) -> tuple[dict[str, tuple[np.ndarray, np.ndarray]], list[tuple[int, int, float]]]:
    """Top-K neighbours per unit for every method, plus duplicate candidates, in row blocks so memory stays
    O(CHUNK x n) instead of O(n^2)."""
    k = min(K_NEIGHBOURS, max(1, n - 1))
    out = {m: (np.zeros((n, k), int), np.zeros((n, k))) for m in METHODS}
    dups: list[tuple[int, int, float]] = []
    for start in range(0, n, CHUNK):
        stop = min(n, start + CHUNK)
        rows = np.arange(start, stop)
        blocks = {
            key: (vec[key][0][start:stop] @ vec[key][0].T).toarray() for key in ("files", "code", "title", "chat")
        }
        r = R[start:stop].toarray()
        blocks["refs"] = r / (r + 2)  # saturating: one explicit ref -> .6
        blocks["combined"] = sum(w * blocks[key] for key, w in BLEND.items())
        for m in METHODS:
            B = blocks[m]
            B[rows - start, rows] = 0
            top = np.argsort(-B, axis=1, kind="stable")[:, :k]  # ties -> lower unit index: deterministic
            out[m][0][start:stop] = top
            out[m][1][start:stop] = np.take_along_axis(B, top, axis=1)
        dup = 0.6 * blocks["title"] + 0.4 * blocks["code"]
        for a, b in zip(*np.nonzero(dup > dup_min), strict=True):
            i, j = start + int(a), int(b)
            if j > i:
                dups.append((i, j, float(dup[a, b])))
    return out, dups


def _cluster(nb: tuple[np.ndarray, np.ndarray], tau: float, res: float) -> list[list[int]]:
    idx, val = nb
    G = nx.Graph()
    G.add_nodes_from(range(len(idx)))
    for i in range(len(idx)):
        for j, v in zip(idx[i], val[i], strict=True):
            if v >= tau and int(j) != i:
                G.add_edge(i, int(j), weight=float(v))
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


# --------------------------------------------------------------------------- names: most central member's title

EXEMPLARS = 8  # most central members kept per cluster (names, LLM prompts)
NAME_CHARS = 80
TITLE_BOOST = 0.5  # extra weight of the title signal when picking the member whose title names the cluster
_TAG = re.compile(r"^\s*(?:\[[^\]]{1,24}\]\s*|(?:wip|draft)\b\s*[:\-]?\s*|[a-z][\w-]{0,15}(?:\([^)]*\))?!?:\s+)+", re.I)
_PRREF = re.compile(r"\s*(?:\((?:PR\s*|pull\s*)?#\d+\)|[-–—]\s*PR\s*#?\d+|\bPR\s*#\d+)", re.I)
_TESTS = re.compile(
    r"(?:,?\s*(?:and|with|\+|&|plus)\s+)?\(?\s*\d+\+?\s+(?:new\s+|unit\s+|passing\s+)?(?:tests?|assertions)\b\s*\)?",
    re.I,
)
_WITH_TESTS = re.compile(r"\s*(?:\+|&|and|with|plus)\s+(?:unit\s+)?tests\b", re.I)
_APPROVALS = re.compile(r"\b(?:approv\w*|lgtm|easter eggs?|verified|reviewed by)\b", re.I)


def unit_title(u: Unit) -> str:
    """The unit's own title; for a unit whose title is just its actions' texts joined (a session), the first one."""
    t = u.title or ""
    first = u.actions[0].text if u.actions else ""
    if len(u.actions) > 1 and first and t.startswith(first):
        return first
    return t


def clean_title(t: str) -> str:
    """A unit title as a subtask name: no conventional-commit / [WIP] prefixes, PR numbers, test counts or merge
    boilerplate ('Merging with 3 approvals - Map/World module' -> 'Map/World module'). '' if nothing is left."""
    t = " ".join((t or "").split("\n", 1)[0].split())
    if re.match(r"merg(?:e|ing)\b", t, re.I):
        if re.match(r"merge pull request\b", t, re.I):
            return ""
        t = re.sub(r"^merg(?:e|ing)\s+", "", t, flags=re.I)
        parts = [p for p in re.split(r"\s+[-–—]\s+", t) if p and not _APPROVALS.search(p)]
        t = parts[0] if parts else ""
    rv = re.match(r'revert\s+"(.+)"\s*$', t, re.I)
    if rv:
        return f"Revert: {clean_title(rv.group(1))}" if clean_title(rv.group(1)) else ""
    t = _TAG.sub("", t)
    t = _PRREF.sub("", t)
    t = _WITH_TESTS.sub("", _TESTS.sub("", t))
    if len(t) > 60 and ". " in t:
        t = t.split(". ", 1)[0]
    t = re.sub(r"\b\d{9,}(?:\.\d+)?\b|\b\d+\.\d{5,}\b", "", t)  # timestamps, random suffixes
    t = re.sub(r"\s+", " ", t).strip(" \t,;:.-–—")
    if t.startswith("(") and t.endswith(")") and "(" not in t[1:-1]:
        t = t[1:-1].strip()
    t = re.sub(r"\s*\([^()]{1,20}\)$", "", t)  # short trailing aside: "(clean)", "(v2)"
    if t.count("(") > t.count(")"):  # a parenthetical emptied or cut short: drop it
        t = t[: t.rfind("(")].strip(" \t,;:.-–—")
    if len(t) > NAME_CHARS:
        t = t[:NAME_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return t[:1].upper() + t[1:]


_AUX = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(?:tests?|docs?|chore|ci|style|build|revert)\b", re.I)
_BOILER = {"merge", "merging", "approval", "approvals", "pull", "request", "update", "updates", "wip", "draft"}


def central_name(units: list[Unit]) -> tuple[str, int | None]:
    """The cleaned title of the first unit (best founder first) that names the work: at least two words, and not a
    test/docs/chore/revert title while a feature title is available. Returns it and its position in ``units``
    ('', None when no title survives cleaning)."""
    titles = [(clean_title(unit_title(u)), unit_title(u)) for u in units]
    ok = [len([w for w in words(t) if w not in _BOILER]) >= 2 for t, _ in titles]
    for allow_aux in (False, True):
        for x, (t, raw) in enumerate(titles):
            if ok[x] and (allow_aux or not _AUX.match(raw)):
                return t, x
    return next(((t, x) for x, (t, _) in enumerate(titles) if t), ("", None))


def centrality(
    vec: dict[str, tuple[sparse.csr_matrix, dict[int, str]]], R: sparse.csr_matrix, lab: np.ndarray, cl: list[list[int]]
) -> np.ndarray:
    """Each unit's mean similarity to the other members of its cluster: the combined blend plus extra title weight
    (so the chosen title reads like the group's own wording). Sparse: O(nnz), never unit x unit."""
    N = len(lab)
    sizes = np.array([len(c) for c in cl])[lab] if N else np.zeros(0, int)
    cen = np.zeros(N)
    if N == 0 or not (sizes > 1).any():
        return cen
    M = sparse.csr_matrix((np.ones(N), (np.arange(N), lab)), shape=(N, len(cl)))
    weights = dict(BLEND)
    weights["title"] += TITLE_BOOST
    rows = np.arange(N)
    for key, w in weights.items():
        if key == "refs":
            Rs = R.tocsr(copy=True)
            Rs.data = Rs.data / (Rs.data + 2)
            s = np.asarray((Rs @ M)[rows, lab]).ravel()
        else:
            X = vec[key][0]
            S = (M.T @ X).tocsr()
            s = np.asarray(X.multiply(S[lab]).sum(1)).ravel() - np.asarray(X.multiply(X).sum(1)).ravel()
        cen += w * s
    return np.where(sizes > 1, cen / np.maximum(sizes - 1, 1), 0.0)


def founders(edges: list[Edge], lab: np.ndarray, cl: list[list[int]], cen: np.ndarray) -> np.ndarray:
    """How much each unit founded its cluster: its share of the cluster's top in-cluster handoffs given (others built
    on, integrated, tested or fixed its work) plus its share of the top centrality. Clusters without internal
    handoffs (most wiki sessions) rank by centrality alone."""
    out = np.zeros(len(lab))
    for e in edges:
        if e.kind != "duplicate" and lab[e.src] == lab[e.dst]:
            out[e.src] += 1
    rank = np.zeros(len(lab))
    for c in cl:
        o, z = out[c], cen[c]
        rank[c] = (o / o.max() if o.max() > 0 else 0) + (z / z.max() if z.max() > 0 else 0)
    return rank


# --------------------------------------------------------------------------- structure over subtasks


@dataclass
class Link:
    """Handoffs from units of subtask ``src`` to units of subtask ``dst`` (dst built on src's work)."""

    src: int
    dst: int
    kinds: collections.Counter
    edges: list[int]  # indices into Inference.edges
    actors: set[tuple[str, str]]  # (giver, taker)


def subtask_links(inf: Inference, method: str, level: str, duplicates: bool = False) -> list[Link]:
    """Unit-level handoffs whose ends lie in different subtasks, aggregated per (src, dst) subtask pair. O(edges)."""
    lab = inf.label_of[method][level]
    agg: dict[tuple[int, int], Link] = {}
    for x, e in enumerate(inf.edges):
        a, b = int(lab[e.src]), int(lab[e.dst])
        if a == b or (e.kind == "duplicate") != duplicates:
            continue
        ln = agg.setdefault((a, b), Link(a, b, collections.Counter(), [], set()))
        ln.kinds[e.kind] += 1
        ln.edges.append(x)
        ln.actors.add((e.giver, e.taker))
    return sorted(agg.values(), key=lambda ln: (-len(ln.edges), ln.src, ln.dst))


def parents(inf: Inference, method: str, level: str) -> list[tuple[int, float]] | None:
    """For each subtask at ``level``, the subtask one level coarser holding most of its units, and that share.
    None at the coarsest level. (Levels are clustered independently, so a subtask can straddle two parents.)"""
    lv = list(LEVELS)
    i = lv.index(level)
    if i == 0:
        return None
    up = inf.label_of[method][lv[i - 1]]
    out = []
    for c in inf.clusters[method][level]:
        p, n = collections.Counter(int(up[j]) for j in c).most_common(1)[0]
        out.append((p, round(n / len(c), 2)))
    return out


# --------------------------------------------------------------------------- main entry


def infer(
    corpus: str,
    units: list[Unit],
    chat: list[ChatMsg],
    dup_min: float = DUP_MIN,
    artifact_meta: dict[str, dict] | None = None,
) -> Inference:
    """Cluster ``units`` and derive handoffs. ``chat``: messages already linked to unit event ids.
    ``artifact_meta``: artifact id -> {"name", "hub", "role"} from the adapter."""
    am = artifact_meta or {}

    def aname(art: str) -> str:
        return am.get(art, {}).get("name") or art.split(":", 2)[-1]

    order = sorted(units, key=lambda u: (u.start, u.event_id))
    idx = {u.event_id: i for i, u in enumerate(order)}
    N = len(order)

    # ---- per-unit features
    creator: dict[str, tuple[int, str, str, str]] = {}  # artifact -> (unit idx, actor, time, action id)
    F = []
    authors: list[str | None] = []
    touch: list[dict[str, set[str]]] = []
    unit_of_action: dict[str, list[int]] = collections.defaultdict(list)
    for i, u in enumerate(order):
        files: collections.Counter = collections.Counter()
        created: set[str] = set()
        code: collections.Counter = collections.Counter()
        imports: dict[str, tuple[str, str]] = {}  # target -> (actor, action id)
        file_first: dict[str, tuple[str, str]] = {}  # artifact -> (actor, action id) of the first action touching it
        rewrites: set[str] = set()
        roles: dict[str, set[str]] = collections.defaultdict(set)
        counts: collections.Counter = collections.Counter()
        for act in u.actions:
            a = act.actor
            unit_of_action[act.event_id].append(i)
            roles[a].add("action")
            counts[a] += 1
            for ch in act.changes:
                path = ch.artifact
                files[path] += max(1, ch.size)
                file_first.setdefault(path, (a, act.event_id))
                if ch.rewrite:
                    rewrites.add(path)
                if ch.new:
                    created.add(path)
                    if path not in creator or creator[path][2] > act.time:
                        creator[path] = (i, a, act.time, act.event_id)
                for t in ch.imports:
                    imports.setdefault(t, (a, act.event_id))
                    for w in file_words(t):
                        code[w] += 3
                for line in ch.lines:
                    for ident in IDENT.findall(line):
                        for w in words(ident):
                            if w not in JS_STOP:
                                code[stem(w)] += 1
                for w in file_words(aname(path)):
                    code[w] += 2
        author = counts.most_common(1)[0][0] if counts else None
        if author:
            roles[author].add("author")
        for a, rs in u.roles.items():
            roles[a] |= set(rs)
        authors.append(author)
        touch.append(dict(roles))
        title_txt = " ".join([u.title] + [act.text for act in u.actions])
        F.append(
            {
                "files": files,
                "created": created,
                "code": code,
                "imports": imports,
                "file_first": file_first,
                "rewrites": rewrites,
                "title": collections.Counter(text_words(title_txt)),
                "refs": {r for r in u.refs if r != u.event_id},
                "chat": collections.Counter(),
            }
        )

    # ---- chat
    chat_of_unit: dict[int, list[int]] = collections.defaultdict(list)
    comention: collections.Counter = collections.Counter()
    for k, msg in enumerate(chat):
        ix = sorted({idx[e] for e in msg.units if e in idx})
        for i in ix:
            chat_of_unit[i].append(k)
        if len(ix) == 1:
            F[ix[0]]["chat"].update(text_words(msg.text))
        elif 2 <= len(ix) <= 4:
            for x in range(len(ix)):
                for y in range(x + 1, len(ix)):
                    comention[(ix[x], ix[y])] += 1

    # ---- similarity: sparse TF-IDF per signal, explicit links, then top-K neighbours per method
    vec = {key: _tfidf([f[key] for f in F]) for key in ("files", "code", "title", "chat")}
    links: collections.Counter = collections.Counter()
    for i, f in enumerate(F):
        for e in f["refs"]:
            if e in idx and idx[e] != i:
                links[(i, idx[e])] += 3
                links[(idx[e], i)] += 3
    for (a, b), c in comention.items():
        links[(a, b)] += c
        links[(b, a)] += c
    R = sparse.csr_matrix(
        (list(links.values()), ([a for a, _ in links], [b for _, b in links])), shape=(N, N), dtype=float
    )
    nbrs, dup_pairs = _neighbours(vec, R, N, dup_min)

    # ---- clusters, sorted by first unit start so ids read chronologically
    clusters: dict[str, dict[str, list[list[int]]]] = {}
    label_of: dict[str, dict[str, np.ndarray]] = {}
    for m in METHODS:
        clusters[m], label_of[m] = {}, {}
        for lvl, res in LEVELS.items():
            cl = sorted(_cluster(nbrs[m], TAU[m], res), key=lambda c: (min(c), -len(c)))
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
            v = np.asarray(X[c].sum(0)).ravel()
            for t in np.argsort(-v)[:8]:
                if v[t] > 0:
                    term = inv[t] if key != "files" else " ".join(file_words(aname(inv[t])))
                    if term:
                        sc[term] += w * v[t]
        out: list[str] = []
        for t, _ in sc.most_common(12):
            if not any(t in o or o in t for o in out):
                out.append(t)
            if len(out) == 3:
                break
        return " · ".join(out)

    def kw(c: list[int]) -> str:
        return name(c) if len(c) > 1 else " · ".join(list(dict.fromkeys(text_words(unit_title(order[c[0]]))))[:3])

    keywords = {m: {lvl: [kw(c) for c in cl] for lvl, cl in clusters[m].items()} for m in METHODS}
    # ---- handoffs
    touch_ct = collections.Counter(path for f in F for path in f["files"])
    hub = {p for p, c in touch_ct.items() if c > max(0.08 * N, 4) or am.get(p, {}).get("hub")}
    stem_of: dict[str, list[str]] = collections.defaultdict(list)
    for path in creator:
        stem_of[module_stem(aname(path))].append(path)
    raw: dict[tuple[int, int, str], Edge] = {}

    def add(i: int, j: int, kind: str, path: str, a: str, b: str, act_a: str, act_b: str) -> None:
        e = raw.setdefault((i, j, kind), Edge(kind, i, j, a, b, [], [], []))
        if path not in e.artifacts:
            e.artifacts.append(path)
        if act_a not in e.giver_actions:
            e.giver_actions.append(act_a)
        if act_b not in e.taker_actions:
            e.taker_actions.append(act_b)

    for j, f in enumerate(F):
        for path in f["files"]:
            if path in creator and path not in hub:
                i, a, _, sha_a = creator[path]
                b, sha_b = f["file_first"][path]
                if i == j or a == b or order[i].start > order[j].start:
                    continue
                add(i, j, "resubmits" if path in f["created"] else "builds_on", path, a, b, sha_a, sha_b)
        only_tests = bool(f["files"]) and all(am.get(q, {}).get("role") == "test" or q in hub for q in f["files"])
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
        e.artifacts = e.artifacts[:6]
        edges.append(e)
    linked = {(e.src, e.dst) for e in edges} | {(e.dst, e.src) for e in edges}
    per_unit: dict[int, list[tuple[float, int]]] = collections.defaultdict(list)
    for i, j, score in dup_pairs:
        if (
            authors[i] != authors[j]
            and (i, j) not in linked
            and not (order[i].completed and order[j].completed)
            and abs(_days(order[i].start, order[j].start)) < 3
        ):
            per_unit[j].append((score, i))  # j (the later unit) duplicated i
    for j, cands in per_unit.items():
        for score, i in sorted(cands, reverse=True)[:DUP_PER_UNIT]:
            edges.append(Edge("duplicate", i, j, authors[i] or "?", authors[j] or "?", [], [], [], round(score, 2)))
    edges.sort(key=lambda e: (order[e.dst].start, e.kind))

    # ---- names: the cleaned title of the member the others built on most, else the most central one
    exemplars: dict[str, dict[str, list[list[int]]]] = {}
    names: dict[str, dict[str, list[str]]] = {}
    name_unit: dict[str, dict[str, list[int | None]]] = {}
    for m in METHODS:
        exemplars[m], names[m], name_unit[m] = {}, {}, {}
        for lvl, cl in clusters[m].items():
            lab = label_of[m][lvl]
            rank = founders(edges, lab, cl, centrality(vec, R, lab, cl))
            ex = [sorted(c, key=lambda i: (-rank[i], order[i].start, i))[:EXEMPLARS] for c in cl]
            exemplars[m][lvl] = ex
            names[m][lvl], name_unit[m][lvl] = [], []
            for k, e in enumerate(ex):
                nm, x = central_name([order[i] for i in e])
                names[m][lvl].append(nm or keywords[m][lvl][k])
                name_unit[m][lvl].append(None if x is None else e[x])

    return Inference(
        corpus=corpus,
        units=order,
        authors=authors,
        touch=touch,
        terms=[[t for t, _ in f["code"].most_common(8)] for f in F],
        artifacts=[f["files"].most_common(8) for f in F],
        vec=vec,
        refs_raw=R,
        clusters=clusters,
        names=names,
        keywords=keywords,
        exemplars=exemplars,
        name_unit=name_unit,
        label_of=label_of,
        agreement=agreement,
        edges=edges,
        chat=chat,
        chat_of_unit=dict(chat_of_unit),
        rewrites=[sorted(f["rewrites"]) for f in F],
        index=idx,
        unit_of_action=dict(unit_of_action),
    )


def _days(a: str, b: str) -> float:
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    return (datetime.strptime(a, fmt) - datetime.strptime(b, fmt)).total_seconds() / 86400


def score(inf: Inference, method: str, i: int, j: int) -> float:
    """Similarity of two units under one method (0..1)."""
    if method == "refs":
        r = float(inf.refs_raw[i, j])
        return r / (r + 2)
    if method == "combined":
        return sum(w * score(inf, key, i, j) for key, w in BLEND.items())
    X = inf.vec[method][0]
    return float(X[i].multiply(X[j]).sum())


def shared_terms(inf: Inference, key: str, i: int, j: int, n: int = 4) -> list[str]:
    X, inv = inf.vec[key]
    prod = X[i].multiply(X[j]).tocoo()
    top = sorted(zip(prod.data, prod.col, strict=True), reverse=True)[:n]
    return [inv[int(c)] for v, c in top if v > 0]


def why(inf: Inference, i: int, j: int) -> dict[str, dict]:
    """Per-signal similarity between two units, with the terms they share."""
    out: dict[str, dict] = {}
    for key in ("code", "title", "files", "chat"):
        s = score(inf, key, i, j)
        if s > 0.05:
            out[key] = {"score": round(s, 2), "shared": shared_terms(inf, key, i, j)}
    raw = float(inf.refs_raw[i, j])
    if raw:
        out["refs"] = {"score": round(raw / (raw + 2), 2), "shared": [f"{int(raw)} link weight"]}
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
