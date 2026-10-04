// B · Superseded claim reused.

import type { EventOf, RecallEvent } from '../../model/types';
import { dependencyReach, type WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { unavailableWhy } from './text';

function run(ws: WorldState): Finding[] {
  const name = ws.agentName;
  const out: Finding[] = [];
  const corrections = ws.visible.filter((e): e is EventOf<'correction'> => e.type === 'correction');
  for (const act of ws.visible) {
    if (act.type !== 'action' || !act.taskId) continue;
    for (const claimId of act.payload.referencesClaims) {
      const corr = corrections.find((c) => c.payload.supersedes === claimId && c.sequence < act.sequence);
      if (!corr) continue;

      // The finding needs the original claim record. If it is withheld or missing, say so:
      // the action and correction alone cannot establish what was reused.
      const claimEventId = ws.claims.get(claimId)?.eventId
        ?? [...act.evidenceRefs, ...corr.evidenceRefs].find((r) => ws.refStatus(r) === 'withheld' || ws.refStatus(r) === 'missing');
      const claimGone = !!claimEventId && ws.refStatus(claimEventId) !== 'available';

      const ackedBefore = ws.visible.find(
        (e) => e.type === 'acknowledgement' && e.agentId === act.agentId &&
          e.payload.acknowledges === corr.id && e.sequence < act.sequence,
      );
      const otherAcks = ws.visible.filter(
        (e) => e.type === 'acknowledgement' && e.payload.acknowledges === corr.id &&
          e.agentId !== act.agentId && e.sequence < act.sequence,
      );
      const resolver: RecallEvent | undefined = claimGone ? undefined : ws.visible.find(
        (e) => e.sequence > act.sequence && e.agentId === act.agentId &&
          ((e.type === 'acknowledgement' && e.payload.acknowledges === corr.id) ||
           (e.type === 'action' && e.taskId === act.taskId &&
            !e.payload.referencesClaims.some((c) => corrections.some((k) => k.payload.supersedes === c)))),
      );
      const addressed = corr.payload.addressedTo?.includes(act.agentId);
      const receiptGap = ackedBefore ? [] : [
        `No delivery or read receipt for ${corr.id} to ${name(act.agentId)}` +
          (addressed ? ' (the correction @-mentions them, but mention ≠ receipt).' : '.'),
      ];

      out.push({
        id: `B:${act.id}:${claimId}`,
        monitor: 'B',
        title: claimGone ? `Cannot establish reuse of withdrawn claim ${claimId}` : `${name(act.agentId)} acted on withdrawn claim ${claimId}`,
        summary: claimGone
          ? `${name(act.agentId)}'s action cites ${claimId}, which ${name(corr.agentId)} withdrew at #${corr.sequence}, but the claim record itself is unavailable to this analysis.`
          : `${name(act.agentId)} performed "${act.payload.action}" citing ${claimId}, which ${name(corr.agentId)} withdrew at #${corr.sequence}; ` +
            (ackedBefore ? `${name(act.agentId)} had acknowledged that correction` : `no acknowledgement from ${name(act.agentId)} was observed before acting`) +
            (resolver ? ` (recovered at #${resolver.sequence}).` : '.'),
        explanation:
          `${name(act.agentId)} performed "${act.payload.action}" and explicitly cited ${claimId}. ` +
          `${name(corr.agentId)} had already withdrawn ${claimId} at #${corr.sequence}. ` +
          (claimGone
            ? `The original claim record (${claimEventId}) is unavailable, so RECALL cannot confirm what was reused.`
            : ackedBefore
              ? `${name(act.agentId)} had acknowledged that correction at #${ackedBefore.sequence}, yet still cited the old claim.`
              : `No acknowledgement from ${name(act.agentId)} is recorded before this action, so RECALL does not assume they saw the correction.`) +
          (otherAcks.length ? ` Meanwhile ${otherAcks.map((a) => name(a.agentId)).join(', ')} acknowledged it and paused.` : ''),
        detectedAt: act.sequence,
        detectedEventId: act.id,
        taskId: act.taskId,
        claimId,
        agentId: act.agentId,
        evidence: [
          ...(claimEventId ? [{ eventId: claimEventId, role: `Original claim ${claimId}` }] : []),
          { eventId: corr.id, role: `Correction superseding ${claimId}` },
          ...otherAcks.map((a) => ({ eventId: a.id, role: `Acknowledged by ${name(a.agentId)}` })),
          { eventId: act.id, role: 'Action citing the withdrawn claim' },
          ...(resolver ? [{ eventId: resolver.id, role: 'Recovery' }] : []),
        ],
        reach: dependencyReach(ws, act.taskId),
        missing: claimGone ? [`${claimEventId} — ${unavailableWhy(ws, claimEventId!)}.`, ...receiptGap] : receiptGap,
        state: claimGone ? 'insufficient' : resolver ? 'resolved' : 'active',
        resolution: resolver
          ? { eventId: resolver.id, text: `${name(resolver.agentId)} ${resolver.type === 'acknowledgement' ? 'acknowledged the correction' : 'acted on a current claim'} at #${resolver.sequence}.` }
          : undefined,
      });
    }
  }
  return out;
}

export const B: MonitorDef = {
  id: 'B',
  title: 'Superseded claim reused',
  family: 'propagation',
  needs: ['action', 'correction'],
  fixture: 'src/data/fixtures/B.json',
  rule:
    'action.referencesClaims ∋ C\n' +
    'AND ∃ correction k: k.supersedes = C ∧ k.seq < action.seq\n' +
    '→ ACTIVE · an acknowledgement only counts if recorded by the acting agent ("not observed" otherwise)\n' +
    '→ RESOLVED when that agent later acknowledges, or acts on a current claim\n' +
    '→ INSUFFICIENT if the original claim record is withheld or missing',
  run,
};
