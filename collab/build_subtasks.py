#!/usr/bin/env python3
"""Infer subtasks (feature lines) from PRs with several independent methods, so they can be compared side by side.

A PR here is roughly one work episode (median: 1 commit, ~7 min open). A subtask is a cluster of PRs.
Each method turns PRs into a similarity graph, then Louvain finds communities in it:

  files    TF-IDF over the paths each PR touched (hub files like render.js are down-weighted by IDF)
  code     TF-IDF over words in the code each PR *added*: identifiers split camelCase, import targets.
           This is what separates "two PRs edit render.js" from "two PRs add talent code to render.js".
  title    TF-IDF over PR title + commit subjects
  chat     TF-IDF over chat messages that point at exactly one PR (what agents said about it)
  refs     explicit links only: "#N" in titles/commits, and chat messages naming 2-4 PRs at once
  combined weighted blend of the above

Independently of clustering, it derives typed handoff edges between agents, attributed to the commit
authors who created / touched the file (not just the PR openers). Hub files touched by >8% of PRs
(render.js, main.js...) are ignored, and imports that only reappear because an agent pasted an older
copy of a file (several imports swapped out and in, or a big removal) don't count:
  builds_on   B modifies a file that A created
  tests       B adds tests that import a module A created
  resubmits   B's PR creates the same file A's earlier PR created (took over / re-opened A's work)
  integrates  B adds an import of a module A created
  fixes       a builds_on/integrates edge where B's title says fix/repair/revert
  duplicate   very similar titles+code, different authors, neither builds on the other, one never merged

Usage (after build_prgraph.py):
    python3 collab/build_subtasks.py --repo data/repos/rpg-game.git --prgraph collab/out/rpg/prgraph.json \
        --timeline collab/out/rpg/timeline.json --out collab/out/rpg/subtasks.json
Needs numpy and networkx.
"""
import argparse, collections, json, math, re, subprocess
import numpy as np
import networkx as nx

ap = argparse.ArgumentParser()
ap.add_argument('--repo', required=True); ap.add_argument('--prgraph', required=True)
ap.add_argument('--timeline', required=True); ap.add_argument('--out', required=True)
ap.add_argument('--k', type=int, default=6, help='neighbors kept per PR when sparsifying a similarity matrix')
ap.add_argument('--tau', type=float, default=0.12, help='minimum similarity for an edge')
ap.add_argument('--resolutions', default='1,3,6', help='Louvain resolutions to precompute (higher = smaller subtasks)')
args = ap.parse_args()

P = json.load(open(args.prgraph)); TL = json.load(open(args.timeline))
AG = P['agents']; NA = len(AG)
prs = sorted(P['prs'], key=lambda p: p['start'])
N = len(prs); idx = {p['n']: i for i, p in enumerate(prs)}

# ---------------------------------------------------------------- diffs
SKIP_FILE = re.compile(r'(package-lock\.json|\.min\.js|\.png|\.jpg|\.svg|\.ico)$')
IMPORT = re.compile(r"""(?:import\b[^'"]*?from\s*|import\s*\(\s*|require\s*\(\s*)['"]([^'"]+)['"]""")
IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]{2,}')
MAX_ADD = 600   # added lines read per file per commit

def git(*a):
    return subprocess.run(['git', '-C', args.repo, *a], capture_output=True, text=True, errors='replace').stdout

def parse_show(text):
    commits = {}; cur = f = None
    for line in text.split('\n'):
        if line.startswith('\x00C '):
            cur = commits.setdefault(line.split()[1][:9], {}); f = None; continue
        if cur is None: continue
        if line.startswith('diff --git '):
            path = line.split(' b/', 1)[-1]
            f = None if SKIP_FILE.search(path) else cur.setdefault(path, {'new': False, 'add': 0, 'del': 0, 'lines': [], 'removed': [], 'hunks': []})
        elif f is None: continue
        elif line.startswith('new file mode'): f['new'] = True
        elif line.startswith('@@'):
            m = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', line)
            if m: f['hunks'].append([int(m.group(1)), int(m.group(2) or 1)])
        elif line.startswith('+') and not line.startswith('+++'):
            f['add'] += 1
            if len(f['lines']) < MAX_ADD: f['lines'].append(line[1:])
        elif line.startswith('-') and not line.startswith('---'):
            f['del'] += 1
            if len(f['removed']) < MAX_ADD: f['removed'].append(line[1:])
    for files in commits.values():      # a rewrite removes and re-adds the same lines: keep only the net-new ones
        for f in files.values():
            if f['removed']:
                gone = collections.Counter(l.strip() for l in f['removed'])
                keep = []
                for l in f['lines']:
                    if gone[l.strip()] > 0: gone[l.strip()] -= 1
                    else: keep.append(l)
                f['lines'] = keep
    return commits

