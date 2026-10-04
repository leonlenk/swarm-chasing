// O · Retracted then re-asserted.
import type { EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, claimsOn, finding, isPass, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const k of ws.visible) {
    if (k.type !== 'correction') continue;
    const c0 = ws.claims.get(k.payload.supersedes);
    if (!c0?.subject || c0.agentId !== k.agentId) continue;
    const key = `${c0.subject.artifact}@${c0.subject.version}`;
    const c1 = claimsOn(ws, key).find((c) => c.agentId === k.agentId && c.sequence > k.sequence) as EventOf<'claim'> | undefined;
    if (!c1) continue;
    const passes = checksOf(ws, key).filter(isPass);
    if (passes.some((p) => p.sequence > k.sequence && p.sequence < c1.sequence)) continue;
    const pass = passes.find((p) => p.sequence > c1.sequence);
    const who = ws.agentName(k.agentId);
    out.push(finding({
      id: `O:${c1.id}`, monitor: 'O', claimId: c1.payload.claimId, taskId: c1.taskId ?? '', agentId: c1.agentId,
      title: `Retracted then re-asserted: ${subjectLabel(key)}`,
      summary: `${who} retracted a claim about ${subjectLabel(key)} (#${k.sequence}) and later claimed it again with no passing check between` + (pass ? `; a pass appeared at #${pass.sequence}.` : '.'),
      explanation: `${k.id} corrects ${c0.id}; ${c1.payload.claimId} re-asserts the subject. No passing check of ${subjectLabel(key)} lies between them.`,
      detectedAt: c1.sequence, detectedEventId: c1.id,
      evidence: [{ eventId: c0.eventId, role: `Original claim ${c0.id}` }, { eventId: k.id, role: 'Own correction' }, { eventId: c1.id, role: `Re-assertion ${c1.payload.claimId}` },
        ...(pass ? [{ eventId: pass.id, role: 'Passing check' }] : [])].filter((x) => x.eventId),
      missing: pass ? [] : [`No passing check of ${subjectLabel(key)} after the re-assertion.`],
      state: pass ? 'resolved' : 'active',
      resolution: pass ? { eventId: pass.id, text: `Pass at #${pass.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const O: MonitorDef = {
  id: 'O', title: 'Retracted then re-asserted', family: 'propagation', needs: ['claim', 'correction', 'subject'], reads: ['tool_result'], fixture: 'src/data/fixtures/O.json',
  rule: 'Active when: Agent corrects own claim on S, then later claims S again with no passing check between\n' +
    'Resolves when: Pass appears',
  run,
};
