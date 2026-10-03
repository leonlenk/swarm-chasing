#!/usr/bin/env python3
"""Build a PR-centric "git graph" dataset: who opened, committed to, reviewed, discussed, and merged each PR.

Joins the shared repo's real git history with the village activity log.

Usage (run build_timeline.py first; it supplies review/merge/chat events):
    python3 collab/build_timeline.py --goal "RPG together" --out collab/out/rpg
    python3 collab/build_prgraph.py  --repo data/repos/rpg-game.git --timeline collab/out/rpg/timeline.json \
        --out collab/out/rpg/prgraph.json [--saboteurs collab/rpg_saboteurs.json]

Get the repo (bare clone incl. every PR head, so squash-merged PRs keep their commits):
    git clone --bare https://github.com/ai-village-agents/rpg-game data/repos/rpg-game.git
    git -C data/repos/rpg-game.git fetch origin '+refs/pull/*/head:refs/pull/*/head'
"""
import argparse, bisect, collections, json, os, re, subprocess
from datetime import datetime, timezone

ap = argparse.ArgumentParser()
ap.add_argument('--repo', required=True); ap.add_argument('--timeline', required=True)
ap.add_argument('--out', required=True); ap.add_argument('--saboteurs')
ap.add_argument('--main', default='main'); ap.add_argument('--max-commits', type=int, default=80)
args = ap.parse_args()

TL = json.load(open(args.timeline))
AG = TL['agents']; NA = len(AG)
T0 = datetime.fromisoformat(TL['meta']['start']).replace(tzinfo=timezone.utc)
TEND = (datetime.fromisoformat(TL['meta']['end']).replace(tzinfo=timezone.utc) - T0).total_seconds()
def sec_iso(s): return int((datetime.fromisoformat(s) - T0).total_seconds())

def family(name):
    n = name.lower()
    return 'claude' if any(k in n for k in ('claude', 'opus', 'sonnet', 'haiku')) else \
           'gpt' if 'gpt' in n or re.match(r'o\d', n) else 'gemini' if 'gemini' in n else 'other'

# ---------- git author -> agent ----------
def norm(s):
    s = re.sub(r'[^a-z0-9]', '', s.lower())
    for suf in ('aivillage', 'collab', 'agent'): s = s.replace(suf, '')
    return s
ALIAS = {norm(a['name']): i for i, a in enumerate(AG)}
extra = []  # authors that are not village agents in this window
def agent_of(author):
    k = norm(author)
    if k in ALIAS: return ALIAS[k]
    if k.startswith('claude') is False and norm('Claude ' + author) in ALIAS: return ALIAS[norm('Claude ' + author)]
    if author not in extra: extra.append(author)
    return NA + extra.index(author)

def git(*a):
    return subprocess.run(['git', '-C', args.repo, *a], capture_output=True, text=True, check=True).stdout

# ---------- main first-parent history ----------
SEP, END = '\x1f', '\x1e'
main = []
for rec in git('log', '--first-parent', args.main, '--format=%H%x1f%P%x1f%an%x1f%aI%x1f%cI%x1f%s%x1f%b%x1e').split(END):
    rec = rec.strip('\n')
    if not rec: continue
    h, parents, an, ai, ci, subj, body = rec.split(SEP)
    main.append({'h': h, 'p': parents.split(), 'an': an, 'at': sec_iso(ai), 'ct': sec_iso(ci), 'subj': subj, 'body': body.strip()})
main.reverse()                                   # oldest first
main_ct = [m['ct'] for m in main]
main_idx = {m['h']: i for i, m in enumerate(main)}

merged_by_commit = {}  # pr -> (main index, kind)
for i, m in enumerate(main):
    mm = re.match(r'Merge pull request #(\d+) from \S+?/(\S+)', m['subj'])
    if mm: merged_by_commit[int(mm.group(1))] = (i, 'merge', mm.group(2)); continue
    ms = re.search(r'\(#(\d+)\)$', m['subj'])
    if ms and int(ms.group(1)) not in merged_by_commit: merged_by_commit[int(ms.group(1))] = (i, 'squash', None)

heads = {}
for l in git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/pull').splitlines():
    ref, h = l.split(); heads[int(ref.split('/')[2])] = h
for n, h in heads.items():                       # fast-forward / rebase merges: head sits on main
    if n not in merged_by_commit and h in main_idx: merged_by_commit[n] = (main_idx[h], 'fast-forward', None)

