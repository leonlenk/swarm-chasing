// G · Stale after failure.
import type { EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, isFail, isPass, keyOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    const key = keyOf(c);
    const checks = checksOf(ws, key).filter((k) => isPass(k) || isFail(k));
    const before = checks.filter((k) => k.sequence < c.sequence);
    const basis = before[before.length - 1];
    if (!basis || !isPass(basis)) continue; // not supported at its time
    const laterFail = checks.find((k) => k.sequence > c.sequence && isFail(k));
    if (!laterFail) continue;
    const correction = ws.visible.find((e): e is EventOf<'correction'> =>
      e.type === 'correction' && e.agentId === c.agentId && e.payload.supersedes === c.payload.claimId && e.sequence > c.sequence);
    const laterPass = checks.find((k) => k.sequence > laterFail.sequence && isPass(k));
    const resolver = [correction, laterPass].filter(Boolean).sort((a, b) => a!.sequence - b!.sequence)[0];
    const who = ws.agentName(c.agentId);
    out.push(finding({
      id: `G:${c.id}`, monitor: 'G', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Stale after failure: ${subjectLabel(key)}`,
      summary: `${who}'s claim about ${subjectLabel(key)} was backed by ${basis.payload.runId} at the time, but ${ws.agentName(laterFail.agentId)}'s later check ${laterFail.payload.runId} failed` +
        (resolver ? ` (resolved at #${resolver.sequence}).` : ` and ${who} has not corrected it; no later pass.`),
      explanation: `At #${c.sequence} the latest verification of ${subjectLabel(key)} (${basis.id}) passed, so the claim was supported. ` +
        `At #${laterFail.sequence} a verification failed (${laterFail.id}). ` +
        (resolver ? `${resolver.type === 'correction' ? 'The claimant corrected the claim' : 'A later check passed'} at #${resolver.sequence}.` : 'No correction by the claimant and no later pass are recorded.'),
      detectedAt: laterFail.sequence, detectedEventId: laterFail.id,
      evidence: [
        { eventId: basis.id, role: 'Passing check at the time of the claim' },
        { eventId: c.id, role: `Claim ${c.payload.claimId}` },
        { eventId: laterFail.id, role: 'Later failing check' },
        ...(resolver ? [{ eventId: resolver.id, role: resolver.type === 'correction' ? 'Correction by the claimant' : 'Later passing check' }] : []),
      ],
      missing: resolver ? [] : [`No correction by ${who} and no passing check of ${subjectLabel(key)} after #${laterFail.sequence}.`],
      state: resolver ? 'resolved' : 'active',
      resolution: resolver ? { eventId: resolver.id, text: `${resolver.type === 'correction' ? 'Claim corrected' : 'Passing check'} at #${resolver.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const G: MonitorDef = {
  id: 'G', title: 'Stale after failure', family: 'claim-evidence', needs: ['claim', 'tool_result', 'subject'], fixture: 'src/data/fixtures/G.json',
  rule: 'Active when: Claim S supported at its time; later a failing check of S; no correction by the claimant and no later pass\n' +
    'Resolves when: Correction or later pass\n' +
    'Insufficient when: Later check withheld',
  run,
};
