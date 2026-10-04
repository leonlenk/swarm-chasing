// AX · Human-prompted correction (a count: corrections split into human-prompted vs self-initiated).
import type { EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, quotesOf, subjectKey, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const k of ws.visible) {
    if (k.type !== 'correction') continue;
    const key = subjectKey(ws.claims.get(k.payload.supersedes)?.subject);
    if (!key) continue;
    const human = quotesOf(ws, key).filter((q) => q.payload.isHuman && q.sequence < k.sequence).pop() as EventOf<'quote'> | undefined;
    if (!human) continue;
    if (checksOf(ws, key).some((c) => c.agentId === k.agentId && c.sequence > human.sequence && c.sequence < k.sequence)) continue; // P re-checked first
    out.push(finding({
      attributes: ['human-prompted'],
      id: `AX:${k.id}`, monitor: 'AX', claimId: k.payload.supersedes, taskId: k.taskId ?? '', agentId: k.agentId,
      title: `Human-prompted correction: ${subjectLabel(key)}`,
      summary: `${ws.agentName(k.agentId)} corrected a claim about ${subjectLabel(key)} after a human reported it failing (#${human.sequence}), without re-checking first.`,
      explanation: `${human.id} is a human failure report naming ${subjectLabel(key)}; ${k.id} corrects ${k.payload.supersedes} with no check of the subject by ${ws.agentName(k.agentId)} between them.`,
      detectedAt: k.sequence, detectedEventId: k.id,
      evidence: [{ eventId: human.id, role: 'Human failure report' }, { eventId: k.id, role: 'Correction' }],
      state: 'active',
    }, ws));
  }
  return out;
}

export const AX: MonitorDef = {
  id: 'AX', title: 'Human-prompted correction', family: 'propagation', needs: ['correction', 'quote'], reads: ['claim', 'tool_result'], fixture: 'src/data/fixtures/AX.json',
  rule: 'Active when: Correction of S by agent P occurs after a `USER_TALK` naming S with `NEGATIVE_RE` and before any further check by P',
  run,
};
