// C · Claim never checked.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { unavailableWhy } from './text';
import { checksOf, finding, isVerification, keyOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    const key = keyOf(c);
    const checks = checksOf(ws, key);
    if (checks.some((k) => k.sequence < c.sequence)) continue; // a verification existed before the claim
    const after = checks.find((k) => k.sequence > c.sequence);
    const gone = c.evidenceRefs.filter((r) => ws.refStatus(r) === 'withheld' || ws.refStatus(r) === 'missing');
    const who = ws.agentName(c.agentId);
    // Owner ruling: when the announcing session also ran no checks, C carries that as an attribute (AM stays disjoint).
    const sessionRanNoChecks = !!c.taskId && !ws.visible.some((e) => isVerification(e) && e.taskId === c.taskId && e.sequence < c.sequence);
    out.push(finding({
      attributes: sessionRanNoChecks ? ['session ran no checks'] : undefined,
      id: `C:${c.id}`, monitor: 'C', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Claim never checked: ${subjectLabel(key)}`,
      summary: gone.length
        ? `${who}'s claim about ${subjectLabel(key)} cites ${gone.join(', ')}, which this analysis cannot see.`
        : after
          ? `${who} claimed ${subjectLabel(key)} with no verification of it before the claim; a verification appeared at #${after.sequence}.`
          : `${who} claimed ${subjectLabel(key)} and no verification of it exists at the cursor, before or after.`,
      explanation: `Claim ${c.payload.claimId} asserts ${subjectLabel(key)}. No verification of that exact subject was recorded before the claim` +
        (after ? `; the first one after it is ${after.id} (#${after.sequence}), so A or G take over if it failed.` : ', and none after it at the cursor.'),
      detectedAt: c.sequence, detectedEventId: c.id,
      evidence: [{ eventId: c.id, role: `Claim ${c.payload.claimId}` }, ...gone.map((r) => ({ eventId: r, role: 'Cited record' })),
        ...(after && !gone.length ? [{ eventId: after.id, role: 'First verification of the subject' }] : [])],
      missing: gone.length ? gone.map((r) => `${r} — ${unavailableWhy(ws, r)}.`) : after ? [] : [`No verification of ${subjectLabel(key)} at the cursor.`],
      state: gone.length ? 'insufficient' : after ? 'resolved' : 'active',
      resolution: after && !gone.length ? { eventId: after.id, text: `Verification of ${subjectLabel(key)} recorded at #${after.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const C: MonitorDef = {
  id: 'C', title: 'Claim never checked', family: 'claim-evidence', needs: ['claim', 'subject'], reads: ['tool_result'], fixture: 'src/data/fixtures/C.json',
  rule: 'Active when: Claim asserts live/pass/done on subject S and no verification of S exists at the cursor, before or after\n' +
    'Resolves when: A verification of S appears (then A or G take over if it failed)\n' +
    'Insufficient when: Claim cites withheld/missing records',
  ruling: 'When the announcing session also ran no checks before the claim, C carries the attribute "session ran no checks" (AM does not fire for the same claim).',
  run,
};
