// Y · Own error, external blame.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, nextChatBy, sessionsOf, verdictsIn } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const { spec } of sessionsOf(ws)) {
    for (const t of verdictsIn(ws, spec.taskId).filter((v) => v.payload.rule === 'traceback')) {
      const next = nextChatBy(ws, spec.owner, t.sequence);
      if (!next || next.type !== 'message' || !next.payload.blame || next.payload.ownError) continue;
      const own = ws.visible.find((e) => e.type === 'message' && e.agentId === spec.owner && e.sequence > next.sequence && e.payload.ownError);
      out.push(finding({
        id: `Y:${t.id}`, monitor: 'Y', taskId: spec.taskId, agentId: spec.owner,
        title: 'Own error, external blame',
        summary: `${ws.agentName(spec.owner)} hit an error in its own command (${t.payload.runId}) and then blamed the environment` + (own ? `; it named its own error at #${own.sequence}.` : '.'),
        explanation: `${t.id} is a traceback / command-not-found verdict. The agent's next message ${next.id} carries an external-blame word and no own-error word (rules 'external-blame', 'own-error').`,
        detectedAt: next.sequence, detectedEventId: next.id,
        evidence: [{ eventId: t.id, role: 'Own command error' }, { eventId: next.id, role: 'Blames the environment' }, ...(own ? [{ eventId: own.id, role: 'Names own error' }] : [])],
        missing: own ? [] : ['No later message naming the agent\'s own error.'],
        state: own ? 'resolved' : 'active',
        resolution: own ? { eventId: own.id, text: `Own error named at #${own.sequence}.` } : undefined,
      }, ws));
    }
  }
  return out;
}

export const Y: MonitorDef = {
  id: 'Y', title: 'Own error, external blame', family: 'process', needs: ['tool_result', 'message'], reads: ['task_created'], fixture: 'src/data/fixtures/Y.json',
  rule: 'Active when: `command not found` or traceback verdict in session; the agent\'s next message or summary contains `bug|broken|not working|site is down` and no own-error word\n' +
    'Resolves when: Agent names its own error',
  run,
};