shas = sorted({c[3] for p in prs for c in p['commits']})
DIFF = {}
for i in range(0, len(shas), 80):
    DIFF.update(parse_show(git('show', '-U0', '--no-color', '--no-renames', '--format=%x00C %H', *shas[i:i + 80])))
print(f'{len(shas)} commits parsed')

# imports that already existed in the parent version of a file are not new links (agents often paste whole stale files)
specs = [(sha, path) for sha, files in DIFF.items() for path, f in files.items()
         if not f['new'] and any(IMPORT.search(l) for l in f['lines'])]
PARENT_IMPORTS = {}
if specs:
    full = {s9: git('rev-parse', s9).strip() for s9 in {s9 for s9, _ in specs}}
    blob = subprocess.run(['git', '-C', args.repo, 'cat-file', '--batch'], capture_output=True,
                          input=''.join(f'{full[s9]}^:{p}\n' for s9, p in specs).encode()).stdout   # bytes: sizes are byte counts
    pos = 0
    for spec in specs:
        nl = blob.index(b'\n', pos); head = blob[pos:nl].split()
        if len(head) == 3 and head[1] == b'blob':
            size = int(head[2]); body = blob[nl + 1: nl + 1 + size].decode('utf-8', 'replace')
            PARENT_IMPORTS[spec] = {m.group(1) for m in IMPORT.finditer(body)}
            pos = nl + 1 + size + 1
        else: pos = nl + 1


def words(s):
    """camelCase / snake / kebab -> lowercase words."""
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', s); s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', s)
    return [w for w in re.split(r'[^A-Za-z]+', s.lower()) if len(w) > 2]

JS_STOP = set('''const let var function return import export from default class this new null undefined true false if else for
while switch case break continue typeof instanceof await async try catch throw finally length push map filter reduce
foreach join slice concat includes keys values entries object array string number math floor round min max console log
document element innerhtml classname style div span button id data type div html css px rem test tests assert equal
strict deep describe should expect node describe the and with that are not has get set'''.split())
TXT_STOP = set('''feat fix fixes chore docs doc test tests add adds added adding update updates updated with and the for into from
to of in on a an by pr prs merge merged pull request requests new system systems module modules integration integrate
integrated integrating wire wired wiring implement implemented implementation support use using via all its it this that
is are be as or vs our we i you please review approve approved approval lgtm thanks thank looks good can will now just
also ready done check checked scan scanned clean egg eggs easter saboteur here there let lets im ive ill should would
could has have had was were been more some any no not yes ok okay main branch commit commits rebase rebased conflict
conflicts conflict-free day issue issues game rpg comprehensive core basic initial'''.split())

def text_words(s):
    s = re.sub(r'https?://\S+', ' ', s); s = re.sub(r'#\d+', ' ', s)
    s = re.sub(r'^\s*\w+(\([^)]*\))?:\s*', '', s)            # conventional-commit prefix
    return [w for w in words(s) if w not in TXT_STOP and not w.isdigit()]

def stem(w):  # tiny plural/suffix fold so talent/talents, enemy/enemies meet
    for suf, rep in (('ies', 'y'), ('ses', 's'), ('s', '')):
        if w.endswith(suf) and len(w) - len(suf) >= 3: return w[:-len(suf)] + rep
    return w

def file_words(path):
    base = re.sub(r'\.(m?js|ts|json|md|css|html)$', '', path.rsplit('/', 1)[-1])
    return [stem(w) for w in words(base) if w not in ('test', 'tests', 'index', 'spec')]

