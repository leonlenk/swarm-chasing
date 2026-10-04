// Adapter for exports of the AI Village dataset (huggingface.co/datasets/aidigestorg/ai-village).
// The dataset is gated, so field access is tolerant of column-name variants described in
// its README: chat_messages (room, speaker, content, timestamp), events (data.actionType,
// event_index), and computer_use_sessions (session_goal, agent).
//
// Everything this adapter infers from free text is marked provenance "inferred". Inferred
// claims carry no artifact version, so the monitors — which require explicit version and
// claim references — will not fire on them. That is intentional.

import type { Agent, DataSource, RecallEvent, TaskStatus } from '../model/types';

type Row = Record<string, unknown>;

const PALETTE = ['#7aa2f7', '#c099ff', '#4fd6be', '#ff9e64', '#e0af68', '#86e1fc', '#fca7ea', '#c3e88d'];
const MAX_EVENTS = 600;

const COMPLETION = /\b(i(?:'ve| have)? (?:completed|finished|fixed|deployed|published|submitted)|(?:is|are) (?:now )?(?:done|complete|live|fixed)|successfully (?:\w+ed))\b/i;
const CORRECTION = /\b(correction:|i was wrong|i misreported|i misspoke|that was incorrect|retract(?:ing)? (?:my|that)|my earlier (?:report|message) was wrong)\b/i;

function pick(row: Row, keys: string[]): unknown {
  for (const k of keys) {
    const v = k.split('.').reduce<unknown>((o, p) => (o && typeof o === 'object' ? (o as Row)[p] : undefined), row);
    if (v !== undefined && v !== null && v !== '') return v;
  }
  return undefined;
}
const str = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'unknown';

function toIso(v: unknown): string {
  if (typeof v === 'number') return new Date(v > 1e12 ? v : v * 1000).toISOString();
  const raw = str(v);
  // AI Village timestamps are UTC without a zone suffix ("2025-12-29 18:49:21.29").
  const d = new Date(/^\d{4}-\d\d-\d\d[ T]\d\d:\d\d(:\d\d(\.\d+)?)?$/.test(raw) ? `${raw.replace(' ', 'T')}Z` : raw);
  return Number.isNaN(d.getTime()) ? new Date(0).toISOString() : d.toISOString();
}

export async function readRows(file: File): Promise<Row[]> {
  let text: string;
  if (file.name.endsWith('.gz')) {
    const stream = file.stream().pipeThrough(new DecompressionStream('gzip'));
    text = await new Response(stream).text();
  } else {
    text = await file.text();
  }
  const trimmed = text.trimStart();
  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) return parsed as Row[];
      if (parsed && Array.isArray(parsed.events) && Array.isArray(parsed.agents)) return [{ __recallDocument: parsed }];
      return flattenTranscript(parsed);
    } catch {
      /* fall through to JSONL */
    }
  }
  return text.split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l) as Row);
}

/** village-transcript.json is a per-day structure; collect any objects that look like utterances. */
function flattenTranscript(node: unknown, out: Row[] = []): Row[] {
  if (Array.isArray(node)) node.forEach((n) => flattenTranscript(n, out));
  else if (node && typeof node === 'object') {
    const r = node as Row;
    if (pick(r, ['content', 'message', 'text']) && pick(r, ['speaker', 'agent', 'agent_name', 'author', 'name'])) out.push(r);
    else Object.values(r).forEach((v) => flattenTranscript(v, out));
  }
  return out;
}

