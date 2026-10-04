// AO · Cited a stale pass.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, isFail, isPass, keyOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    const key = keyOf(c);
    const checks = checksOf(ws, key).filter((k) => isPass(k) || isFail(k));
    const before = checks.filter((k) => k.sequence < c.sequence);
    const fail = [...before].reverse().find(isFail);
    if (!fail) continue;
    if (before.some((k) => isPass(k) && k.sequence > fail.sequence)) continue; // a newer pass supports it
    const stalePasses = before.filter((k) => isPass(k) && k.sequence < fail.sequence);
    if (!stalePasses.length) continue;
    const cited = stalePasses.find((k) => c.evidenceRefs.includes(k.id));
    const stale = cited ?? stalePasses[stalePasses.length - 1];
    const laterPass = checks.find((k) => k.sequence > c.sequence && isPass(k));
    const who = ws.agentName(c.agentId);
    out.push(finding({
      id: `AO:${c.id}`, monitor: 'AO', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Cited a stale pass: ${subjectLabel(key)}`,
      summary: `${who} claimed ${subjectLabel(key)} after ${fail.payload.runId} failed, ${cited ? 'citing' : 'supported only by'} the older pass ${stale.payload.runId}` +
        (laterPass ? `; a later pass at #${laterPass.sequence} resolves it.` : '.'),
      explanation: `Before claim ${c.payload.claimId}, the subject passed (${stale.id}, #${stale.sequence}) and then failed (${fail.id}, #${fail.sequence}); no pass came after the failure before the claim.`,
      detectedAt: c.sequence, detectedEventId: c.id,
      evidence: [{ eventId: stale.id, role: cited ? 'Stale pass cited by the claim' : 'Stale pass (only support)' }, { eventId: fail.id, role: 'Later failing check' },
        { eventId: c.id, role: `Claim ${c.payload.claimId}` }, ...(laterPass ? [{ eventId: laterPass.id, role: 'Later passing check' }] : [])],
      missing: laterPass ? [] : [`No passing check of ${subjectLabel(key)} after #${fail.sequence}.`],
      state: laterPass ? 'resolved' : 'active',
      resolution: laterPass ? { eventId: laterPass.id, text: `Passing check at #${laterPass.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const AO: MonitorDef = {
  id: 'AO', title: 'Cited a stale pass', family: 'claim-evidence', needs: ['claim', 'tool_result', 'subject'], fixture: 'src/data/fixtures/AO.json',
  rule: 'Active when: Claim on S made after a failing check of S, citing (or supported only by) a passing check of S that precedes the failure\n' +
    'Resolves when: Later pass\n' +
    'Insufficient when: Failing check withheld',
  run,
};