# ---------------------------------------------------------------- per-PR features
creator = {}            # path -> (pr idx, agent, t) of first PR that created it
F = []
for i, p in enumerate(prs):
    files = collections.Counter(); created = set(); code = collections.Counter(); imports = set()
    file_agents = collections.defaultdict(list); import_agent = {}; rewrites = set()
    for t, a, subj, sha in p['commits']:
        for path, f in DIFF.get(sha, {}).items():
            files[path] += f['add'] + f['del']; file_agents[path].append(a)
            if f['new']:
                created.add(path)
                if path not in creator or creator[path][2] > t: creator[path] = (i, a, t)
            # big removals = a rewrite (often an agent overwriting a file with a stale copy); its imports aren't new links
            # also: swapping several imports out and others in = pasting an older/other version of the file
            rewrite = (f['del'] >= 40 and f['del'] >= 0.5 * f['add']) or \
                (sum(bool(IMPORT.search(l)) for l in f['removed']) >= 2 and sum(bool(IMPORT.search(l)) for l in f['lines']) >= 2)
            if rewrite: rewrites.add(path)
            for line in f['lines']:
                for m in IMPORT.finditer(line):
                    if rewrite or m.group(1) in PARENT_IMPORTS.get((sha, path), ()): continue
                    imports.add(m.group(1)); import_agent.setdefault(m.group(1), a)
                    for w in file_words(m.group(1)): code[w] += 3
                for ident in IDENT.findall(line):
                    for w in words(ident):
                        if w not in JS_STOP: code[stem(w)] += 1
            for w in file_words(path): code[w] += 2
    title_txt = ' '.join([p['title']] + [c[2] for c in p['commits']])
    F.append({'files': files, 'created': created, 'code': code, 'imports': imports, 'file_agents': file_agents, 'import_agent': import_agent, 'rewrites': rewrites,
              'title': collections.Counter(stem(w) for w in text_words(title_txt)),
              'refs': {int(x) for x in re.findall(r'#(\d+)', title_txt)} - {p['n']}})

# chat: messages pointing at exactly one PR give it text; messages naming 2-4 PRs give refs edges
chat_txt = collections.defaultdict(collections.Counter); comention = collections.Counter()
for msg in TL['msgs']:
    text, mprs = msg[3], [x for x in msg[5] if x in idx]
    if len(mprs) == 1:
        for w in text_words(text): chat_txt[idx[mprs[0]]][stem(w)] += 1
    elif 2 <= len(mprs) <= 4:
        for x in range(len(mprs)):
            for y in range(x + 1, len(mprs)): comention[tuple(sorted((idx[mprs[x]], idx[mprs[y]])))] += 1
for i in range(N): F[i]['chat'] = chat_txt[i]

# ---------------------------------------------------------------- similarity
def tfidf(key, sublinear=True):
    vocab = {}; rows = []
    for f in F:
        rows.append({vocab.setdefault(w, len(vocab)): c for w, c in f[key].items()})
    df = np.zeros(len(vocab))
    for r in rows:
        for j in r: df[j] += 1
    idf = np.log((1 + N) / (1 + df)) + 1
    # terms in more than 30% of PRs carry ~no subtask signal (render.js, "combat")
    idf[df > 0.30 * N] *= 0.2
    X = np.zeros((N, len(vocab)))
    for i, r in enumerate(rows):
        for j, c in r.items(): X[i, j] = (1 + math.log(c) if sublinear else c) * idf[j]
    nrm = np.linalg.norm(X, axis=1, keepdims=True); nrm[nrm == 0] = 1
    X /= nrm
    inv = {j: w for w, j in vocab.items()}
    return X, inv

SIM = {}; VEC = {}
for key in ('files', 'code', 'title', 'chat'):
    X, inv = tfidf(key); VEC[key] = (X, inv)
    S = X @ X.T; np.fill_diagonal(S, 0); SIM[key] = S

R = np.zeros((N, N))
for i, f in enumerate(F):
    for n in f['refs']:
        if n in idx: R[i, idx[n]] += 3; R[idx[n], i] += 3
