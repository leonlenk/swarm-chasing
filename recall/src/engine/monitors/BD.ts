// BD · Ended on failure.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { endOf, finding, isFail, turnsIn, verdictsIn } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const e of ws.visible) {
    if (e.type !== 'task_created') continue;
    for (const t of e.payload.tasks) {
      const end = endOf(ws, t.taskId);
      if (!end) continue;
      const verdicts = verdictsIn(ws, t.taskId).filter((v) => v.sequence < end.sequence);
      const last = verdicts[verdicts.length - 1];
      if (!last || !isFail(last)) continue;
      if (turnsIn(ws, t.taskId).some((a) => a.sequence > last.sequence && a.sequence < end.sequence)) continue; // a further turn followed
      const reason = end.type === 'status_updated' ? end.payload.endReason : undefined;
      const reasonLabel = reason === 'stop' ? 'STOP' : reason === 'consolidate' ? 'CONSOLIDATE' : reason === 'next-start' ? 'next START' : 'session end';
      out.push(finding({
        attributes: [`ended by: ${reasonLabel}`],
        id: `BD:${t.taskId}`, monitor: 'BD', taskId: t.taskId, agentId: t.owner,
        title: `Ended on failure: ${last.payload.rule ?? last.payload.tool}`,
        summary: `${ws.agentName(t.owner)}'s session stopped right after a failing verdict (${last.payload.runId}), with no further turn.`,
        explanation: `The last verdict in session ${t.taskId} is a fail (${last.id}, #${last.sequence}); the session ended by ${reasonLabel} at #${end.sequence} with no turn in between.`,
        detectedAt: end.sequence, detectedEventId: end.id,
        evidence: [{ eventId: last.id, role: 'Last verdict (fail)' }, { eventId: end.id, role: 'Session end' }],
        state: 'active',
      }, ws));
    }
  }
  return out;
}

export const BD: MonitorDef = {
  id: 'BD', title: 'Ended on failure', family: 'session', needs: ['task_created', 'tool_result'], reads: ['status_updated', 'action'], fixture: 'src/data/fixtures/BD.json',
  ruling: 'A session is ended by STOP, CONSOLIDATE, or the agent\'s next START; the end reason is recorded on the finding.',
  rule: "Active when: Session's last verdict is fail and the session STOPs with no further turn\n" +
    'Insufficient when: Last turn withheld',
  run,
};