# rebase merges rewrite SHAs: match PR commits to main commits by patch-id (content), then by author+subject
def patch_ids(shas):
    if not shas: return {}
    lg = subprocess.run(['git', '-C', args.repo, 'show', '--no-color', '--format=commit %H', *shas], capture_output=True, text=True).stdout
    pid = subprocess.run(['git', '-C', args.repo, 'patch-id', '--stable'], input=lg, capture_output=True, text=True).stdout
    return {l.split()[1]: l.split()[0] for l in pid.splitlines()}
# every commit that reached main (any ancestry), in the window plus a week of slack
since = (T0.timestamp() - 86400); until = T0.timestamp() + TEND + 7 * 86400
all_main = []
for l in git('log', args.main, '--no-merges', '--format=%H%x1f%an%x1f%s%x1f%ct').splitlines():
    h, an, subj, ct = l.split(SEP)
    if since <= int(ct) <= until: all_main.append((h, an, subj))
main_pid = {v: k for k, v in patch_ids([h for h, _, _ in all_main]).items()}   # patch-id -> sha
main_subj = {(an, subj): h for h, an, subj in all_main}
def is_anc(a, b):
    return subprocess.run(['git', '-C', args.repo, 'merge-base', '--is-ancestor', a, b]).returncode == 0
def landing_index(sha):
    # first first-parent main commit that contains sha
    lo, hi = 0, len(main) - 1
    if not is_anc(sha, main[hi]['h']): return None
    while lo < hi:
        mid = (lo + hi) // 2
        if is_anc(sha, main[mid]['h']): hi = mid
        else: lo = mid + 1
    return lo

def main_before(t):
    i = bisect.bisect_left(main_ct, t) - 1
    return main[max(i, 0)]['h']

# ---------- village-side PR events ----------
village = TL['prs']
def vevents(n, kinds):
    return [e for e in village.get(str(n), {}).get('events', []) if e[2] in kinds]

# ---------- assemble PRs ----------
prs = []
for n in sorted(heads):
    h = heads[n]
    v_open = vevents(n, {'pr_create'})
    mb = merged_by_commit.get(n)
    if mb:
        mi, kind, branch = mb; mc = main[mi]
        base = mc['p'][0] if kind in ('merge', 'squash') and mc['p'] else None
    else:
        mi = kind = branch = mc = None; base = None
    open_t = v_open[0][0] if v_open else None
    if base is None:
        ref_t = open_t if open_t is not None else (mc['ct'] if mc else None)
        if ref_t is None:
            ref_t = sec_iso(git('log', '-1', '--format=%aI', h).strip())
        base = main_before(ref_t)
    out = git('rev-list', '--first-parent', '--no-merges', '--format=%H%x1f%an%x1f%aI%x1f%s', h, '^' + base)
    commits = []
    for l in out.splitlines():
        if l.startswith('commit '): continue
        ch, an, ai, s = l.split(SEP, 3)
        commits.append([sec_iso(ai), agent_of(an), s[:140], ch[:9]])
    commits.sort()
    if not mb and commits:   # look for a rebase merge of this PR's commits
        full = [l.split(SEP)[0] for l in out.splitlines() if not l.startswith('commit ')]
        pids = patch_ids(full)
        hits = [main_pid.get(pids.get(x)) or main_subj.get((l.split(SEP)[1], l.split(SEP)[3]))
                for x, l in zip(full, [l for l in out.splitlines() if not l.startswith('commit ')])]
        hits = [x for x in hits if x]
        if is_anc(h, main[-1]['h']): hits.append(h)
        if hits:
            idx = [landing_index(x) for x in hits]; idx = [i for i in idx if i is not None]
            if idx:
                mi = max(idx); mc = main[mi]
                kind = 'rebase' if mc['h'] in hits else 'via-other'
                mb = (mi, kind, None)
                if kind == 'rebase': merged_by_commit[n] = mb
    truncated = len(commits) > args.max_commits
    commits = commits[-args.max_commits:]
    if kind == 'fast-forward' and not commits: commits = []
    # time window filter: keep PRs with any activity inside the goal window
    times = [c[0] for c in commits] + ([open_t] if open_t is not None else []) + ([mc['ct']] if mc else [])
    if not times or max(times) < 0 or min(times) > TEND: continue

    v_merge = vevents(n, {'pr_merge'}); v_close = vevents(n, {'pr_close'}); p_via = None
    merged_t = mc['ct'] if mc else None
    merger = None
    if mc:
        near = [e for e in v_merge if abs(e[0] - merged_t) < 900]
        if near: merger = min(near, key=lambda e: abs(e[0] - merged_t))[1]
        elif kind in ('merge', 'via-other') and mc['subj'].startswith('Merge'): merger = agent_of(mc['an'])
        if kind == 'via-other': p_via = re.search(r'#(\d+)', mc['subj'])
    closed_t = None
    if not mc and v_close: closed_t = v_close[-1][0]
    # author = who wrote most of the branch's commits; opener = who ran `gh pr create` (when we saw it)
    author = collections.Counter(c[1] for c in commits).most_common(1)[0][0] if commits else (v_open[0][1] if v_open else None)
    opener = v_open[0][1] if v_open else author
    if mc and kind == 'merge':
        title = (mc['body'].split('\n')[0] if mc['body'] else '') or (commits[-1][2] if commits else '')
    elif mc and kind == 'squash':
        title = re.sub(r'\s*\(#\d+\)$', '', mc['subj'])
    else:
        title = commits[-1][2] if commits else git('log', '-1', '--format=%s', h).strip()
    reviews = [[e[0], e[1], {'pr_approve': 'approve', 'pr_request_changes': 'changes', 'pr_review': 'review', 'pr_comment': 'comment'}[e[2]]]
               for e in vevents(n, {'pr_approve', 'pr_request_changes', 'pr_review', 'pr_comment'})]
    chat = [[e[0], e[1]] for e in vevents(n, {'chat'})]
    start = min([t for t in (open_t, commits[0][0] if commits else None) if t is not None] or [merged_t or 0])
    end = merged_t if merged_t is not None else closed_t
    prs.append({'n': n, 'title': title[:160], 'branch': branch, 'author': author, 'opener': opener, 'open': open_t,
                'start': start, 'end': end, 'state': ('landed' if kind == 'via-other' else 'merged') if mc else ('closed' if closed_t is not None else 'unmerged'),
                'external': bool(commits) and all(c[1] >= NA for c in commits) and not v_open,
                'mergeKind': kind, 'merger': merger, 'via': int(p_via.group(1)) if p_via else None, 'mainSha': mc['h'][:9] if mc else None, 'commits': commits, 'truncated': truncated,
                'reviews': reviews, 'chat': chat})

