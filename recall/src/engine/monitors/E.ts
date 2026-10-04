// E · Correction not propagated.
import type { RecallEvent } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, isPass, keyOf, quotesOf, subjectClaims, subjectKey, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    const key = keyOf(c);
    // Failure signals about S by someone else: corrections of a claim on S, and failure reports (quotes).
    const signals: RecallEvent[] = [
      ...ws.visible.filter((e) => e.type === 'correction' && subjectKey(ws.claims.get(e.payload.supersedes)?.subject) === key),
      ...quotesOf(ws, key),
    ].filter((e) => e.agentId !== c.agentId && e.sequence < c.sequence).sort((a, b) => a.sequence - b.sequence);
    const f = signals[signals.length - 1];
    if (!f) continue;
    const passes = checksOf(ws, key).filter(isPass);
    if (passes.some((k) => k.sequence > f.sequence && k.sequence < c.sequence)) continue;
    const ack = ws.visible.find((e) => e.type === 'acknowledgement' && e.agentId === c.agentId && e.payload.acknowledges === f.id && e.sequence > c.sequence);
    const own = ws.visible.find((e) => e.type === 'correction' && e.agentId === c.agentId && e.payload.supersedes === c.payload.claimId);
    const pass = passes.find((k) => k.sequence > c.sequence);
    const resolver = [ack, own, pass].filter((x): x is RecallEvent => !!x).sort((a, b) => a.sequence - b.sequence)[0];
    const human = f.type === 'quote' && !!f.payload.isHuman;
    out.push(finding({
      attributes: [`source: ${human ? 'human' : 'agent'}`],
      id: `E:${c.id}`, monitor: 'E', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Correction not propagated: ${subjectLabel(key)}`,
      summary: `${ws.agentName(c.agentId)} claimed ${subjectLabel(key)} after ${human ? 'a human' : ws.agentName(f.agentId)} reported it failing (#${f.sequence}), with no new passing check between` + (resolver ? `; resolved at #${resolver.sequence}.` : '.'),
      explanation: `${f.id} (${f.type}) reports a failure of ${subjectLabel(key)}. Claim ${c.payload.claimId} came later with no passing check in between. Receipt is not assumed.`,
      detectedAt: c.sequence, detectedEventId: c.id,
      evidence: [{ eventId: f.id, role: f.type === 'correction' ? 'Correction' : `Failure report (${human ? 'human' : 'agent'})` }, { eventId: c.id, role: `Claim ${c.payload.claimId}` },
        ...(resolver ? [{ eventId: resolver.id, role: resolver.type === 'tool_result' ? 'Passing check' : resolver.type === 'correction' ? 'Claimant corrected' : 'Claimant acknowledged' }] : [])],
      missing: resolver ? [] : [`No acknowledgement or correction by ${ws.agentName(c.agentId)} and no passing check.`],
      state: resolver ? 'resolved' : 'active',
      resolution: resolver ? { eventId: resolver.id, text: `Resolved at #${resolver.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const E: MonitorDef = {
  id: 'E', title: 'Correction not propagated', family: 'propagation', needs: ['claim', 'subject'], reads: ['correction', 'quote', 'acknowledgement', 'tool_result'], fixture: 'src/data/fixtures/E.json',
  rule: 'Active when: After a correction or human/agent `quote` of failure for S, another agent claims S live/pass with no new passing check between\n' +
    'Resolves when: That agent acknowledges, corrects, or a pass appears\n' +
    'Insufficient when: Correction withheld',
  run,
};
