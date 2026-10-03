#!/usr/bin/env python3
"""Build a collaboration timeline (tool use + communication) for one AI Village goal.

Usage:
    python3 collab/build_timeline.py --goal "RPG together" --out collab/out/rpg
    python3 collab/build_timeline.py --start "2026-03-05 15:51" --end "2026-03-16 16:21" --out collab/out/x

Reads the AI Village export in ./data and writes <out>/timeline.json (compact,
consumed by collab/timeline.html) plus <out>/stats.txt.

Sources merged:
  events                -> chat (AGENT_TALK/USER_TALK), computer sessions, waits/pauses, room moves
  chat_messages         -> message text + room (source of truth for communication)
  computer_use_turns    -> every tool action (bash, GUI) for standard-scaffold agents
  claude_code_messages  -> tool calls for the Claude Code agent
Derived:
  mentions  (message -> agents named in it)
  PR events (agent x PR number x action), from gh/git commands and chat "#123" refs
"""
import argparse, collections, gzip, json, os, re, subprocess, sys
from datetime import datetime

ap = argparse.ArgumentParser()
ap.add_argument('--data', default=os.path.join(os.path.dirname(__file__), '..', 'data'))
ap.add_argument('--goal', help='substring of village_goals.goal; uses its start/end')
ap.add_argument('--start'); ap.add_argument('--end')
ap.add_argument('--out', required=True)
ap.add_argument('--repo', default='rpg-game', help='repo name used for PR attribution')
args = ap.parse_args()
D = lambda f: os.path.join(args.data, f)

def rows(f):
    with gzip.open(D(f), 'rt') as fh:
        for l in fh: yield json.loads(l)

# ---------- window ----------
goal_text = None
if args.goal:
    for g in rows('village_goals.jsonl.gz'):
        if args.goal.lower() in g['goal'].lower():
            goal_text, S, E = g['goal'], g['start_time'], g['end_time']
            break
    else: sys.exit('goal not found')
else:
    S, E = args.start, args.end
S, E = S[:19], E[:19]
T0 = datetime.fromisoformat(S)
def sec(ts): return int((datetime.fromisoformat(ts[:19]) - T0).total_seconds())
def inwin(ts): return S <= ts[:19] < E
month_prefixes = sorted({S[:7], E[:7]})  # cheap line prefilter for huge files

# ---------- agents / rooms ----------
agents = {a['id']: a for a in rows('agents.jsonl.gz')}
rooms = {r['id']: r['name'] for r in rows('chat_rooms.jsonl.gz')}
AIDX = {}; ALIST = []
def aidx(aid):
    if aid not in AIDX:
        AIDX[aid] = len(ALIST); a = agents.get(aid, {})
        ALIST.append({'id': aid, 'name': a.get('name', aid[:8]), 'model': a.get('model_string', '')})
    return AIDX[aid]
RIDX = {}; RLIST = []
def ridx(rid):
    if rid not in RIDX: RIDX[rid] = len(RLIST); RLIST.append(rooms.get(rid, rid[:8] if rid else '?'))
    return RIDX[rid]

# ---------- events: sessions, idle, chat speakers ----------
sessions = {}   # sid -> [agent, start, end, goal]
idle = []       # [t, agent, kind, seconds]
moves = []      # [t, agent, from_room, to_room]
human_names = {}
for e in rows('events.jsonl.gz'):
    if not inwin(e['created_at']): continue
    d = e['data']; at = d.get('actionType'); t = sec(e['created_at'])
    if at == 'START_USING_COMPUTER' and d.get('computerUseSessionId'):
        sessions[d['computerUseSessionId']] = [aidx(d['agentId']), t, None, d.get('sessionGoal') or '']
    elif at == 'STOP_USING_COMPUTER':
        for sid, s in reversed(list(sessions.items())):
            if s[0] == aidx(d['agentId']) and s[2] is None: s[2] = t; break
    elif at in ('WAIT', 'PAUSE'):
        idle.append([t, aidx(d['agentId']), at.lower(), int(d.get('seconds') or 0)])
    elif at == 'ENTER_ROOM':
        moves.append([t, aidx(d['agentId']), ridx(d.get('previousRoomId')), ridx(d.get('roomId'))])
    elif at == 'USER_TALK' and d.get('messageId'):
        human_names[d['messageId']] = d.get('speakerName') or 'human'

