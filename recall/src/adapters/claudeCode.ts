// Claude Code sessions recorded by the swarm-live hooks (anand/live-plugin) → RECALL DataSource.
//
// Input: one session document from `swarm-mcp render recall` (public/data/scope/live/<session>.json): the
// claude_code adapter's rows for a main agent and its subagents, with SwarmScope evidence ids.
// The session is mapped onto the AI Village window shape and run through adaptAiVillageWindow, so every verdict,
// claim, correction and directive rule — and therefore every monitor — applies unchanged:
//
//   Claude Code                                RECALL (via the AI Village window adapter)            provenance
//   main agent / subagent                      agent (role: main agent · subagent type)              observed
//   each agent's run (period)                  task: in progress → ended at its last record          observed
//   Bash call: command + output (or error)     tool_result when a verdict rule matches               observed · rule
//   every tool call                            action (command, or "Tool · target"), writes/destructive observed
//   user prompt                                human message (or failure report, rule human-negative) observed
//   Agent/Task delegation                      "@subagent <prompt>": directive to the subagent       inferred · directive
//   agent text (final / transcript)            message, claim, correction or quote by the same rules inferred · rule
//   subagent spawned by a run                  dependency: subagent's task → its parent's task       observed
//
// Every event carries `storeId`, the record's SwarmScope evidence id (citable with findings_record).
import type { Agent, DataSource, RecallEvent } from '../model/types';
import type { LiveSession } from '../model/scope';
import { adaptAiVillageWindow, destructiveOf, refsIn, writesOf, type HfAgent, type HfChat, type HfEvent, type HfSession, type HfTurn, type HfTurnLite } from './aiVillageHf';

/** Stable short hex id (FNV-1a, 64 bits as two 32-bit halves) so generated ids and claim ids stay unique and readable. */
export function shortHash(s: string): string {
  let a = 0x811c9dc5, b = 0x01000193 ^ 0x5bd1e995;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    a = Math.imul(a ^ c, 0x01000193) >>> 0;
    b = Math.imul(b ^ c, 0x5bd1e995) >>> 0;
  }
  return (a.toString(16).padStart(8, '0') + b.toString(16).padStart(8, '0'));
}

const FILE_TOOLS = new Set(['Write', 'Edit', 'MultiEdit', 'NotebookEdit']);
const plusMs = (iso: string, ms: number) => new Date(Date.parse(iso) + ms).toISOString();
const clip = (s: string, n: number) => (s.length <= n ? s : `${s.slice(0, n - 1)}…`);

/** The window adapter words run boundaries for AI Village computer-use sessions; say what they are here. */
function runText(e: RecallEvent): string | null {
  if (e.type === 'task_created') return e.text.replace(/^Computer-use session started(?: \(before this window\))?\. Goal:/, 'Agent run started. Task:');
  if (e.type === 'status_updated' && e.payload.status === 'in_progress') return 'Run in progress.';
  if (e.type === 'status_updated' && e.payload.status === 'ended') return 'Run ended (the agent stopped after its last recorded step). Ending a run does not mean its task was achieved.';
  return null;
}

