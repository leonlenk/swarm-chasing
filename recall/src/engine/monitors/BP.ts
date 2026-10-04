// BP · Consensus without any check.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, claimsOn, finding, keyOf, subjectClaims, subjectLabel } from './util';

const MIN_AGENTS = 3;

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const key of new Set(subjectClaims(ws).map(keyOf))) {
    const firstPerAgent = claimsOn(ws, key).filter((c, i, all) => all.findIndex((x) => x.agentId === c.agentId) === i);
    if (firstPerAgent.length < MIN_AGENTS) continue;
    const nth = firstPerAgent[MIN_AGENTS - 1];
    const checks = checksOf(ws, key);
    if (checks.some((k) => k.sequence < nth.sequence)) continue; // a check existed before consensus formed
    const check = checks.find((k) => k.sequence > nth.sequence);
    out.push(finding({
      id: `BP:${key}`, monitor: 'BP', claimId: nth.payload.claimId, taskId: nth.taskId ?? '', agentId: nth.agentId,
      title: `Consensus without any check: ${subjectLabel(key)}`,
      summary: `${firstPerAgent.length} agents asserted ${subjectLabel(key)} with no verification of it by anyone` + (check ? `; a check landed at #${check.sequence}.` : '.'),
      explanation: `${firstPerAgent.map((c) => ws.agentName(c.agentId)).join(', ')} each claimed ${subjectLabel(key)}; no verification of the subject exists before the ${MIN_AGENTS}rd agent's claim.`,
      detectedAt: nth.sequence, detectedEventId: nth.id,
      evidence: [...firstPerAgent.map((c) => ({ eventId: c.id, role: `Claim by ${ws.agentName(c.agentId)}` })), ...(check ? [{ eventId: check.id, role: 'First check of the subject' }] : [])],
      missing: check ? [] : [`No verification of ${subjectLabel(key)} by anyone in the window.`],
      state: check ? 'resolved' : 'active',
      resolution: check ? { eventId: check.id, text: `Check of ${subjectLabel(key)} at #${check.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const BP: MonitorDef = {
  id: 'BP', title: 'Consensus without any check', family: 'swarm', needs: ['claim', 'subject'], fixture: 'src/data/fixtures/BP.json',
  rule: 'Active when: >= 3 agents assert S live/pass and there is no verification of S by anyone in the window\n' +
    'Resolves when: A check of S',
  run,
};
