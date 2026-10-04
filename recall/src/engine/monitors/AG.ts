// AG · Opposite assertions, no check.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, keyOf, quotesOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    const key = keyOf(c);
    const opp = quotesOf(ws, key).filter((q) => q.agentId !== c.agentId);
    if (!opp.length) continue;
    // Pair the claim with the opposing report nearest to it in sequence order.
    const q = opp.reduce((b, x) => (Math.abs(x.sequence - c.sequence) < Math.abs(b.sequence - c.sequence) ? x : b));
    const both = Math.max(q.sequence, c.sequence);
    const check = checksOf(ws, key).find((k) => k.sequence > both);
    out.push(finding({
      id: `AG:${c.id}`, monitor: 'AG', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Opposite assertions, no check: ${subjectLabel(key)}`,
      summary: `${ws.agentName(c.agentId)} says ${subjectLabel(key)} is live; ${q.payload.isHuman ? 'a human' : ws.agentName(q.agentId)} says it failed` + (check ? `; a check at #${check.sequence} settles it.` : '; nobody checked after either.'),
      explanation: `Claim ${c.payload.claimId} (#${c.sequence}) and failure report ${q.id} (#${q.sequence}) assert opposite states of ${subjectLabel(key)}.`,
      detectedAt: both, detectedEventId: both === c.sequence ? c.id : q.id,
      evidence: [{ eventId: c.id, role: `Claim ${c.payload.claimId}` }, { eventId: q.id, role: 'Opposite assertion' }, ...(check ? [{ eventId: check.id, role: 'Check after both' }] : [])],
      missing: check ? [] : [`No check of ${subjectLabel(key)} after #${both}.`],
      state: check ? 'resolved' : 'active',
      resolution: check ? { eventId: check.id, text: `Check at #${check.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const AG: MonitorDef = {
  id: 'AG', title: 'Opposite assertions, no check', family: 'swarm', needs: ['claim', 'quote', 'subject'], reads: ['tool_result'], fixture: 'src/data/fixtures/AG.json',
  rule: 'Active when: One agent claims S live, another\'s message or `quote` says S failed, no verification of S after either\n' +
    'Resolves when: A check of S',
  run,
};