# ---------- mentions ----------
def build_aliases():
    al = []
    for aid, a in agents.items():
        n = a['name']; al.append((n, aid))
        if n.startswith('Claude ') and 'Claude Code' not in n: al.append((n[len('Claude '):], aid))
        if 'Claude Code' in n: al.append(('Claude Code', aid))
    al.sort(key=lambda x: -len(x[0]))
    return [(re.compile(r'(?<![\w.])@?' + re.escape(n) + r'(?!\.?\d)(?![\w-])', re.I), aid) for n, aid in al]
ALIASES = build_aliases()
def mentions(text, speaker):
    found = []; masked = text
    for rx, aid in ALIASES:
        def sub(m):
            if aid != speaker and aid not in found: found.append(aid)
            return '\0' * len(m.group(0))
        masked = rx.sub(sub, masked)
    return found

# "PR 12", "PR #12", "pull/12", "#152" (bare "#n" only for n >= 10, to skip "#1 priority")
PR_RX = re.compile(r'(?:\bPRs?\s*#?|pull/)(\d{1,4})\b|(?<![\w&])#(\d{2,4})\b', re.I)

# ---------- chat ----------
msgs = []   # [t, agent(-1 human), room, text, [mentions], [prs], speakerName?]
for m in rows('chat_messages.jsonl.gz'):
    if not inwin(m['created_at']): continue
    txt = m['content'] or ''
    if m['speaker_type'] == 'agent' and m['agent_speaker_id']:
        a = aidx(m['agent_speaker_id']); spk = m['agent_speaker_id']; nm = None
    else:
        a = -1; spk = None; nm = human_names.get(m['id'], 'human')
    men = [aidx(x) for x in mentions(txt, spk)]
    prs = sorted({int(a or b) for a, b in PR_RX.findall(txt)})
    row = [sec(m['created_at']), a, ridx(m['room_id']), txt[:1200], men, prs]
    if nm: row.append(nm)
    msgs.append(row)
msgs.sort(key=lambda r: r[0])

# ---------- bash classification ----------
RULES = [
    ('github', 'pr_create',  r'gh pr create'),
    ('github', 'pr_merge',   r'gh pr merge'),
    ('github', 'pr_review',  r'gh pr review'),
    ('github', 'pr_comment', r'gh (?:pr|issue) comment'),
    ('github', 'pr_close',   r'gh pr close'),
    ('github', 'inspect',    r'\bgh (?:pr|issue|api|run|repo|search)\b'),
    ('git',    'commit',     r'\bgit (?:-C \S+ )?commit\b'),
    ('git',    'push',       r'\bgit (?:-C \S+ )?push\b'),
    ('edit',   'edit',       r"sed -i|perl -pi|apply_patch|\bcat\s*>|cat <<|\btee\b|>\s*[\w./-]+\.(?:m?js|md|json|html|css|txt|ya?ml)\b|\bcodex\b|python3? - <<"),
    ('git',    'sync',       r'\bgit (?:-C \S+ )?(?:fetch|pull|checkout|switch|rebase|merge|stash|reset|clone|branch|restore|cherry-pick)\b'),
    ('git',    'inspect',    r'\bgit (?:-C \S+ )?(?:diff|log|show|status|blame|rev-parse|ls-files|grep)\b'),
    ('run',    'run',        r'\b(?:node|npm|npx|python3?|pytest|deno|bun)\b'),
    ('read',   'read',       r'\b(?:grep|rg|cat|sed|ls|find|head|tail|wc|nl|less|tree)\b'),
]
RULES = [(c, s, re.compile(p)) for c, s, p in RULES]
def classify_bash(cmd):
    body = '\n'.join(l for l in cmd.split('\n') if not l.strip().startswith('#'))
    for c, s, rx in RULES:
        if rx.search(body):
            if s == 'pr_review':  # split reviews by verdict
                if re.search(r'gh pr review[^\n]*?(--approve|\s-a\b)', body): s = 'pr_approve'
                elif re.search(r'gh pr review[^\n]*?(--request-changes|\s-r\b)', body): s = 'pr_request_changes'
            return c, s
    return 'other', 'other'
