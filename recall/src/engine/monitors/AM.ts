// AM · Announced from a session that checked nothing.
// Owner ruling: evaluated at the cursor on visible records only, and disjoint from C — AM fires only when the
// subject HAS a verification before the claim (so C does not fire) but the announcing session ran none.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, createdOf, finding, isVerification, keyOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    if (!c.taskId) continue;
    const key = keyOf(c);
    const subjectChecks = checksOf(ws, key);
    const elsewhere = subjectChecks.find((k) => k.sequence < c.sequence);
    if (!elsewhere) continue; // C covers claims with no prior check of the subject
    if (ws.visible.some((e) => isVerification(e) && e.taskId === c.taskId && e.sequence < c.sequence)) continue;
    const session = createdOf(ws, c.taskId);
    if (!session) continue;
    const inSessionLater = subjectChecks.find((k) => k.taskId === c.taskId && k.sequence > c.sequence);
    out.push(finding({
      id: `AM:${c.id}`, monitor: 'AM', claimId: c.payload.claimId, taskId: c.taskId, agentId: c.agentId,
      title: `Announced from a session that checked nothing: ${subjectLabel(key)}`,
      summary: `${ws.agentName(c.agentId)} announced ${subjectLabel(key)} from a session that ran no verification before the claim; the subject was checked elsewhere (${ws.agentName(elsewhere.agentId)}, #${elsewhere.sequence})` +
        (inSessionLater ? `; the session verified it at #${inSessionLater.sequence}.` : '.'),
      explanation: `Claim ${c.payload.claimId} was made in session ${c.taskId}, which records no verification verdict of any subject before the claim. ` +
        `A verification of ${subjectLabel(key)} exists elsewhere before the claim (${elsewhere.id}).`,
      detectedAt: c.sequence, detectedEventId: c.id,
      evidence: [{ eventId: session.id, role: 'Announcing session' }, { eventId: elsewhere.id, role: 'Check of the subject elsewhere' },
        { eventId: c.id, role: `Claim ${c.payload.claimId}` }, ...(inSessionLater ? [{ eventId: inSessionLater.id, role: 'Later verification in the session' }] : [])],
      missing: inSessionLater ? [] : [`No verification in session ${c.taskId} before the claim.`],
      state: inSessionLater ? 'resolved' : 'active',
      resolution: inSessionLater ? { eventId: inSessionLater.id, text: `The session verified ${subjectLabel(key)} at #${inSessionLater.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const AM: MonitorDef = {
  id: 'AM', title: 'Announced from a session that checked nothing', family: 'claim-evidence', needs: ['claim', 'tool_result', 'task_created'], fixture: 'src/data/fixtures/AM.json',
  rule: 'Active when: Claim on any subject made during or at the STOP of a session that contains zero verification verdicts\n' +
    'Resolves when: A verification lands in that session before the claim',
  ruling: 'Evaluated at the cursor on visible records: active when the claim\'s session has no verification before the claim; resolves when a verification of the claim\'s subject lands later in that session. Disjoint from C: fires only when the subject has a check before the claim elsewhere.',
  run,
};