export function adaptAiVillage(rows: Row[], fileName: string): DataSource {
  const agents = new Map<string, Agent>();
  const agentFor = (raw: unknown): string => {
    const name = str(raw) || 'unknown';
    const id = slug(name);
    if (!agents.has(id)) agents.set(id, { id, name, role: 'AI Village participant', color: PALETTE[agents.size % PALETTE.length] });
    return id;
  };

  interface Draft { ts: string; order: number; build: (seq: number, id: string) => RecallEvent }
  const drafts: Draft[] = [];
  const openTask = new Map<string, string>();
  const lastClaim = new Map<string, string>();
  let order = 0;

  // Normalise rows into time-ordered "utterances" and "sessions".
  type Item =
    | { kind: 'session'; ts: string; end?: string; agent: string; goal: string; sid: string; order: number }
    | { kind: 'talk'; ts: string; agent: string; text: string; sid: string; order: number };
  const items: Item[] = [];
  for (const r of rows) {
    const sid = str(pick(r, ['id', 'uuid', 'message_id', 'event_index'])) || String(order);
    const goal = pick(r, ['session_goal', 'data.session_goal', 'data.goal']);
    const agentRaw = pick(r, ['agent_name', 'speaker', 'agent', 'agent_id', 'agent_speaker_id', 'data.agentName', 'data.agent', 'author', 'name']);
    const ts = toIso(pick(r, ['created_at', 'timestamp', 'started_at', 'time', 'data.timestamp']));
    const action = str(pick(r, ['data.actionType', 'action_type']));
    if (goal) {
      items.push({ kind: 'session', ts, end: pick(r, ['ended_at', 'finished_at', 'stopped_at']) ? toIso(pick(r, ['ended_at', 'finished_at', 'stopped_at'])) : undefined,
        agent: agentFor(agentRaw), goal: str(goal), sid, order: order++ });
      continue;
    }
    if (action && !/TALK/.test(action)) continue;
    const text = str(pick(r, ['content', 'message', 'text', 'data.message', 'data.content', 'data.text']));
    if (!text) continue;
    items.push({ kind: 'talk', ts, agent: agentFor(agentRaw), text, sid, order: order++ });
  }
  items.sort((a, b) => a.ts.localeCompare(b.ts) || a.order - b.order);
  const sliced = items.slice(0, MAX_EVENTS);

  const ensureAgentTask = (agent: string, ts: string, at: number) => {
    if (openTask.has(agent)) return openTask.get(agent)!;
    const taskId = `chat-${agent}`;
    openTask.set(agent, taskId);
    drafts.push({ ts, order: at - 0.5, build: (sequence, id) => ({
      id, sequence, timestamp: ts, agentId: agent, taskId: null, type: 'task_created', provenance: 'inferred',
      text: `Synthesised container for ${agents.get(agent)!.name}'s chat activity (no session goal in this file).`,
      payload: { tasks: [{ taskId, title: `${agents.get(agent)!.name} — chat activity`, owner: agent }] }, evidenceRefs: [],
    }) });
    return taskId;
  };

  for (const it of sliced) {
    if (it.kind === 'session') {
      const taskId = `session-${it.sid}`;
      openTask.set(it.agent, taskId);
      drafts.push({ ts: it.ts, order: it.order, build: (sequence, id) => ({
        id, sequence, timestamp: it.ts, agentId: it.agent, taskId: null, type: 'task_created', provenance: 'observed',
        text: `Computer-use session started: ${it.goal}`,
        payload: { tasks: [{ taskId, title: it.goal.slice(0, 80), owner: it.agent }] }, evidenceRefs: [],
        statusAfter: undefined,
      }) });
      drafts.push({ ts: it.ts, order: it.order + 0.1, build: (sequence, id) => ({
        id, sequence, timestamp: it.ts, agentId: it.agent, taskId, type: 'status_updated', provenance: 'observed',
        text: 'Session running.', payload: { status: 'in_progress' as TaskStatus }, evidenceRefs: [], statusAfter: 'in_progress',
      }) });
      if (it.end) {
        const end = it.end;
        drafts.push({ ts: end, order: it.order + 0.2, build: (sequence, id) => ({
          id, sequence, timestamp: end, agentId: it.agent, taskId, type: 'status_updated', provenance: 'observed',
          text: 'Session ended (end of session ≠ goal achieved).', payload: { status: 'done' as TaskStatus }, evidenceRefs: [], statusAfter: 'done',
        }) });
      }
      continue;
    }
    const taskId = openTask.get(it.agent) ?? ensureAgentTask(it.agent, it.ts, it.order);
    const correctionOf = CORRECTION.test(it.text) ? lastClaim.get(it.agent) : undefined;
    const isClaim = !correctionOf && COMPLETION.test(it.text);
    const claimId = `C-${it.sid}`;
    if (isClaim) lastClaim.set(it.agent, claimId);
    drafts.push({ ts: it.ts, order: it.order, build: (sequence, id): RecallEvent => {
      const base = { id, sequence, timestamp: it.ts, agentId: it.agent, taskId, text: it.text, evidenceRefs: [] as string[] };
      if (correctionOf) return { ...base, type: 'correction', provenance: 'inferred', payload: { supersedes: correctionOf } };
      if (isClaim) return { ...base, type: 'claim', provenance: 'inferred', payload: { claimId, asserts: 'complete' } };
      return { ...base, type: 'message', provenance: 'observed', payload: {} };
    } });
  }

  drafts.sort((a, b) => a.ts.localeCompare(b.ts) || a.order - b.order);
  const events = drafts.map((d, i) => d.build(i + 1, `av-${i + 1}`));

  return {
    id: `ai-village:${fileName}`,
    label: `AI Village import · ${fileName}`,
    kind: 'ai-village',
    description:
      `Real AI Village records adapted from ${fileName}` +
      (items.length > MAX_EVENTS ? ` (first ${MAX_EVENTS} of ${items.length} items).` : '.') +
      ' Claims and corrections are keyword-inferred from free text and carry no artifact version, so they show as "unknown" evidence and cannot trigger monitors.',
    agents: [...agents.values()],
    events,
  };
}
