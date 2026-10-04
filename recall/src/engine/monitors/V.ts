// V · Concurrent duplicate goal.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { endOf, finding } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  const sessions = ws.visible.flatMap((e) => (e.type === 'task_created' ? e.payload.tasks.map((t) => ({ spec: t, created: e })) : []))
    .filter((s) => s.spec.goalKey)
    .map((s) => ({ ...s, end: endOf(ws, s.spec.taskId) }))
    .map((s) => ({ ...s, lo: s.created.sequence, hi: s.end?.sequence ?? Number.POSITIVE_INFINITY }));
  for (let a = 0; a < sessions.length; a++) {
    for (let b = a + 1; b < sessions.length; b++) {
      const x = sessions[a]; const y = sessions[b];
      if (x.spec.owner === y.spec.owner || x.spec.goalKey !== y.spec.goalKey) continue;
      if (!(x.lo <= y.hi && y.lo <= x.hi)) continue; // overlap in sequence order
      const later = x.lo >= y.lo ? x : y;
      out.push(finding({
        id: `V:${x.spec.taskId}:${y.spec.taskId}`, monitor: 'V', taskId: later.spec.taskId, agentId: later.spec.owner,
        title: `Concurrent duplicate goal: “${x.spec.title}”`,
        summary: `${ws.agentName(x.spec.owner)} and ${ws.agentName(y.spec.owner)} ran sessions with the same goal at the same time.`,
        explanation: `Sessions ${x.spec.taskId} and ${y.spec.taskId} have the identical normalized short goal "${x.spec.goalKey}" and overlap in sequence order.`,
        detectedAt: later.lo, detectedEventId: later.created.id,
        evidence: [{ eventId: x.created.id, role: `Session by ${ws.agentName(x.spec.owner)}` }, { eventId: y.created.id, role: `Session by ${ws.agentName(y.spec.owner)}` },
          ...[x.end, y.end].filter((e): e is NonNullable<typeof e> => !!e && e.sequence <= ws.cursor).map((e) => ({ eventId: e.id, role: 'Session end' }))],
        state: 'active',
      }, ws));
    }
  }
  return out;
}

export const V: MonitorDef = {
  id: 'V', title: 'Concurrent duplicate goal', family: 'session', needs: ['task_created'], reads: ['status_updated'], fixture: 'src/data/fixtures/V.json',
  rule: "Active when: Two agents' sessions with identical normalized short goal overlap in time",
  run,
};