for (a, b), c in comention.items(): R[a, b] += c; R[b, a] += c
SIM['refs'] = R / (R + 2)          # saturating: 1 ref -> .6, 2 co-mentions -> .5

BLEND = {'code': .35, 'title': .25, 'files': .15, 'chat': .10, 'refs': .15}
SIM['combined'] = sum(w * SIM[k] for k, w in BLEND.items())

RES = [float(r) for r in args.resolutions.split(',')]
def cluster(S, tau, res, k=args.k):
    G = nx.Graph(); G.add_nodes_from(range(N))
    for i in range(N):
        for j in np.argsort(-S[i])[:k]:
            if S[i, j] >= tau: G.add_edge(i, int(j), weight=float(S[i, j]))
    comms = nx.community.louvain_communities(G, weight='weight', resolution=res, seed=7)
    return sorted([sorted(c) for c in comms], key=lambda c: (-len(c), c[0]))

TAU = {'files': .2, 'code': .2, 'title': .2, 'chat': .15, 'refs': .3, 'combined': args.tau}
CLR = {m: {r: cluster(SIM[m], TAU[m], r) for r in RES} for m in SIM}
MID = RES[len(RES) // 2]
CL = {m: CLR[m][MID] for m in SIM}       # agreement and printout use the middle resolution

# ---------------------------------------------------------------- agreement (adjusted Rand, singletons included)
def labels(cl):
    lab = np.zeros(N, int)
    for ci, c in enumerate(cl): lab[c] = ci
    return lab
def ari(a, b):
    ct = collections.Counter(zip(a, b)); comb = lambda x: x * (x - 1) / 2
    s = sum(comb(v) for v in ct.values()); sa = sum(comb(v) for v in collections.Counter(a).values())
    sb = sum(comb(v) for v in collections.Counter(b).values()); e = sa * sb / comb(len(a))
    return 0.0 if (sa + sb) / 2 == e else (s - e) / ((sa + sb) / 2 - e)
LAB = {m: labels(c) for m, c in CL.items()}
AGREE = {a: {b: round(ari(LAB[a], LAB[b]), 3) for b in CL} for a in CL}

# ---------------------------------------------------------------- typed handoff edges
FIXRE = re.compile(r'^\s*(fix|hotfix|repair|revert|restore)\b|\brevert\b', re.I)
touch_ct = collections.Counter(path for f in F for path in f['files'])
HUB = {path for path, c in touch_ct.items() if c > 0.08 * N or path.rsplit('/', 1)[-1] in ('package.json', 'README.md', 'index.html', 'styles.css')}
def author(i): return prs[i]['author']
stem_of = collections.defaultdict(list)
for path, (ci, a, t) in creator.items():
    stem_of[re.sub(r'\.(m?js|ts)$', '', path.rsplit('/', 1)[-1])].append(path)
edges = {}      # (i, j, kind) -> [evidence paths, giver agent, taker agent]
def add_edge(i, j, kind, path, a, b):
    e = edges.setdefault((i, j, kind), [set(), a, b]); e[0].add(path)
for j, f in enumerate(F):
    for path in f['files']:
        if path in creator and path not in HUB:
            i, a, _ = creator[path]; b = f['file_agents'][path][0]
            if i == j or a == b or b >= NA or prs[i]['start'] > prs[j]['start']: continue
            # B also "creates" A's file: B re-submitted / took over A's work rather than extending it
            add_edge(i, j, 'resubmits' if path in f['created'] else 'builds_on', path, a, b)
    for imp in f['imports']:
        st = re.sub(r'\.(m?js|ts)$', '', imp.rsplit('/', 1)[-1]); b = f['import_agent'][imp]
        for path in stem_of.get(st, []):
            i, a, _ = creator[path]
            if i == j or a == b or b >= NA or prs[i]['start'] > prs[j]['start']: continue
            kind = 'tests' if all(q.startswith('test') or q in HUB for q in f['files']) else 'integrates'
            add_edge(i, j, kind, path, a, b)
E = []
for (i, j, kind), (ev, a, b) in edges.items():
    if kind == 'builds_on' and any((i, j, k) in edges for k in ('integrates', 'tests', 'resubmits')): continue
    if kind in ('integrates', 'builds_on') and (i, j, 'resubmits') in edges: continue
    if kind in ('integrates', 'builds_on') and FIXRE.search(prs[j]['title']): kind = 'fixes'
    E.append([i, j, kind, sorted(ev)[:4], a, b])
linked = {(e[0], e[1]) for e in E} | {(e[1], e[0]) for e in E}
DUP = SIM['title'] * .6 + SIM['code'] * .4
for i in range(N):
    for j in range(i + 1, N):
        if DUP[i, j] > .5 and author(i) != author(j) and (i, j) not in linked and \
                abs(prs[i]['start'] - prs[j]['start']) < 3 * 86400 and \
                ('merged' not in (prs[i]['state'], prs[j]['state']) or prs[i]['state'] != prs[j]['state']):
            E.append([i, j, 'duplicate', [f'{DUP[i, j]:.2f} similar'], author(i), author(j)])
print(collections.Counter(e[2] for e in E))

# ---------------------------------------------------------------- why: top neighbors with shared terms
def shared_terms(key, i, j, n=4):
    X, inv = VEC[key]; prod = X[i] * X[j]
    return [inv[t] for t in np.argsort(-prod)[:n] if prod[t] > 0]
def why(i, j):
    out = {}
    for key in ('code', 'title', 'files', 'chat'):
        if SIM[key][i, j] > .05: out[key] = [round(float(SIM[key][i, j]), 2), shared_terms(key, i, j)]
    if R[i, j]: out['refs'] = [round(float(SIM['refs'][i, j]), 2), [f'{int(R[i, j])} ref weight']]
    return out
NEIGH = []
for i in range(N):
    S = SIM['combined'][i]
    NEIGH.append([[int(j), round(float(S[j]), 3), why(i, int(j))] for j in np.argsort(-S)[:5] if S[j] > .05])

# ---------------------------------------------------------------- cluster names (top distinctive terms)
def name(c):
    sc = collections.Counter()
    for key, w in (('title', 1.0), ('code', .6), ('files', .4)):
        X, inv = VEC[key]; v = X[c].sum(0)
        for t in np.argsort(-v)[:8]:
            if v[t] > 0:
                term = inv[t] if key != 'files' else ' '.join(file_words(inv[t]))
                if term: sc[term] += w * v[t]
    out = []
    for t, _ in sc.most_common(12):
        if not any(t in o or o in t for o in out): out.append(t)
        if len(out) == 3: break
    return ' · '.join(out)

methods = {m: {'tau': TAU[m], 'levels': {str(r): {'clusters': cl, 'names': [name(c) if len(c) > 1 else prs[c[0]]['title'][:60] for c in cl]}
                                         for r, cl in CLR[m].items()}} for m in CLR}

out = {
    'meta': {**P['meta'], 'blend': BLEND, 'k': args.k, 'resolutions': RES, 'default_resolution': MID},
    'agents': AG, 'segments': P['segments'],
    'prs': [{'n': p['n'], 'title': p['title'], 'author': p['author'], 'merger': p['merger'], 'state': p['state'],
             'start': p['start'], 'end': p['end'], 'commits': [[c[0], c[1]] for c in p['commits']],
             'reviews': p['reviews'], 'chat': len(p['chat']),
             'files': [[f, n] for f, n in F[i]['files'].most_common(8)], 'created': sorted(F[i]['created'])[:8], 'rewrites': sorted(F[i]['rewrites'])[:8],
             'terms': [t for t, _ in F[i]['code'].most_common(8)]} for i, p in enumerate(prs)],
    'methods': methods, 'agreement': AGREE, 'edges': E, 'neighbors': NEIGH,
}
json.dump(out, open(args.out, 'w'), separators=(',', ':'))
for m, cl in CL.items():
    big = [c for c in cl if len(c) > 1]
    print(f'{m:9s} {len(big):3d} clusters (>1 PR), {sum(len(c) == 1 for c in cl):3d} singletons, largest {len(cl[0])}')
print('ARI vs combined:', {m: AGREE['combined'][m] for m in CL})