def first_line(cmd):
    m = re.search(r'^[^\n]*\bgh (?:pr|issue) [^\n]*', cmd, re.M)  # prefer the gh line when present
    if m: return m.group(0).strip()[:200]
    for l in cmd.split('\n'):
        l = l.strip()
        if l and not l.startswith('#') and not l.startswith('cd '): return l[:160]
    return cmd.strip()[:160]
def pr_nums(cmd, output, sub):
    nums = set()
    for m in re.finditer(r'gh pr \w+ (?:--repo \S+ |-R \S+ )?#?(\d{1,4})\b', cmd): nums.add(int(m.group(1)))
    nums |= {int(x) for x in re.findall(r'pull/(\d{1,4})\b', cmd)}
    if sub == 'pr_create': nums |= {int(x) for x in re.findall(r'pull/(\d{1,4})\b', output or '')}
    return sorted(nums)

GUI = {'left_click','right_click','double_click','triple_click','mouse_move','left_click_drag','scroll','type','key','hold_key','screenshot','wait','get_pixel_coords_of_element','view_clipboard','zoom'}
tools = []   # [t, agent, cat, sub, detail, [prs], ok]
pr_events = collections.defaultdict(list)   # pr -> [[t, agent, action]]

def add_tool(t, a, cat, sub, detail, prs, ok=1):
    tools.append([t, a, cat, sub, detail, prs, ok])
    for p in prs: pr_events[p].append([t, a, sub if cat == 'github' else cat])

# ---------- computer_use_turns (stream, prefiltered) ----------
sess_agent = {}
for s in rows('computer_use_sessions.jsonl.gz'):
    sess_agent[s['id']] = s['agent_id']
pat = '|'.join('"created_at":"%s-' % p for p in month_prefixes)
proc = subprocess.Popen(['bash', '-c', "zcat '%s' | grep -E '%s'" % (D('computer_use_turns.jsonl.gz'), pat)],
                        stdout=subprocess.PIPE, text=True, bufsize=1 << 20)
for l in proc.stdout:
    r = json.loads(l)
    if not inwin(r['created_at']): continue
    aid = sess_agent.get(r['session_id'])
    if not aid: continue
    a = aidx(aid); t = sec(r['created_at']); act = r['agent_action']
    if not isinstance(act, dict): continue
    err = (r.get('error') or '')
    ok = 0 if re.search(r'returncode [1-9]|Traceback|fatal:|error:', err[:400]) else 1
    if act.get('command'):
        cat, sub = classify_bash(act['command'])
        add_tool(t, a, cat, sub, first_line(act['command']), pr_nums(act['command'], r.get('output'), sub), ok)
    elif act.get('action') in GUI:
        add_tool(t, a, 'gui', act['action'], (act.get('text') or '')[:80], [], ok)
    elif act.get('action') == 'send_message_back_to_chat':
        pass  # captured via chat_messages
    elif act.get('action') == 'move_to_room':
        pass  # captured via events
proc.wait()

# ---------- claude code agent ----------
CC_MAP = {'Read': ('read','read'), 'Grep': ('read','read'), 'Glob': ('read','read'),
          'Write': ('edit','edit'), 'Edit': ('edit','edit'), 'MultiEdit': ('edit','edit'),
          'mcp__village__get_events': ('poll','get_events'), 'mcp__village__edit_memory': ('memory','edit_memory'),
          'mcp__village__computer_use': ('gui','computer_use'), 'mcp__village__get_pixel_coordinates': ('gui','pixel'),
          'mcp__village__search_history': ('memory','search_history'), 'TodoWrite': ('memory','todo')}
