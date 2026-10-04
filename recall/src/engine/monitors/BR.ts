// BR · Human correction unanswered.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, isAgentChat, namesSubject, subjectKey, subjectLabel } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const h of ws.visible) {
    if (h.type !== 'quote' || !h.payload.isHuman) continue;
    const key = subjectKey(h.payload.subject);
    const reply = ws.visible.find((e) => e.sequence > h.sequence && e.room === h.room && (isAgentChat(e) || e.type === 'claim') && namesSubject(ws, e, key));
    out.push(finding({
      id: `BR:${h.id}`, monitor: 'BR', taskId: h.taskId ?? '', agentId: h.agentId,
      title: `Human correction unanswered: ${subjectLabel(key)}`,
      summary: reply ? `${ws.agentName(reply.agentId)} named ${subjectLabel(key)} in the room at #${reply.sequence}.` : `A human reported ${subjectLabel(key)} failing; no agent named it in that room afterwards.`,
      explanation: `${h.id} is a human failure report naming ${subjectLabel(key)} (rule 'human-negative').` + (reply ? ` ${reply.id} is the first agent record in the room naming it.` : ''),
      detectedAt: h.sequence, detectedEventId: h.id,
      evidence: [{ eventId: h.id, role: 'Human failure report' }, ...(reply ? [{ eventId: reply.id, role: 'Agent names the subject' }] : [])],
      missing: reply ? [] : [`No agent message naming ${subjectLabel(key)} in that room afterwards.`],
      state: reply ? 'resolved' : 'active',
      resolution: reply ? { eventId: reply.id, text: `Named at #${reply.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const BR: MonitorDef = {
  id: 'BR', title: 'Human correction unanswered', family: 'human', needs: ['quote'], reads: ['message', 'claim', 'correction', 'directive'], fixture: 'src/data/fixtures/BR.json',
  rule: 'Active when: `USER_TALK` naming S with `NEGATIVE_RE`; no agent message naming S in that room afterwards in the window\n' +
    'Resolves when: An agent message names S',
  run,
};
