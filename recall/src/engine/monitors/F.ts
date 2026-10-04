// F · Repeat without recheck.
import type { EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, keyOf, subjectClaims, subjectLabel } from './util';

const MIN_CHAIN = 3;

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  const chains = new Map<string, EventOf<'claim'>[]>();
  const firstById = new Map<string, EventOf<'claim'>>();
  for (const c of subjectClaims(ws)) {
    if (!c.payload.repeatOf) { firstById.set(c.payload.claimId, c); chains.set(c.payload.claimId, [c]); continue; }
    const head = c.payload.repeatOf;
    chains.set(head, [...(chains.get(head) ?? []), c]);
  }
  for (const [head, chain] of chains) {
    if (chain.length < MIN_CHAIN) continue;
    const first = firstById.get(head) ?? chain[0];
    const last = chain[chain.length - 1];
    const key = keyOf(first);
    const inside = checksOf(ws, key).find((k) => k.sequence > first.sequence && k.sequence < last.sequence);
    const gone = first.evidenceRefs.filter((r) => ws.refStatus(r) === 'withheld' || ws.refStatus(r) === 'missing');
    const who = ws.agentName(first.agentId);
    out.push(finding({
      id: `F:${first.id}`, monitor: 'F', claimId: first.payload.claimId, taskId: last.taskId ?? '', agentId: first.agentId,
      title: `Repeat without recheck ×${chain.length}: ${subjectLabel(key)}`,
      summary: `${who} claimed ${subjectLabel(key)} ${chain.length} times` + (inside ? `; a check landed inside the chain at #${inside.sequence}.` : ' with no verification of it between the first and the last claim.'),
      explanation: `Claims ${chain.map((c) => c.payload.claimId).join(', ')} share subject and assertion (rule 'repeat'). ` + (inside ? `${inside.id} checks the subject between them.` : 'No verification of the subject lies between the first and the last.'),
      detectedAt: chain[MIN_CHAIN - 1].sequence, detectedEventId: chain[MIN_CHAIN - 1].id,
      evidence: [...chain.map((c, n) => ({ eventId: c.id, role: n === 0 ? 'First claim' : `Repeat ${n}` })), ...gone.map((r) => ({ eventId: r, role: 'Cited by the first claim' })),
        ...(inside ? [{ eventId: inside.id, role: 'Check inside the chain' }] : [])],
      missing: gone.length ? gone.map((r) => `${r} — cited by the first claim, ${ws.refStatus(r)}.`) : inside ? [] : [`No verification of ${subjectLabel(key)} between #${first.sequence} and #${last.sequence}.`],
      state: gone.length ? 'insufficient' : inside ? 'resolved' : 'active',
      resolution: inside && !gone.length ? { eventId: inside.id, text: `Check inside the chain at #${inside.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const F: MonitorDef = {
  id: 'F', title: 'Repeat without recheck', family: 'claim-evidence', needs: ['claim', 'subject'], reads: ['tool_result'], fixture: 'src/data/fixtures/F.json',
  rule: 'Active when: `repeatOf` chain for S has >= 3 claims with no verification of S between first and last\n' +
    'Resolves when: A check of S lands inside the chain\n' +
    'Insufficient when: First claim cites withheld',
  run,
};
