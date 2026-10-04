// AU · Number in claim differs from record.
import type { EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, keyOf, resultsOf, subjectClaims, subjectLabel } from './util';

const observedOf = (e: EventOf<'tool_result'>, kind: 'tests-passed' | 'http-status') => (kind === 'tests-passed' ? e.payload.observed?.passed : e.payload.observed?.status);

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    const n = c.payload.number;
    if (!n) continue;
    const key = keyOf(c);
    const records = resultsOf(ws, key).filter((k) => observedOf(k, n.kind) !== undefined);
    const cited = c.evidenceRefs.map((r) => ws.byId.get(r)).find((e): e is EventOf<'tool_result'> => e?.type === 'tool_result' && observedOf(e, n.kind) !== undefined);
    const basis = cited ?? records.filter((k) => k.sequence < c.sequence).pop();
    if (!basis || observedOf(basis, n.kind) === n.value) continue;
    const match = records.find((k) => k.sequence > c.sequence && observedOf(k, n.kind) === n.value);
    const label = n.kind === 'tests-passed' ? 'tests passed' : 'HTTP status';
    out.push(finding({
      id: `AU:${c.id}`, monitor: 'AU', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Number in claim differs from record: ${n.value} vs ${observedOf(basis, n.kind)}`,
      summary: `${ws.agentName(c.agentId)}'s claim states ${n.value} ${label}; the record ${basis.payload.runId} shows ${observedOf(basis, n.kind)}` + (match ? `; a record with ${n.value} appeared at #${match.sequence}.` : '.'),
      explanation: `Claim ${c.payload.claimId} on ${subjectLabel(key)} states ${n.value} (rule 'claim-number'); ${cited ? 'the cited' : 'the latest same-subject'} record ${basis.id} parsed ${observedOf(basis, n.kind)}.`,
      detectedAt: c.sequence, detectedEventId: c.id,
      evidence: [{ eventId: basis.id, role: cited ? 'Cited record' : 'Latest same-subject record' }, { eventId: c.id, role: `Claim ${c.payload.claimId}` },
        ...(match ? [{ eventId: match.id, role: 'Record with the stated number' }] : [])],
      missing: match ? [] : [`No record of ${subjectLabel(key)} showing ${n.value}.`],
      state: match ? 'resolved' : 'active',
      resolution: match ? { eventId: match.id, text: `Record with ${n.value} at #${match.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const AU: MonitorDef = {
  id: 'AU', title: 'Number in claim differs from record', family: 'claim-evidence', needs: ['claim', 'tool_result', 'subject'], fixture: 'src/data/fixtures/AU.json',
  rule: 'Active when: Claim text contains a number next to a pass word ("47 tests pass", "200 OK") and the cited or same-subject record\'s parsed number differs\n' +
    'Resolves when: Record with the stated number appears',
  run,
};
