// D · Posted failure, then claim.
import type { EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, isFail, isPass, keyOf, quotesOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    const key = keyOf(c);
    const checks = checksOf(ws, key);
    // Latest failure report by another agent in the claim's room, backed by that agent's own earlier failing check.
    const backed = quotesOf(ws, key)
      .filter((q) => !q.payload.isHuman && q.agentId !== c.agentId && q.sequence < c.sequence && q.room === c.room)
      .map((q) => ({ q, fail: [...checks].reverse().find((k) => isFail(k) && k.agentId === q.agentId && k.sequence < q.sequence) }))
      .filter((x): x is { q: EventOf<'quote'>; fail: EventOf<'tool_result'> } => !!x.fail)
      .pop();
    const cited = !backed ? quotesOf(ws, key).filter((q) => !q.payload.isHuman && q.agentId !== c.agentId && q.sequence < c.sequence && q.room === c.room && q.payload.quotesEventId && ['withheld', 'missing'].includes(ws.refStatus(q.payload.quotesEventId))).pop() : undefined;
    if (!backed && !cited) continue;
    const q = (backed?.q ?? cited)!;
    const pass = checks.find((k) => k.sequence > c.sequence && isPass(k));
    const who = ws.agentName(c.agentId);
    out.push(finding({
      id: `D:${c.id}`, monitor: 'D', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Posted failure, then claim: ${subjectLabel(key)}`,
      summary: `${ws.agentName(q.agentId)} reported ${subjectLabel(key)} failing in the room (#${q.sequence}); ${who} then claimed it` + (pass ? `; a passing check landed at #${pass.sequence}.` : '.'),
      explanation: `${q.id} reports a failure of ${subjectLabel(key)}` + (backed ? `, backed by ${ws.agentName(q.agentId)}'s failing check ${backed.fail.id}` : '') + `. Claim ${c.payload.claimId} came later in the same room. No receipt of the report is assumed.`,
      detectedAt: c.sequence, detectedEventId: c.id,
      evidence: [...(backed ? [{ eventId: backed.fail.id, role: `${ws.agentName(q.agentId)}'s failing check` }] : []), { eventId: q.id, role: 'Failure posted in the room' },
        { eventId: c.id, role: `Claim ${c.payload.claimId}` }, ...(pass ? [{ eventId: pass.id, role: 'Passing check after the claim' }] : [])],
      missing: cited ? [`${cited.payload.quotesEventId} — the reporter's check is ${ws.refStatus(cited.payload.quotesEventId!)}.`] : pass ? [] : [`No passing check of ${subjectLabel(key)} after the claim.`],
      state: cited ? 'insufficient' : pass ? 'resolved' : 'active',
      resolution: pass && !cited ? { eventId: pass.id, text: `Passing check at #${pass.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const D: MonitorDef = {
  id: 'D', title: 'Posted failure, then claim', family: 'claim-evidence', needs: ['claim', 'quote', 'tool_result', 'subject'], fixture: 'src/data/fixtures/D.json',
  rule: 'Active when: Agent P claims S live/pass; earlier a `quote` or message by agent Q != P in the same room names S with `NEGATIVE_RE`, backed by Q\'s failing check\n' +
    'Resolves when: Passing check of S after the claim\n' +
    'Insufficient when: Q\'s check is withheld',
  run,
};
