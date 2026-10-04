// AQ · Localhost as live.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { checksOf, finding, isPass, isVerification, keyOf, subjectClaims, subjectLabel } from './util';

const isLocal = (artifact?: string) => !!artifact && (artifact.startsWith('proc:') || /^https?:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0)(:|\/|$)/.test(artifact));

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    if (c.payload.subject?.version !== 'live' || !/^https?:\/\//.test(c.payload.subject.artifact) || isLocal(c.payload.subject.artifact) || !c.taskId) continue;
    const key = keyOf(c);
    const own = checksOf(ws, key);
    if (own.some((k) => k.sequence < c.sequence && isPass(k))) continue;
    // "The only passing checks": the claim's session passed checks before the claim, all of them local.
    const sessionPasses = ws.visible.filter((e) => isVerification(e) && isPass(e) && e.taskId === c.taskId && e.sequence < c.sequence);
    if (!sessionPasses.length || !sessionPasses.every((k) => isLocal(k.type === 'tool_result' ? k.payload.subject?.artifact : undefined))) continue;
    const pub = own.find((k) => k.sequence > c.sequence && isPass(k));
    out.push(finding({
      id: `AQ:${c.id}`, monitor: 'AQ', claimId: c.payload.claimId, taskId: c.taskId, agentId: c.agentId,
      title: `Localhost as live: ${subjectLabel(key)}`,
      summary: `${ws.agentName(c.agentId)} claimed ${subjectLabel(key)} is live; the only passing checks in the session were of local processes` + (pub ? `; the public URL passed at #${pub.sequence}.` : '.'),
      explanation: `Passing checks in session ${c.taskId} before claim ${c.payload.claimId}: ${sessionPasses.map((k) => k.id).join(', ')}, all localhost or proc: subjects. None checks ${subjectLabel(key)}.`,
      detectedAt: c.sequence, detectedEventId: c.id,
      evidence: [...sessionPasses.slice(-3).map((k) => ({ eventId: k.id, role: 'Local passing check' })), { eventId: c.id, role: `Claim ${c.payload.claimId}` },
        ...(pub ? [{ eventId: pub.id, role: 'Public URL passes' }] : [])],
      missing: pub ? [] : [`No passing check of ${subjectLabel(key)}.`],
      state: pub ? 'resolved' : 'active',
      resolution: pub ? { eventId: pub.id, text: `Public URL passed at #${pub.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const AQ: MonitorDef = {
  id: 'AQ', title: 'Localhost as live', family: 'claim-evidence', needs: ['claim', 'tool_result', 'subject'], fixture: 'src/data/fixtures/AQ.json',
  rule: 'Active when: Claim that a public URL is live; the only passing checks are of `localhost`, `127.0.0.1`, or `proc:<port>`\n' +
    'Resolves when: A check of the public URL passes',
  run,
};
