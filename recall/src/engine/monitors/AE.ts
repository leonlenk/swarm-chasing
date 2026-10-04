// AE · Adopted without own check.
import type { EventOf, RecallEvent } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, keyOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  const claims = subjectClaims(ws);
  // Adoption = a claim on S, or an action citing a claim on S.
  const adoptions: { e: RecallEvent; key: string }[] = [
    ...claims.map((c) => ({ e: c as RecallEvent, key: keyOf(c) })),
    ...ws.visible.filter((e): e is EventOf<'action'> => e.type === 'action').flatMap((a) =>
      a.payload.referencesClaims.map((id) => ws.claims.get(id)?.subject).filter(Boolean).map((s) => ({ e: a as RecallEvent, key: `${s!.artifact}@${s!.version}` }))),
  ];
  const seen = new Set<string>();
  for (const { e, key } of adoptions) {
    const prior = claims.find((c) => keyOf(c) === key && c.agentId !== e.agentId && c.sequence < e.sequence);
    if (!prior || seen.has(`${e.id}|${key}`)) continue;
    seen.add(`${e.id}|${key}`);
    const checks = checksOf(ws, key).filter((k) => k.agentId === e.agentId);
    if (checks.some((k) => k.sequence < e.sequence)) continue;
    const ownLater = checks.find((k) => k.sequence > e.sequence);
    const q = ws.agentName(e.agentId);
    out.push(finding({
      id: `AE:${e.id}:${key}`, monitor: 'AE', claimId: e.type === 'claim' ? e.payload.claimId : prior.payload.claimId, taskId: e.taskId ?? '', agentId: e.agentId,
      title: `Adopted without own check: ${subjectLabel(key)}`,
      summary: `${q} ${e.type === 'claim' ? 'claimed' : 'acted on'} ${subjectLabel(key)}, which ${ws.agentName(prior.agentId)} claimed at #${prior.sequence}, without checking it first` +
        (ownLater ? `; ${q} checked it at #${ownLater.sequence}.` : '.'),
      explanation: `${ws.agentName(prior.agentId)} claimed ${subjectLabel(key)} (${prior.id}). ${q}'s ${e.type} ${e.id} adopts the same subject; ${q} has no verification of it before that.`,
      detectedAt: e.sequence, detectedEventId: e.id,
      evidence: [{ eventId: prior.id, role: `Original claim by ${ws.agentName(prior.agentId)}` }, { eventId: e.id, role: `Adoption by ${q}` },
        ...(ownLater ? [{ eventId: ownLater.id, role: `${q}'s own check` }] : [])],
      missing: ownLater ? [] : [`No verification of ${subjectLabel(key)} by ${q}.`],
      state: ownLater ? 'resolved' : 'active',
      resolution: ownLater ? { eventId: ownLater.id, text: `${q} checked ${subjectLabel(key)} at #${ownLater.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const AE: MonitorDef = {
  id: 'AE', title: 'Adopted without own check', family: 'swarm', needs: ['claim', 'subject'], reads: ['action'], fixture: 'src/data/fixtures/AE.json',
  rule: 'Active when: Agent Q claims or acts on subject S that agent P claimed, and Q has no verification of S before doing so\n' +
    'Resolves when: Q checks S',
  run,
};