export function adaptClaudeCodeSession(doc: LiveSession): DataSource {
  const h = shortHash;
  const store = new Map<string, string>(); // generated RECALL id → SwarmScope evidence id
  const agentById = new Map(doc.agents.map((a) => [a.id, a]));
  const times = [...doc.messages.map((m) => m.t), ...doc.actions.map((x) => x.t), ...doc.periods.flatMap((p) => [p.start, p.end])]
    .filter((t): t is string => !!t).sort();
  const first = times[0] ?? doc.start ?? new Date(0).toISOString();
  const last = times[times.length - 1] ?? doc.updated ?? first;
  const window = { from: plusMs(first, -1000), to: plusMs(last, 1000) };

  const agents: HfAgent[] = doc.agents.map((a) => ({ id: a.id, name: a.name, model_string: null }));

  // Runs → sessions. A finished run ends just after its agent's last record, so its final words stay attached to it.
  const lastOf = new Map<string, string>();
  for (const t of [...doc.messages.map((m) => ({ who: m.author, t: m.t })), ...doc.actions.map((x) => ({ who: x.agent, t: x.end ?? x.t }))]) {
    if (t.t && (!lastOf.has(t.who) || t.t > lastOf.get(t.who)!)) lastOf.set(t.who, t.t);
  }
  const runs = doc.periods.filter((p) => p.start);
  const sessions: HfSession[] = runs.map((p) => {
    store.set(`session/${h(p.id)}`, p.id);
    return { id: h(p.id), agent_id: p.agent, session_goal: p.label, short_displayed_session_goal: clip(p.label, 70), created_at: p.start! };
  });
  const boundaries: HfEvent[] = [];
  runs.forEach((p, i) => {
    const a = agentById.get(p.agent);
    const running = (p.status ?? a?.status) === 'running';
    if (running) return;
    const end = [p.end, lastOf.get(p.agent)].filter((t): t is string => !!t).sort().pop();
    if (!end) return;
    boundaries.push({ id: `stop-${h(p.id)}`, event_index: i + 1, created_at: plusMs(end, 1),
      data: { actionType: 'STOP_USING_COMPUTER', agentId: p.agent, computerUseSessionId: h(p.id) } });
  });
  const runOf = new Map(runs.map((p) => [p.agent, h(p.id)]));

  // Messages → chat. Delegations become an @mention of the subagent (rule 'directive' then applies).
  const chats: HfChat[] = doc.messages.filter((m) => m.t && m.text.trim()).map((m) => {
    const gen = h(m.id);
    store.set(`chat/${gen}`, m.id);
    const human = !agentById.has(m.author);
    let content = m.text;
    if (m.type === 'delegation') {
      const sub = m.to.map((id) => agentById.get(id)).find(Boolean);
      if (sub) content = `@${sub.name} ${m.text}`;
    }
    return { id: gen, speaker_type: human ? 'human' : 'agent', agent_speaker_id: human ? null : m.author, content, room_id: doc.folder ?? 'session', created_at: m.t! };
  });

  // Tool calls → turns (Bash with a command: verdict rules) and actions (every call).
  const turns: HfTurn[] = [];
  const actions: HfTurnLite[] = [];
  for (const x of doc.actions) {
    const session = x.run ? h(x.run) : runOf.get(x.agent);
    if (!x.t || !session) continue;
    const gen = h(x.id);
    store.set(`turn/${gen}`, x.id);
    store.set(`act/${gen}`, x.id);
    const failed = x.status === 'error';
    const cmd = x.tool === 'Bash' ? x.command ?? '' : '';
    if (cmd) turns.push({ id: gen, session_id: session, created_at: x.t, agent_action: { command: cmd }, output: failed ? '' : x.output, error: failed ? x.output : null });
    const detail = x.text.replace(new RegExp(`^${x.tool}:?\\s*`), '');
    const filePath = FILE_TOOLS.has(x.tool) ? detail.trim() : '';
    const writes = cmd ? writesOf(cmd) : filePath ? [`file:${filePath}`] : [];
    const refs = refsIn(`${cmd || x.text}\n${x.output}`);
    actions.push({
      id: gen, session_id: session, created_at: x.t,
      kind: cmd ? 'command' : 'computer',
      ...(cmd ? { command: clip(cmd.split('\n')[0], 300), commandHash: h(cmd) } : { computerAction: clip(`${x.tool} · ${detail || x.tool}${failed ? ' (failed)' : ''}`, 160) }),
      outputHash: h(x.output), emptyOutput: !x.output.trim(),
      ...(writes.length ? { writes } : {}), ...(cmd && destructiveOf(cmd) ? { destructive: destructiveOf(cmd) } : {}),
      ...(refs.length ? { refs } : {}),
    });
  }

  const base = adaptAiVillageWindow({
    id: `live-${doc.session}`, label: doc.label, goal: doc.label, window, agents, sessions, boundaries, chats, actions, turns,
    generatedAt: doc.updated ?? last, maxEvents: 4000,
  });

  // Delegation structure: a subagent's task is a prerequisite of the run that spawned it.
  const taskOfRun = new Map<string, string>();
  for (const e of base.events) if (e.type === 'task_created') taskOfRun.set(store.get(e.id) ?? '', e.payload.tasks[0].taskId);
  const deps = new Map<string, RecallEvent>(); // after the child's task_created event id
  for (const p of runs) {
    if (!p.parent) continue;
    const child = taskOfRun.get(p.id), parent = taskOfRun.get(p.parent);
    if (!child || !parent) continue;
    const created = base.events.find((e) => store.get(e.id) === p.id && e.type === 'task_created')!;
    deps.set(created.id, {
      id: `dep/${h(p.id)}`, timestamp: created.timestamp, sequence: 0, agentId: agentById.get(p.agent)?.parent ?? p.agent, taskId: null,
      type: 'dependency_created', text: `Delegated: ${agentById.get(p.agent)?.name ?? 'subagent'} works on “${clip(p.label, 80)}” for its parent run.`,
      payload: { from: child, to: parent, reason: 'spawned by an Agent/Task call' }, evidenceRefs: [], provenance: 'observed', storeId: p.id,
    } as RecallEvent);
  }
  const events: RecallEvent[] = [];
  for (const e of base.events) {
    const sid = store.get(e.id.replace(/#.*$/, ''));
    const text = runText(e);
    events.push({ ...e, ...(sid ? { storeId: sid } : {}), ...(text ? { text } : {}) } as RecallEvent);
    const dep = deps.get(e.id);
    if (dep) events.push(dep);
  }
  events.forEach((e, i) => { e.sequence = i + 1; });

  const roles: Agent[] = base.agents.map((a) => {
    const src = agentById.get(a.id);
    if (!src) return { ...a, role: 'The person running Claude Code' };
    return { ...a, role: src.kind === 'main' ? `Main agent${doc.folder ? ` · ${doc.folder}` : ''}` : `Subagent · ${src.agentType ?? 'general'}` };
  }).map((a) => (a.id === 'humans' ? { ...a, name: 'User', role: 'The person running Claude Code' } : a));

  const subs = doc.agents.filter((a) => a.kind === 'subagent').length;
  return {
    ...base,
    id: `live:${doc.session}`,
    label: doc.label,
    kind: 'claude-code',
    description: `${doc.synthetic ? 'Synthetic demo recording' : 'Claude Code session recorded by the swarm-live hooks'}${doc.folder ? ` in ${doc.folder}` : ''}: ` +
      `the main agent and ${subs} subagent${subs === 1 ? '' : 's'}, ${doc.actions.length} tool calls. Claims and corrections are pattern-inferred from agent text; tool verdicts are rule-based.`,
    agents: roles,
    events,
    meta: {
      ...base.meta!,
      origin: 'live',
      dataset: doc.synthetic ? 'swarm-live demo recording (synthetic)' : 'swarm-live recordings (Claude Code hooks)',
      citation: `SwarmScope source "${doc.source}", session ${doc.session}`,
      live: { session: doc.session, status: doc.status, updated: doc.updated, synthetic: doc.synthetic, folder: doc.folder },
      notes: [
        'Tasks are agent runs: the main session and each subagent run. A subagent task is a prerequisite of the run that spawned it.',
        'Delegations are shown as directives to the subagent (rule directive): the Agent call’s prompt, addressed to it.',
        'Tool verdicts come only from Bash commands whose output matches a deterministic rule (http-status, test-summary, git-push, …). Other tools are actions.',
        'A failed call’s output is the error Claude Code reported (PostToolUseFailure); a command that ran but printed failures counts by its output.',
        ...doc.notes.map((n) => `Recorder: ${n}`),
      ],
    },
  };
}
