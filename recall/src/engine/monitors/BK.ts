// BK · Empty-output evidence. Always insufficient with missing "non-empty output"; never counted as a pass.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, keyOf, resultsOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    const key = keyOf(c);
    const results = resultsOf(ws, key);
    const cited = c.evidenceRefs.map((r) => ws.byId.get(r)).filter((e) => e?.type === 'tool_result');
    const before = cited.length ? cited : results.filter((k) => k.sequence < c.sequence);
    if (!before.length || !before.every((k) => k?.type === 'tool_result' && k.payload.emptyOutput)) continue;
    const full = results.find((k) => k.sequence > c.sequence && !k.payload.emptyOutput);
    const last = before[before.length - 1]!;
    out.push(finding({
      id: `BK:${c.id}`, monitor: 'BK', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Empty-output evidence: ${subjectLabel(key)}`,
      summary: `${ws.agentName(c.agentId)}'s claim about ${subjectLabel(key)} ${cited.length ? 'cites' : 'is supported only by'} runs with empty output` + (full ? `; a non-empty run landed at #${full.sequence}.` : '.'),
      explanation: `${before.map((k) => k!.id).join(', ')} printed nothing; an empty result is "nothing there" or "not allowed to look", never a pass.`,
      detectedAt: c.sequence, detectedEventId: c.id,
      evidence: [{ eventId: last.id, role: 'Empty-output run' }, { eventId: c.id, role: `Claim ${c.payload.claimId}` }, ...(full ? [{ eventId: full.id, role: 'Non-empty run' }] : [])],
      missing: full ? [] : ['non-empty output'],
      state: full ? 'resolved' : 'insufficient',
      resolution: full ? { eventId: full.id, text: `Non-empty run at #${full.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const BK: MonitorDef = {
  id: 'BK', title: 'Empty-output evidence', family: 'claim-evidence', needs: ['claim', 'tool_result', 'subject'], fixture: 'src/data/fixtures/BK.json',
  rule: 'Active when: Claim cites or is supported only by a `tool_result` whose output is empty where the command should print (curl without `-s -o`, ls, pytest)\n' +
    'Resolves when: Non-empty run\n' +
    'Insufficient when: Always `insufficient` with missing: "non-empty output"',
  run,
};
