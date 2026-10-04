// H · Hedge never closed.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, claimsOn, finding, isFail, isPass, keyOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    if (!c.payload.hedged) continue;
    const key = keyOf(c);
    const later = checksOf(ws, key).filter((k) => k.sequence > c.sequence);
    const correction = ws.visible.find((e) => e.type === 'correction' && e.agentId === c.agentId && e.payload.supersedes === c.payload.claimId && e.sequence > c.sequence);
    const followUps = claimsOn(ws, key).filter((x) => x.agentId === c.agentId && x.sequence > c.sequence);
    const unhedged = followUps.find((x) => !x.payload.hedged);
    const pass = later.find(isPass);
    const closer = correction ?? (unhedged && pass ? (unhedged.sequence > pass.sequence ? unhedged : pass) : undefined);
    if (!later.length || !isFail(later[0])) continue; // the hedge was followed by a failure
    if (!closer && (later.some(isPass) || followUps.length)) continue;
    const who = ws.agentName(c.agentId);
    out.push(finding({
      id: `H:${c.id}`, monitor: 'H', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Hedge never closed: ${subjectLabel(key)}`,
      summary: closer ? `${who}'s hedged claim about ${subjectLabel(key)} was closed at #${closer.sequence}.` : `${who} hedged a claim about ${subjectLabel(key)}; later checks only failed and ${who} never followed up or corrected it.`,
      explanation: `Claim ${c.payload.claimId} is hedged (rule 'hedged'). Checks after it: ${later.map((k) => `${k.id} ${k.payload.outcome}`).join(', ')}.`,
      detectedAt: later[0].sequence, detectedEventId: later[0].id,
      evidence: [{ eventId: c.id, role: `Hedged claim ${c.payload.claimId}` }, ...later.filter(isFail).slice(0, 4).map((k) => ({ eventId: k.id, role: 'Later failing check' })),
        ...(closer ? [{ eventId: closer.id, role: closer.type === 'correction' ? 'Correction' : 'Closed (unhedged claim with a pass)' }] : [])],
      missing: closer ? [] : [`No follow-up claim or correction by ${who}.`],
      state: closer ? 'resolved' : 'active',
      resolution: closer ? { eventId: closer.id, text: `Closed at #${closer.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const H: MonitorDef = {
  id: 'H', title: 'Hedge never closed', family: 'claim-evidence', needs: ['claim', 'tool_result', 'subject'], reads: ['correction'], fixture: 'src/data/fixtures/H.json',
  rule: 'Active when: Claim with `hedged: true`; later only failing checks of S; no follow-up claim or correction by the same agent\n' +
    'Resolves when: Unhedged claim with a pass, or correction',
  run,
};
