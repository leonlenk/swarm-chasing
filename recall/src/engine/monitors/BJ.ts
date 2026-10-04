// BJ · Error-suppressed check. Never active by the brief's row: always insufficient with missing "unsuppressed run".
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, isPass, keyOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    const key = keyOf(c);
    const checks = checksOf(ws, key);
    const before = checks.filter((k) => k.sequence < c.sequence);
    if (!before.length || !before.every((k) => k.payload.suppressed)) continue;
    const clean = checks.find((k) => k.sequence > c.sequence && !k.payload.suppressed && isPass(k));
    const last = before[before.length - 1];
    out.push(finding({
      id: `BJ:${c.id}`, monitor: 'BJ', claimId: c.payload.claimId, taskId: c.taskId ?? '', agentId: c.agentId,
      title: `Error-suppressed check: ${subjectLabel(key)}`,
      summary: `${ws.agentName(c.agentId)}'s claim about ${subjectLabel(key)} is supported only by checks that suppressed errors, so they could not fail` + (clean ? `; an unsuppressed run passed at #${clean.sequence}.` : '.'),
      explanation: `Checks before claim ${c.payload.claimId}: ${before.map((k) => k.id).join(', ')}; each command contains an error-suppression idiom (rule 'suppressed-error').`,
      detectedAt: c.sequence, detectedEventId: c.id,
      evidence: [{ eventId: last.id, role: 'Suppressed check' }, { eventId: c.id, role: `Claim ${c.payload.claimId}` }, ...(clean ? [{ eventId: clean.id, role: 'Unsuppressed passing run' }] : [])],
      missing: clean ? [] : ['unsuppressed run'],
      state: clean ? 'resolved' : 'insufficient',
      resolution: clean ? { eventId: clean.id, text: `Unsuppressed run passed at #${clean.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const BJ: MonitorDef = {
  id: 'BJ', title: 'Error-suppressed check', family: 'claim-evidence', needs: ['claim', 'tool_result', 'subject'], fixture: 'src/data/fixtures/BJ.json',
  rule: 'Active when: Claim supported only by a check whose command suppressed errors (`|| true`, `2>/dev/null`, `|| echo`, `set +e`, `; true`)\n' +
    'Resolves when: A check without suppression passes\n' +
    'Insufficient when: Always `insufficient` with missing: "unsuppressed run"',
  run,
};
