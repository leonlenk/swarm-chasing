// AW · Correction delay strip.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, isFail, subjectKey, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const k of ws.visible) {
    if (k.type !== 'correction') continue;
    const claim = ws.claims.get(k.payload.supersedes);
    if (!claim?.subject) continue;
    const key = subjectKey(claim.subject);
    const firstFail = checksOf(ws, key).find((c) => isFail(c) && c.sequence < k.sequence);
    if (!firstFail) continue;
    const between = ws.visible.filter((e) => e.sequence > firstFail.sequence && e.sequence < k.sequence).length;
    out.push(finding({
      id: `AW:${k.id}`, monitor: 'AW', claimId: claim.id, taskId: k.taskId ?? '', agentId: k.agentId,
      title: `Correction delay: ${between} events after the first failing check`,
      summary: `${ws.agentName(k.agentId)}'s correction of ${claim.id} came ${between} events after the first failing check of ${subjectLabel(key)}.`,
      explanation: `First failing check of ${subjectLabel(key)}: ${firstFail.id} (#${firstFail.sequence}). Correction: ${k.id} (#${k.sequence}). ${between} records lie between them.`,
      detectedAt: k.sequence, detectedEventId: k.id,
      evidence: [{ eventId: firstFail.id, role: 'First failing check of the subject' }, { eventId: k.id, role: 'Correction' }],
      state: 'active',
    }, ws));
  }
  return out;
}

export const AW: MonitorDef = {
  id: 'AW', title: 'Correction delay strip', family: 'propagation', needs: ['correction', 'tool_result'], reads: ['claim'], fixture: 'src/data/fixtures/AW.json',
  rule: 'Active when: For each correction, the count of events between the first failing check of S by anyone and the correction\n' +
    'Insufficient when: First failing check withheld',
  run,
};
