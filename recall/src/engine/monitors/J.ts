// J · Split evidence.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, isFail, isPass, keyOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    const key = keyOf(c);
    const checks = checksOf(ws, key);
    const pass = [...checks].reverse().find((k) => isPass(k) && k.agentId === c.agentId);
    const fail = [...checks].reverse().find((k) => isFail(k) && k.agentId !== c.agentId);
    if (!pass || !fail) continue;
    // Owner ruling: a failure older than the claimant's pass is superseded by it; J needs the failure to be the newer record.
    if (fail.sequence < pass.sequence) continue;
    const pivot = Math.max(pass.sequence, fail.sequence);
    const later = checks.find((k) => k.sequence > pivot && (k.agentId === pass.agentId || k.agentId === fail.agentId));
    const who = ws.agentName(c.agentId);
    out.push(finding({
      id: `J:${c.id}`, monitor: 'J', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Split evidence: ${subjectLabel(key)}`,
      summary: `${who}'s own check of ${subjectLabel(key)} passed (${pass.payload.runId}) while ${ws.agentName(fail.agentId)}'s failed (${fail.payload.runId})` +
        (later ? `; a later check at #${later.sequence} settles it.` : '; no later check settles it.'),
      explanation: `Claim ${c.payload.claimId} on ${subjectLabel(key)} has a passing check by the claimant (${pass.id}, #${pass.sequence}) and a failing check by another agent (${fail.id}, #${fail.sequence}).` +
        (later ? ` A later check by either (${later.id}) exists.` : ' No later check of the subject by either agent is recorded.'),
      detectedAt: Math.max(pivot, c.sequence), detectedEventId: pivot >= c.sequence ? (pass.sequence > fail.sequence ? pass.id : fail.id) : c.id,
      evidence: [
        { eventId: c.id, role: `Claim ${c.payload.claimId}` },
        { eventId: pass.id, role: 'Passing check by the claimant' },
        { eventId: fail.id, role: 'Failing check by another agent' },
        ...(later ? [{ eventId: later.id, role: 'Later check of the subject' }] : []),
      ],
      missing: later ? [] : [`No check of ${subjectLabel(key)} after #${pivot} by either agent.`],
      state: later ? 'resolved' : 'active',
      resolution: later ? { eventId: later.id, text: `Later check of ${subjectLabel(key)} at #${later.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const J: MonitorDef = {
  id: 'J', title: 'Split evidence', family: 'claim-evidence', needs: ['claim', 'tool_result', 'subject'], fixture: 'src/data/fixtures/J.json',
  ruling: 'Fires only when the other agent\'s failing check is newer than the claimant\'s pass; an older failure is superseded by the pass.',
  rule: 'Active when: Claim S has a passing check by the claimant and a failing check by another agent, both before the cursor, no later check\n' +
    'Resolves when: A later check of S by either',
  run,
};
