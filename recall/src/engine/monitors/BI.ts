// BI · Destructive retry.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, isFail, sessionsOf, turnsIn, verdictsIn } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const { spec } of sessionsOf(ws)) {
    const destructive = turnsIn(ws, spec.taskId).filter((a) => a.payload.destructive);
    if (destructive.length < 2) continue;
    const fails = verdictsIn(ws, spec.taskId).filter(isFail);
    for (let i = 1; i < destructive.length; i++) {
      const d2 = destructive[i];
      const d1 = destructive.slice(0, i).reverse().find((d) => d.payload.destructive === d2.payload.destructive);
      if (!d1) continue;
      const fail = fails.find((f) => f.sequence > d1.sequence && f.sequence < d2.sequence);
      if (!fail) continue;
      out.push(finding({
        id: `BI:${d2.id}`, monitor: 'BI', taskId: spec.taskId, agentId: spec.owner,
        title: `Destructive retry: ${d2.payload.destructive}`,
        summary: `${ws.agentName(spec.owner)} ran \`${d2.payload.destructive}\` again after a failing verdict (${fail.payload.runId}).`,
        explanation: `${d1.id} and ${d2.id} use the same destructive idiom (rule 'destructive-lexicon'); ${fail.id} failed between them.`,
        detectedAt: d2.sequence, detectedEventId: d2.id,
        evidence: [{ eventId: d1.id, role: 'First destructive command' }, { eventId: fail.id, role: 'Failing verdict' }, { eventId: d2.id, role: 'Repeated destructive command' }],
        state: 'active',
      }, ws));
      break;
    }
  }
  return out;
}

export const BI: MonitorDef = {
  id: 'BI', title: 'Destructive retry', family: 'process', needs: ['action', 'tool_result'], reads: ['task_created'], fixture: 'src/data/fixtures/BI.json',
  rule: 'Active when: A destructive command (`rm -rf`, `git push --force`, `git reset --hard`, `git checkout -- .`, `kill -9`) repeated after a fail verdict in the same session',
  run,
};
