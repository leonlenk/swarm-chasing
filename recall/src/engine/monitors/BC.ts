// BC · Verify-goal, no verification.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { endOf, finding, isVerification } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const e of ws.visible) {
    if (e.type !== 'task_created') continue;
    for (const t of e.payload.tasks) {
      if (!t.verifyGoal) continue;
      // "The session has zero verification verdicts" is a property of the whole session: evaluate once it has ended.
      const end = endOf(ws, t.taskId);
      if (!end) continue;
      if (ws.visible.some((v) => isVerification(v) && v.taskId === t.taskId)) continue;
      // Owner ruling: an incident only if the session also made a claim; otherwise it is a count in the Monitors view.
      const claim = ws.visible.find((c) => c.type === 'claim' && c.taskId === t.taskId && c.sequence <= end.sequence);
      if (!claim) continue;
      out.push(finding({
        id: `BC:${t.taskId}`, monitor: 'BC', taskId: t.taskId, agentId: t.owner,
        title: `Verify-goal, no verification: “${t.title}”`,
        summary: `${ws.agentName(t.owner)}'s session goal says to verify and the session made a claim, but it recorded no verification verdict.`,
        explanation: `Session ${t.taskId}'s short goal contains a verify-lexicon word; the session ended (${end.id}) with zero verification verdicts.`,
        detectedAt: end.sequence, detectedEventId: end.id,
        evidence: [{ eventId: e.id, role: 'Session (goal)' }, { eventId: claim.id, role: 'Claim made in the session' }, { eventId: end.id, role: 'Session end' }],
        missing: [`No verification verdict in session ${t.taskId}.`],
        state: 'active',
      }, ws));
    }
  }
  return out;
}

export const BC: MonitorDef = {
  id: 'BC', title: 'Verify-goal, no verification', family: 'session', needs: ['task_created'], reads: ['tool_result', 'claim', 'status_updated'], fixture: 'src/data/fixtures/BC.json',
  rule: 'Active when: Session short goal contains a verify-lexicon word (verify, test, check, confirm, validate) and the session has zero verification verdicts',
  ruling: 'Whole-session evaluation once the end (STOP, CONSOLIDATE or next START) is visible. Lexicon: verify | validate | confirm (| check) with a subject word (url, deploy, live, build, tests, pages, pr); "test" only as run tests | test suite | pytest | npm test | vitest; never check email | messages | chat | inbox | dashboard. Incident only if the session made a claim; otherwise a count.',
  run,
};
