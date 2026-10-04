// BM · Screenshot-only claim. Always insufficient with missing "screenshot <turn id>".
import type { EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, keyOf, resultsOf, subjectClaims, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const c of subjectClaims(ws)) {
    if (!c.taskId || c.payload.subject?.version !== 'live') continue;
    const key = keyOf(c);
    const navs = ws.visible.filter((e): e is EventOf<'action'> => e.type === 'action' && e.taskId === c.taskId && e.sequence < c.sequence && e.payload.navigates === c.payload.subject!.artifact);
    if (!navs.length) continue;
    if (resultsOf(ws, key).some((k) => k.taskId === c.taskId && k.sequence < c.sequence)) continue; // a text verdict exists
    const last = navs[navs.length - 1];
    out.push(finding({
      id: `BM:${c.id}`, monitor: 'BM', claimId: c.payload.claimId, taskId: c.taskId, agentId: c.agentId,
      title: `Screenshot-only claim: ${subjectLabel(key)}`,
      summary: `${ws.agentName(c.agentId)}'s session only looked at ${subjectLabel(key)} in the browser (${navs.length} navigation${navs.length > 1 ? 's' : ''}); no text verdict exists.`,
      explanation: `Session ${c.taskId} navigated to ${subjectLabel(key)} (${navs.map((n) => n.id).join(', ')}) and recorded no tool result for it before claim ${c.payload.claimId}. Only the screenshot could confirm it.`,
      detectedAt: c.sequence, detectedEventId: c.id,
      evidence: [...navs.slice(-3).map((n) => ({ eventId: n.id, role: 'Browser navigation' })), { eventId: c.id, role: `Claim ${c.payload.claimId}` }],
      missing: [`screenshot ${last.payload.turnId ?? last.id}`],
      state: 'insufficient',
    }, ws));
  }
  return out;
}

export const BM: MonitorDef = {
  id: 'BM', title: 'Screenshot-only claim', family: 'claim-evidence', needs: ['claim', 'action', 'subject'], reads: ['tool_result'], fixture: 'src/data/fixtures/BM.json',
  rule: 'Active when: Claim on S made in a session whose only actions on S are browser navigations with no text verdict\n' +
    'Insufficient when: Always `insufficient` with missing: "screenshot <turn id>"',
  run,
};