SKIP = {'mcp__village__chat_message', 'mcp__village__start_computer_session', 'mcp__village__stop_computer_session', 'mcp__village__move_to_room'}
with gzip.open(D('claude_code_messages.jsonl.gz'), 'rt') as fh:
    for l in fh:
        if not any(('"created_at":"%s-' % p) in l or ('%s-' % p) in l[:400] for p in month_prefixes): continue
        r = json.loads(l)
        if not inwin(r['created_at']): continue
        msg = (r.get('content') or {}).get('message') or {}
        cont = msg.get('content') if isinstance(msg, dict) else None
        if not isinstance(cont, list): continue
        for b in cont:
            if not (isinstance(b, dict) and b.get('type') == 'tool_use'): continue
            n = b.get('name'); inp = b.get('input') or {}
            if n in SKIP: continue
            a = aidx(r['agent_id']); t = sec(r['created_at'])
            if n in ('Bash', 'mcp__village__bash'):
                cmd = str(inp.get('command', ''))
                cat, sub = classify_bash(cmd)
                add_tool(t, a, cat, sub, first_line(cmd), pr_nums(cmd, '', sub))
            else:
                cat, sub = CC_MAP.get(n, ('other', n))
                det = str(inp.get('file_path') or inp.get('pattern') or inp.get('query') or '')[:120]
                add_tool(t, a, cat, sub, det, [])

tools.sort(key=lambda r: r[0])

# chat references to PRs count as PR activity too
for m in msgs:
    if m[1] >= 0:
        for p in m[5]: pr_events[p].append([m[0], m[1], 'chat'])
for p in pr_events: pr_events[p].sort()

# PR author = first agent with pr_create on it
pr_info = {}
for p, evs in pr_events.items():
    author = next((a for t, a, k in evs if k == 'pr_create'), None)
    merger = next((a for t, a, k in evs if k == 'pr_merge'), None)
    pr_info[p] = {'author': author, 'merger': merger, 'events': evs}

# ---------- active-hour segments (for a compressed time axis) ----------
ts = sorted([r[0] for r in tools] + [m[0] for m in msgs])
segs = []; GAP = 45 * 60
for t in ts:
    if segs and t - segs[-1][1] <= GAP: segs[-1][1] = t
    else: segs.append([t, t])

for s in sessions.values():
    if s[2] is None: s[2] = s[1]

out = {
    'meta': {'goal': goal_text, 'start': S, 'end': E, 'built': datetime.utcnow().isoformat(timespec='seconds'),
             'tool_fields': ['t','agent','cat','sub','detail','prs','ok'],
             'msg_fields': ['t','agent','room','text','mentions','prs','humanName?']},
    'agents': ALIST, 'rooms': RLIST, 'segments': segs,
    'sessions': sorted(sessions.values(), key=lambda s: s[1]),
    'tools': tools, 'msgs': msgs, 'idle': idle, 'moves': moves,
    'prs': {str(k): v for k, v in sorted(pr_info.items())},
}
os.makedirs(args.out, exist_ok=True)
with open(os.path.join(args.out, 'timeline.json'), 'w') as f: json.dump(out, f, separators=(',', ':'))

# ---------- stats ----------
st = []
st.append(f"window {S} .. {E}  goal: {goal_text}")
st.append(f"agents {len(ALIST)}  msgs {len(msgs)}  tools {len(tools)}  sessions {len(sessions)}  PRs {len(pr_info)}  segments {len(segs)}")
st.append("tool categories: " + str(collections.Counter(r[2] for r in tools).most_common()))
st.append("per agent (tools / msgs / mentions given / mentions received):")
mg = collections.Counter(); mr = collections.Counter()
for m in msgs:
    for x in m[4]: mg[m[1]] += 1; mr[x] += 1
tc = collections.Counter(r[1] for r in tools); mc = collections.Counter(m[1] for m in msgs)
for i, a in enumerate(ALIST):
    st.append(f"  {a['name']:28s} {tc[i]:6d} {mc[i]:5d} {mg[i]:5d} {mr[i]:5d}")
open(os.path.join(args.out, 'stats.txt'), 'w').write('\n'.join(st) + '\n')
print('\n'.join(st))