# direct commits on main (not attributable to a PR merge)
pr_main = {merged_by_commit[p['n']][0] for p in prs if p['n'] in merged_by_commit}
direct = [[m['ct'], agent_of(m['an']), m['subj'][:140], m['h'][:9]] for i, m in enumerate(main)
          if i not in pr_main and not m['subj'].startswith('Merge ') and -3600 <= m['ct'] <= TEND]

# ---------- lane packing (git-graph tracks) ----------
GAPS = 120
prs.sort(key=lambda p: p['start'])
lane_end = []
for p in prs:
    last = max([p['end'] or 0, p['start']] + [c[0] for c in p['commits']] + [r[0] for r in p['reviews']])
    p['last'] = last
    for li, le in enumerate(lane_end):
        if le + GAPS < p['start']: p['lane'] = li; lane_end[li] = last; break
    else: p['lane'] = len(lane_end); lane_end.append(last)

# saboteur overlay: mark egg PRs
if args.saboteurs and os.path.exists(args.saboteurs):
    byn = {p['n']: p for p in prs}
    for day in json.load(open(args.saboteurs))['days']:
        for sb in day['saboteurs']:
            for e in sb['eggs']:
                if e['pr'] in byn: byn[e['pr']]['egg'] = {'agent': sb['agent'], 'what': e['what'], 'outcome': e['outcome'], 'date': day['date']}

agents = [{'name': a['name'], 'family': family(a['name'])} for a in AG] + [{'name': x, 'family': family(x), 'external': True} for x in extra]
sab = json.load(open(args.saboteurs)) if args.saboteurs and os.path.exists(args.saboteurs) else None
out = {'meta': {**TL['meta'], 'repo': 'ai-village-agents/rpg-game', 'lanes': len(lane_end)},
       'agents': agents, 'segments': TL['segments'], 'prs': prs, 'direct': direct, 'saboteurs': sab}
json.dump(out, open(args.out, 'w'), separators=(',', ':'))

c = collections.Counter(p['state'] for p in prs); k = collections.Counter(p['mergeKind'] for p in prs)
print(f"PRs {len(prs)} {dict(c)} merge kinds {dict(k)} lanes {len(lane_end)} direct-to-main {len(direct)}")
print('unmapped git authors:', extra)
print('external PRs:', sum(p['external'] for p in prs), ' PRs with unknown merger:', sum(1 for p in prs if p['state'] == 'merged' and p['merger'] is None),
      ' unknown opener:', sum(1 for p in prs if p['opener'] is None),
      ' commits:', sum(len(p['commits']) for p in prs), ' truncated:', sum(p['truncated'] for p in prs))
