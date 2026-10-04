// U · Repeated goal, repeated failure.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, isFail, isPass, verdictsIn } from './util';

const MIN_SESSIONS = 3;

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  const groups = new Map<string, { taskId: string; created: string; seq: number }[]>();
  for (const e of ws.visible) {
    if (e.type !== 'task_created') continue;
    for (const t of e.payload.tasks) {
      if (!t.goalKey) continue;
      const k = `${t.owner}|${t.goalKey}`;
      groups.set(k, [...(groups.get(k) ?? []), { taskId: t.taskId, created: e.id, seq: e.sequence }]);
    }
  }
  for (const [k, sessions] of groups) {
    const [owner, goalKey] = k.split('|');
    const failing = sessions.map((s) => ({ s, v: verdictsIn(ws, s.taskId) }))
      .filter(({ v }) => v.some(isFail) && !v.some(isPass))
      .map(({ s, v }) => ({ s, fail: v.find(isFail)! }));
    if (failing.length < MIN_SESSIONS) continue;
    const third = failing[MIN_SESSIONS - 1];
    const lastFailSeq = failing[failing.length - 1].s.seq;
    const passer = sessions.filter((s) => s.seq > lastFailSeq).map((s) => verdictsIn(ws, s.taskId).find(isPass)).find(Boolean);
    out.push(finding({
      id: `U:${owner}:${goalKey}`, monitor: 'U', taskId: third.s.taskId, agentId: owner,
      title: `Repeated goal, repeated failure ×${failing.length}`,
      summary: `${ws.agentName(owner)} ran ${failing.length} sessions with the goal "${goalKey}", each with a failing verdict and no pass` + (passer ? `; a later session passed at #${passer.sequence}.` : '.'),
      explanation: `${failing.length} sessions by ${ws.agentName(owner)} share the normalized short goal "${goalKey}"; each records a fail verdict and no pass verdict.`,
      detectedAt: Math.max(third.s.seq, third.fail.sequence), detectedEventId: third.fail.id,
      evidence: [...failing.flatMap(({ s, fail }, n) => [{ eventId: s.created, role: `Session ${n + 1}` }, { eventId: fail.id, role: `Fail verdict in session ${n + 1}` }]),
        ...(passer ? [{ eventId: passer.id, role: 'Later session passes' }] : [])],
      state: passer ? 'resolved' : 'active',
      resolution: passer ? { eventId: passer.id, text: `A later session with the same goal passed at #${passer.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const U: MonitorDef = {
  id: 'U', title: 'Repeated goal, repeated failure', family: 'session', needs: ['task_created', 'tool_result'], reads: ['status_updated'], fixture: 'src/data/fixtures/U.json',
  rule: 'Active when: >= 3 sessions by one agent with identical short goal, each with a fail verdict and no pass\n' +
    'Resolves when: A later session with the same goal passes',
  run,
};
