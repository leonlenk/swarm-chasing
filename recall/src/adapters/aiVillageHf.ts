// AI Village (huggingface.co/datasets/aidigestorg/ai-village) → RECALL.
// Pure and deterministic: takes already-sliced table rows for one time window and returns a
// DataSource. All fetching/slicing lives in scripts/fetch-ai-village.ts.
//
// Honesty rules (also recorded in meta.notes):
//  - Session boundaries, turns and chat text are OBSERVED records.
//  - Attaching a chat message to the speaker's open session is INFERRED.
//  - Claims/corrections are INFERRED from fixed phrase patterns + an explicit URL.
//  - Tool verdicts come only from deterministic output patterns (each carries `rule`);
//    stderr alone is never treated as failure.

import type { Agent, DataSource, RecallEvent, TaskStatus } from '../model/types';

export interface HfAgent { id: string; name: string; model_string?: string | null }
export interface HfSession {
  id: string; agent_id: string; session_goal?: string | null;
  short_displayed_session_goal?: string | null; created_at: string;
}
export interface HfEvent { id: string; event_index: number; created_at: string; data: Record<string, unknown> }
export interface HfChat {
  id: string; speaker_type: string; agent_speaker_id?: string | null; content?: string | null;
  room_id?: string | null; created_at: string;
}
export interface HfTurn {
  id: string; session_id: string; created_at: string;
  agent_action?: Record<string, unknown> | null; output?: string | null; error?: string | null;
}

export interface HfWindowInput {
  id: string;
  label: string;
  goal?: string;
  window: { from: string; to: string };
  agents: HfAgent[];
  /** Sessions created in [from − lookback, to]. */
  sessions: HfSession[];
  /** START_USING_COMPUTER / STOP_USING_COMPUTER / CONSOLIDATE events in [from − lookback, to]. */
  boundaries: HfEvent[];
  /** chat_messages rows in [from, to]. */
  chats: HfChat[];
  /** computer_use_turns rows for the selected sessions (any; non-verdict turns are ignored). */
  turns: HfTurn[];
  generatedAt: string;
  rows?: Record<string, number>;
  maxEvents?: number;
}

// ---------- shared, deterministic helpers (mirrors scripts/scan_ai_village.py) ----------

