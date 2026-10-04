// AF · Checking concentration.
import type { EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, isVerification, keyOf, subjectClaims } from './util';

const MIN_CHECKS = 3;
const MIN_CLAIMANTS = 2;

function run(ws: WorldState): Finding[] {
  const claimants = new Map<string, Set<string>>();
  const firstClaim = new Map<string, EventOf<'claim'>>();
  for (const c of subjectClaims(ws)) {
    const k = keyOf(c);
    claimants.set(k, (claimants.get(k) ?? new Set()).add(c.agentId));
    if (!firstClaim.has(k)) firstClaim.set(k, c);
  }
  const checks = ws.visible.filter((e): e is EventOf<'tool_result'> => isVerification(e) && claimants.has(keyOf(e)) && [...claimants.get(keyOf(e))!].some((a) => a !== e.agentId));
  if (checks.length < MIN_CHECKS) return [];
  const checkers = new Set(checks.map((k) => k.agentId));
  if (checkers.size !== 1) return [];
  const claimantsChecked = new Set(checks.flatMap((k) => [...claimants.get(keyOf(k))!].filter((a) => a !== k.agentId)));
  if (claimantsChecked.size < MIN_CLAIMANTS) return [];
  const r = checks[0].agentId;
  const last = checks[checks.length - 1];
  const subjects = [...new Set(checks.map((k) => keyOf(k)))];
  return [finding({
    id: `AF:${r}`, monitor: 'AF', agentId: r, taskId: last.taskId ?? '',
    title: `Checking concentration: ${ws.agentName(r)} ran every check of others' claims`,
    summary: `All ${checks.length} verifications of subjects claimed by ${claimantsChecked.size} other agents were run by ${ws.agentName(r)}.`,
    explanation: `${checks.length} verification record${checks.length > 1 ? 's' : ''} check subjects claimed by other agents (${subjects.length} subject${subjects.length > 1 ? 's' : ''}); every one was run by ${ws.agentName(r)}.`,
    detectedAt: last.sequence, detectedEventId: last.id,
    evidence: [...checks.map((k) => ({ eventId: k.id, role: `Check by ${ws.agentName(r)}` })), ...subjects.map((s) => ({ eventId: firstClaim.get(s)!.id, role: 'Claim being checked' }))],
    state: 'active',
  }, ws)];
}

export const AF: MonitorDef = {
  id: 'AF', title: 'Checking concentration', family: 'swarm', needs: ['claim', 'tool_result', 'subject'], fixture: 'src/data/fixtures/AF.json',
  rule: 'Active when: All verifications of other agents\' claimed subjects in the window were run by one agent',
  ruling: '>= 3 checks of other agents\' claimed subjects, all by one agent, across >= 2 distinct claimants. A single check is not concentration.',
  run,
};
