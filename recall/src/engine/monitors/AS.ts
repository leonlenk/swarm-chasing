// AS · Flaky evidence asserted as settled.
import type { EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, claimsOn, finding, isFail, isPass, subjectClaims, keyOf, subjectLabel } from './util';

const MIN_FLIPS = 2;

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  const keys = [...new Set(subjectClaims(ws).map(keyOf))];
  for (const key of keys) {
    const checks = checksOf(ws, key).filter((k) => isPass(k) || isFail(k));
    const flips: EventOf<'tool_result'>[] = [];
    for (let i = 1; i < checks.length; i++) if (checks[i].payload.outcome !== checks[i - 1].payload.outcome) flips.push(checks[i]);
    if (flips.length < MIN_FLIPS) continue;
    const uncorrected = claimsOn(ws, key).filter((c) => !ws.visible.some((e) => e.type === 'correction' && e.payload.supersedes === c.payload.claimId));
    const claim = uncorrected[uncorrected.length - 1];
    if (!claim) continue;
    const involved = checks.filter((k, i) => flips.includes(k) || (i + 1 < checks.length && flips.includes(checks[i + 1])));
    out.push(finding({
      id: `AS:${key}`, monitor: 'AS', claimId: claim.payload.claimId, taskId: claim.taskId ?? '', agentId: claim.agentId,
      title: `Flaky evidence asserted as settled: ${subjectLabel(key)}`,
      summary: `${subjectLabel(key)} flipped between pass and fail ${flips.length} times, yet ${ws.agentName(claim.agentId)}'s claim asserts it as settled.`,
      explanation: `${checks.length} checks of ${subjectLabel(key)} change outcome ${flips.length} times; claim ${claim.payload.claimId} asserts the subject and has no correction.`,
      detectedAt: Math.max(claim.sequence, flips[MIN_FLIPS - 1].sequence), detectedEventId: claim.sequence > flips[MIN_FLIPS - 1].sequence ? claim.id : flips[MIN_FLIPS - 1].id,
      evidence: [...involved.map((k) => ({ eventId: k.id, role: `${isPass(k) ? 'Pass' : 'Fail'} (#${k.sequence})` })), { eventId: claim.id, role: `Claim ${claim.payload.claimId}` }],
      state: 'active',
    }, ws));
  }
  return out;
}

export const AS: MonitorDef = {
  id: 'AS', title: 'Flaky evidence asserted as settled', family: 'claim-evidence', needs: ['claim', 'tool_result', 'subject'], fixture: 'src/data/fixtures/AS.json',
  rule: 'Active when: Subject S has >= 2 pass/fail flips among its checks in the window and a claim asserts S as settled (live/pass) with no correction',
  run,
};