export const URL_RE = /https?:\/\/[^\s'"<>)\]`*,]+/g;
const HTTP_RE = /HTTP\/[\d.]+\s+(\d{3})/g;
export const CLAIM_RE =
  /\b(is (?:now )?live|are (?:now )?live|now live|went live|deployed|published|is up at|are up at|shipped|launched|fixed|is working(?! on)|are working(?! on)|verified)\b/i;
/** A message reporting a problem is not a completion claim, even if it says "verified". */
const NEGATIVE_RE =
  /\b(404|not found|broken|fail(?:ed|ing|s|ure)?|errors?|is down|are down|isn['’]?t live|not (?:yet )?live|still (?:returning|showing)|unreachable)\b/i;
/** Future or conditional wording: "will be live at", "once set up", "soon". Not a completion claim. */
const FUTURE_RE = /\b(will (?:be|go)|once\b|soon\b|going to|planning|plan to|about to|should be|when (?:it|this) (?:is|goes))\b/i;

/**
 * URLs a message actually claims: the completion phrase must be in the same paragraph as the URL,
 * and that paragraph must not be future/conditional or report a failure.
 */
export function claimedUrls(content: string, urls: string[]): string[] {
  const paragraphs = content.split(/\n\s*\n|\n(?=\s*[-*•]|\s*\*\*)/);
  return urls.filter((u) => paragraphs.some((p) => {
    const inP = urlsIn(p).includes(u);
    return inP && CLAIM_RE.test(p) && !FUTURE_RE.test(p) && !NEGATIVE_RE.test(p);
  }));
}

const CORRECTION_RE =
  /\b(correction|i was wrong|i misspoke|my mistake|that was incorrect|retract(?:ing)?|is (?:actually )?(?:down|broken|still 404|returning 404)|not (?:actually )?live)\b/i;

/** UTC timestamps in the dataset have no zone suffix ("2025-12-29 18:49:21.291984"). */
export function hfTime(s: string): string {
  if (!s) return new Date(0).toISOString();
  const iso = /[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s.replace(' ', 'T') : `${s.replace(' ', 'T')}Z`;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? new Date(0).toISOString() : d.toISOString();
}

/**
 * Normalise a URL for "is this page live" matching: https, lower-case host, no fragment,
 * no query string (agents add cache-busters like ?v=1), no trailing slash.
 */
export function normUrl(u: string): string {
  const s = u.replace(/[.:;!?]+$/, '');
  const m = /^(https?):\/\/([^/?#]+)([^?#]*)/.exec(s);
  if (!m) return s;
  return `https://${m[2].toLowerCase()}${m[3].replace(/\/+$/, '')}`;
}

/** Templated or shell-expanded URLs ("https://x/{name}", "$URL", "https://[^") are not checkable. */
const concrete = (u: string) => !/[{}[\]$<>*\\]/.test(u) && /^https?:\/\/[a-z0-9.-]+\.[a-z]{2,}/i.test(u);
export const urlsIn = (s: string) => [...new Set((s.match(URL_RE) ?? []).filter(concrete).map(normUrl))];
/** Hosts whose URLs are repositories/PRs, not deployed pages; "live" claims about them are not checked. */
const NOT_A_PAGE = /^https:\/\/(github\.com|gitlab\.com|www\.github\.com)(\/|$)/;
const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s);
const firstLine = (s: string) => s.split('\n').find((l) => l.trim() && !l.trim().startsWith('#'))?.trim() ?? s.trim();

export interface Verdict {
  rule: 'http-status' | 'test-summary' | 'git-push' | 'traceback';
  category: 'verification' | 'execution';
  outcome: 'pass' | 'fail';
  subject?: { artifact: string; version: string };
  summary: string;
}

/** Deterministic verdict for a bash turn, or null when the output carries no unambiguous signal. */
/**
 * Linear-time test summary detection (pytest "==== 3 failed, 409 passed in 2.1s ====", jest/vitest "Tests: 1 failed, 20 passed").
 * Scans lines from the end (final summaries come last). Replaces a backtracking regex that hung for minutes
 * on one multi-megabyte tool output in computer_use_turns.
 */
export function testSummary(text: string): { failed: number; passed: number } | null {
  const lines = text.split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (line.length < 8 || line.length > 300) continue;
    const pytest = line.startsWith('==') && line.endsWith('==') && / in [\d.]+s\b/.test(line);
    const jest = /^Tests?:?\s/.test(line);
    if (!pytest && !jest) continue;
    const f = /(\d+) failed/.exec(line);
    const p = /(\d+) passed/.exec(line);
    if (f || p) return { failed: Number(f?.[1] ?? 0), passed: Number(p?.[1] ?? 0) };
  }
  return null;
}

export function classifyTurn(t: Pick<HfTurn, 'agent_action' | 'output' | 'error'>): Verdict | null {
  const cmd = typeof t.agent_action?.command === 'string' ? (t.agent_action.command as string) : null;
  if (!cmd) return null;
  const out = t.output ?? '';
  const err = t.error ?? '';
  const text = `${out}\n${err}`;

  if (/\bcurl\b/.test(cmd)) {
    const urls = urlsIn(cmd);
    let statuses = [...text.matchAll(HTTP_RE)].map((m) => Number(m[1]));
    // `curl -w '%{http_code}'` prints a bare status code instead of an HTTP status line.
    if (!statuses.length && cmd.includes('%{http_code}')) {
      const bare = [...out.matchAll(/^\s*([1-5]\d\d)\s*$/gm)].map((m) => Number(m[1]));
      if (bare.length === 1) statuses = bare;
    }
    if (urls.length === 1 && statuses.length) {
      const code = statuses[statuses.length - 1];
      return {
        rule: 'http-status', category: 'verification', outcome: code < 400 ? 'pass' : 'fail',
        subject: { artifact: urls[0], version: 'live' },
        summary: `HTTP ${code} from ${urls[0]}`,
      };
    }
  }

  const summary = testSummary(text);
  if (summary) {
    const { failed, passed } = summary;
    const repo = /\bcd\s+([^\s;&|]+)/.exec(cmd)?.[1] ?? 'working directory';
    return {
      rule: 'test-summary', category: 'verification', outcome: failed > 0 ? 'fail' : 'pass',
      subject: { artifact: `tests:${repo}`, version: 'working-tree' },
      summary: `${failed} failed, ${passed} passed (${repo})`,
    };
  }

  if (/\bgit\s+push\b/.test(cmd)) {
    const remote = /To (https?:\/\/\S+)/.exec(text)?.[1];
    if (text.includes('! [rejected]') || /error: failed to push/.test(text)) {
      return { rule: 'git-push', category: 'execution', outcome: 'fail',
        subject: remote ? { artifact: normUrl(remote), version: 'push' } : undefined, summary: `push rejected${remote ? ` by ${normUrl(remote)}` : ''}` };
    }
    const ok = /([0-9a-f]{7,})\.\.([0-9a-f]{7,})\s+\S+\s+->\s+\S+/.exec(text) ?? /\* \[new branch\]\s+\S+\s+->\s+\S+/.exec(text);
    if (ok) {
      return { rule: 'git-push', category: 'execution', outcome: 'pass',
        subject: remote ? { artifact: normUrl(remote), version: ok[2] ?? 'new-branch' } : undefined,
        summary: `pushed${ok[2] ? ` ${ok[1]}..${ok[2]}` : ' new branch'}${remote ? ` to ${normUrl(remote)}` : ''}` };
    }
  }

  if (text.includes('Traceback (most recent call last)') || /: command not found\b/.test(text)) {
    const last = text.trim().split('\n').filter(Boolean).pop() ?? '';
    return { rule: 'traceback', category: 'execution', outcome: 'fail', summary: clip(last, 140) };
  }
  return null;
}

// ---------- ingestion helpers (raw-line, no full JSON parse) ----------

/** Commands whose output can carry a verdict. Avoids JSON-parsing the huge agent_messages of other turns. */
export const INTERESTING_CMD = /\bcurl\b|git\s+push|pytest|jest|vitest|npm (?:run )?test|node --test|\bpython3?\b|\bnode\b|\bnpx\b/;

/** The agent_action.command string from a raw JSON line, without parsing the whole row. */
export function rawCommand(line: string): string | null {
  const i = line.indexOf('"agent_action"');
  if (i < 0) return null;
  const k = line.indexOf('"command"', i);
  if (k < 0 || k - i > 400) return null; // command must belong to agent_action, which precedes agent_messages
  let j = line.indexOf('"', line.indexOf(':', k) + 1) + 1;
  let out = '';
  for (; j < line.length && out.length < 4000; j++) {
    const ch = line[j];
    if (ch === '\\') { out += line[j + 1] === 'n' ? '\n' : line[j + 1]; j++; continue; }
    if (ch === '"') return out;
    out += ch;
  }
  return out;
}

// ---------- agents ----------

const PALETTE = ['#2f6a4f', '#9a5b22', '#3b5f94', '#8e3b5c', '#5a6f22', '#6b4a96', '#227777', '#a2482c',
  '#4c6a9e', '#7d6b23', '#984848', '#3c7f5b', '#7a4f7f', '#2d6e8c', '#8a6c3c', '#5d5d9a'];

function provider(model: string): string {
  const m = model.toLowerCase();
  if (m.startsWith('claude-code::')) return 'Anthropic · Claude Code';
  if (m.includes('claude')) return 'Anthropic';
  if (m.startsWith('gpt') || /^o\d/.test(m)) return 'OpenAI';
  if (m.includes('gemini')) return 'Google';
  if (m.includes('deepseek')) return 'DeepSeek';
  if (m.includes('grok')) return 'xAI';
  if (m.includes('glm') || m.startsWith('z-ai')) return 'Z.ai';
  if (m.startsWith('meta') || m.includes('muse')) return 'Meta';
  if (m.startsWith('tinker')) return 'Fine-tuned (Tinker)';
  return 'Model';
}

function roleFor(model?: string | null): string {
  if (!model) return 'AI Village agent';
  const short = model.replace(/^claude-code::/, '').replace(/^tinker:\/\/.*$/, 'fine-tuned weights').replace(/^[\w-]+\//, '');
  return clip(`${provider(model)} · ${short}`, 48);
}

const HUMAN_ID = 'humans';

// ---------- main ----------

type Unsequenced = RecallEvent extends infer E ? (E extends RecallEvent ? Omit<E, 'sequence'> : never) : never;
interface Draft { ts: string; prio: number; order: number; ev: Unsequenced }

export function adaptAiVillageWindow(input: HfWindowInput): DataSource {
  const from = Date.parse(input.window.from);
  const to = Date.parse(input.window.to);
  const inWindow = (iso: string) => { const t = Date.parse(iso); return t >= from && t < to; };
  const agentRows = new Map(input.agents.map((a) => [a.id, a]));
  const notes: string[] = [];
  const drafts: Draft[] = [];
  let order = 0;
  const push = (ts: string, prio: number, ev: Unsequenced) => drafts.push({ ts, prio, order: order++, ev });

  // --- sessions → tasks. Keep sessions created in-window plus each agent's session open at window start.
  const sessions = input.sessions
    .map((s) => ({ ...s, at: hfTime(s.created_at) }))
    .sort((a, b) => a.at.localeCompare(b.at));
  const byAgent = new Map<string, typeof sessions>();
  for (const s of sessions) byAgent.set(s.agent_id, [...(byAgent.get(s.agent_id) ?? []), s]);

  const boundaries = input.boundaries
    .map((e) => ({ ...e, at: hfTime(e.created_at), type: String(e.data.actionType), agent: String(e.data.agentId ?? '') }))
    .sort((a, b) => a.at.localeCompare(b.at) || a.event_index - b.event_index);

  const chosen: (typeof sessions[number] & { end?: { at: string; stop?: typeof boundaries[number] } })[] = [];
  for (const [agentId, list] of byAgent) {
    const before = list.filter((s) => Date.parse(s.at) < from).pop();
    const inside = list.filter((s) => inWindow(s.at));
    for (const s of [...(before ? [before] : []), ...inside]) {
      const next = boundaries.find((b) => b.agent === agentId && b.at > s.at &&
        (b.type === 'STOP_USING_COMPUTER' || b.data.computerUseSessionId !== s.id));
      const end = next && Date.parse(next.at) < to ? { at: next.at, stop: next.type === 'STOP_USING_COMPUTER' ? next : undefined } : undefined;
      if (before === s && end && Date.parse(end.at) < from) continue; // closed before the window opened
      chosen.push({ ...s, end });
    }
  }
  chosen.sort((a, b) => a.at.localeCompare(b.at));

  const taskIdOf = new Map<string, string>();
  const usedTaskIds = new Set<string>();
  const activeAgents = new Set<string>();
  for (const s of chosen) {
    let tid = `S-${s.id.slice(0, 6)}`;
    while (usedTaskIds.has(tid)) tid += 'x';
    usedTaskIds.add(tid);
    taskIdOf.set(s.id, tid);
    activeAgents.add(s.agent_id);
    const goal = (s.session_goal ?? '').trim();
    const title = clip((s.short_displayed_session_goal ?? '').trim() || goal || 'Computer-use session', 70);
    const startedBefore = Date.parse(s.at) < from;
    push(s.at, 0, {
      id: `session/${s.id}`, timestamp: s.at, agentId: s.agent_id, taskId: null, type: 'task_created',
      provenance: 'observed', evidenceRefs: [],
      text: `Computer-use session started${startedBefore ? ' (before this window)' : ''}. Goal: ${clip(goal || title, 600)}`,
      payload: { tasks: [{ taskId: tid, title, owner: s.agent_id }] },
    });
    push(s.at, 1, {
      id: `session/${s.id}#start`, timestamp: s.at, agentId: s.agent_id, taskId: tid, type: 'status_updated',
      provenance: 'observed', evidenceRefs: [], text: 'Session running.',
      payload: { status: 'in_progress' as TaskStatus }, statusAfter: 'in_progress',
    });
    if (s.end) {
      const stop = s.end.stop;
      const summary = stop ? String(stop.data.summary ?? '').trim() : '';
      if (summary) {
        push(s.end.at, 2, {
          id: `event/${stop!.event_index}`, timestamp: s.end.at, agentId: s.agent_id, taskId: tid, type: 'message',
          provenance: 'declared', evidenceRefs: [], payload: {},
          text: `Session summary (agent's own words): ${clip(summary.replace(/\s+\n/g, '\n'), 900)}`,
        });
      }
      push(s.end.at, 3, {
        id: `session/${s.id}#end`, timestamp: s.end.at, agentId: s.agent_id, taskId: tid, type: 'status_updated',
        provenance: 'observed', evidenceRefs: [],
        text: stop ? 'Session stopped by the agent. Ending a session does not mean its goal was achieved.'
          : 'Session replaced by the agent\'s next session (memory consolidation or new start).',
        payload: { status: 'ended' as TaskStatus }, statusAfter: 'ended',
      });
    }
  }

  // Which session is open for an agent at time t (used to attach chat — inferred).
  const openTask = (agentId: string, at: string): string | null => {
    let best: string | null = null;
    for (const s of chosen) {
      if (s.agent_id !== agentId || s.at > at) continue;
      if (s.end && s.end.at <= at) continue;
      best = taskIdOf.get(s.id) ?? best;
    }
    return best;
  };

  // --- turns → verdict-bearing tool results (exact session mapping, observed).
  const sessionOf = new Map(chosen.map((s) => [s.id, s]));
  let turnsWithVerdict = 0;
  for (const t of input.turns) {
    const s = sessionOf.get(t.session_id);
    if (!s) continue;
    const at = hfTime(t.created_at);
    if (!inWindow(at)) continue;
    const v = classifyTurn(t);
    if (!v) continue;
    turnsWithVerdict++;
    const cmd = String(t.agent_action?.command ?? '');
    const output = [`$ ${clip(cmd.trim(), 500)}`, '', clip(((t.output ?? '') + (t.error ? `\n[stderr]\n${t.error}` : '')).trim(), 1400)].join('\n');
    activeAgents.add(s.agent_id);
    push(at, 4, {
      id: `turn/${t.id}`, timestamp: at, agentId: s.agent_id, taskId: taskIdOf.get(s.id)!, type: 'tool_result',
      provenance: 'observed', evidenceRefs: [],
      text: `${clip(firstLine(cmd), 120)} → ${v.summary}`,
      payload: { tool: 'bash', runId: `turn ${t.id.slice(0, 8)}`, category: v.category, rule: v.rule,
        subject: v.subject, outcome: v.outcome, output },
    });
  }

  // --- chat → messages / claims / corrections.
  const names = input.agents
    .map((a) => ({ id: a.id, name: a.name, norm: a.name.replace(/[‐-―−]/g, '-') }))
    .filter((a) => a.name.length >= 2);
  const claimsBy = new Map<string, { claimId: string; eventId: string; url: string }[]>();
  let humanCount = 0;
  let claimCount = 0;
  let correctionCount = 0;
  const chats = input.chats
    .map((c) => ({ ...c, at: hfTime(c.created_at) }))
    .filter((c) => inWindow(c.at) && (c.content ?? '').trim())
    .sort((a, b) => a.at.localeCompare(b.at));
  for (const c of chats) {
    const content = (c.content ?? '').trim();
    if (c.speaker_type !== 'agent' || !c.agent_speaker_id) {
      humanCount++;
      push(c.at, 5, {
        id: `chat/${c.id}`, timestamp: c.at, agentId: HUMAN_ID, taskId: null, type: 'message',
        provenance: 'observed', evidenceRefs: [], payload: {}, text: clip(content, 1200),
      });
      continue;
    }
    const speaker = c.agent_speaker_id;
    activeAgents.add(speaker);
    const taskId = openTask(speaker, c.at);
    const normContent = content.replace(/[‐-―−]/g, '-');
    const mentions = names
      .filter((a) => a.id !== speaker && (normContent.includes(`@${a.norm}`) || (a.norm.length >= 6 && new RegExp(`(^|[^\\w-])${a.norm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\w-])`).test(normContent))))
      .map((a) => a.id);
    const base = { timestamp: c.at, agentId: speaker, taskId, text: clip(content, 1600), mentions: mentions.length ? mentions : undefined };
    const urls = urlsIn(content);
    const pageUrls = urls.filter((u) => !NOT_A_PAGE.test(u));

    const prior = claimsBy.get(speaker) ?? [];
    const corrected = CORRECTION_RE.test(content) ? prior.filter((p) => urls.includes(p.url)).pop() : undefined;
    if (corrected) {
      correctionCount++;
      push(c.at, 6, {
        ...base, id: `chat/${c.id}`, type: 'correction', provenance: 'inferred',
        evidenceRefs: [corrected.eventId], payload: { supersedes: corrected.claimId },
      });
      continue;
    }
    // A claim names one or two deployed pages; long URL roundups are status lists, not specific claims.
    const claimed = pageUrls.length >= 1 && pageUrls.length <= 2 && !NEGATIVE_RE.test(content) ? claimedUrls(content, pageUrls) : [];
    if (claimed.length) {
      claimed.forEach((url, i) => {
        const claimId = `C-${c.id.slice(0, 5)}${claimed.length > 1 ? `-${i + 1}` : ''}`;
        const eventId = claimed.length > 1 ? `chat/${c.id}#${i + 1}` : `chat/${c.id}`;
        claimCount++;
        prior.push({ claimId, eventId, url });
        push(c.at, 6, {
          ...base, id: eventId, type: 'claim', provenance: 'inferred', evidenceRefs: [],
          payload: { claimId, asserts: 'complete', subject: { artifact: url, version: 'live' } },
        });
      });
      claimsBy.set(speaker, prior);
      continue;
    }
    push(c.at, 5, { ...base, id: `chat/${c.id}`, type: 'message', provenance: 'observed', evidenceRefs: [], payload: {} });
  }

  // --- order, cap volume, sequence.
  drafts.sort((a, b) => a.ts.localeCompare(b.ts) || a.prio - b.prio || a.order - b.order);
  const max = input.maxEvents ?? 1200;
  let kept = drafts;
  let windowTo = input.window.to;
  if (kept.length > max) {
    const before = kept.length;
    const plain = (d: Draft) => d.ev.type === 'message' && !d.ev.mentions?.length;
    const excess = kept.length - max;
    const drop = new Set<Draft>();
    // Drop plain messages evenly across the window first (keeps the timeline representative).
    const plains = kept.filter(plain);
    if (plains.length) {
      const step = plains.length / Math.min(excess, plains.length);
      for (let i = 0; i < plains.length && drop.size < excess; i += step) drop.add(plains[Math.floor(i)]);
    }
    kept = kept.filter((d) => !drop.has(d));
    // Still too many: end the window early rather than losing its beginning, and say so.
    if (kept.length > max) {
      kept = kept.slice(0, max);
      windowTo = kept[kept.length - 1].ts;
    }
    notes.push(`Volume cap: kept ${kept.length} of ${before} records (plain chat messages without mentions thinned first` +
      (windowTo !== input.window.to ? `; window shortened to end at ${windowTo.slice(11, 16)} UTC` : '') + ').');
  }
  // Ensure every task referenced still has its creation event (trimming oldest could orphan).
  const created = new Set(kept.flatMap((d) => (d.ev.type === 'task_created' ? d.ev.payload.tasks.map((t) => t.taskId) : [])));
  kept = kept.filter((d) => !d.ev.taskId || created.has(d.ev.taskId) || d.ev.type === 'task_created');
  const keptIds = new Set(kept.map((d) => d.ev.id));
  const events: RecallEvent[] = kept.map((d, i) => {
    const ev = { ...d.ev, sequence: i + 1 } as RecallEvent;
    if (ev.taskId && !created.has(ev.taskId)) ev.taskId = null;
    ev.evidenceRefs = ev.evidenceRefs.filter((r) => keptIds.has(r));
    return ev;
  });

  const agents: Agent[] = [...activeAgents]
    .filter((id) => agentRows.has(id))
    .sort((a, b) => agentRows.get(a)!.name.localeCompare(agentRows.get(b)!.name))
    .map((id, i) => ({ id, name: agentRows.get(id)!.name, role: roleFor(agentRows.get(id)!.model_string), color: PALETTE[i % PALETTE.length] }));
  if (humanCount) agents.push({ id: HUMAN_ID, name: 'Human participants', role: 'Viewer chat · usernames omitted', color: '#8a8f88' });

  notes.push(
    'Tasks are computer-use sessions; start/stop are observed system records. "Session ended" makes no claim the goal was met.',
    'Chat messages are attached to the speaker\'s open session by time (inferred attachment).',
    `Claims (${claimCount}) are inferred: an agent message with completion phrasing that names a URL. Corrections (${correctionCount}) are inferred: the same agent, explicit correction phrasing, same URL.`,
    `Tool verdicts (${turnsWithVerdict}) come only from deterministic output patterns: http-status, test-summary, git-push, traceback. Other turns are not shown.`,
    '@-mentions are matched against agent display names (inferred).',
  );
  if (humanCount) notes.push(`${humanCount} human chat messages are grouped under "Human participants"; usernames are omitted.`);

  return {
    id: `ai-village:${input.id}`,
    label: input.label,
    kind: 'ai-village',
    description:
      `Real records from the AI Village dataset, ${input.window.from.slice(0, 16).replace('T', ' ')}–${input.window.to.slice(11, 16)} UTC` +
      (input.goal ? ` (village goal: “${clip(input.goal, 90)}”)` : '') + '. Claims and corrections are pattern-inferred; tool verdicts are rule-based.',
    agents,
    events,
    meta: {
      origin: 'huggingface',
      dataset: 'aidigestorg/ai-village',
      citation: 'AI Digest, AI Village dataset (huggingface.co/datasets/aidigestorg/ai-village)',
      window: { from: input.window.from, to: windowTo },
      goal: input.goal,
      generatedAt: input.generatedAt,
      rows: input.rows,
      notes,
    },
  };
}
