// AH · Human question unanswered.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, isAgentChat } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const q of ws.visible) {
    // Human questions: plain messages, and human failure reports (quotes) that end in '?'.
    if (!((q.type === 'message' || q.type === 'quote') && q.payload.isHuman && q.payload.isQuestion)) continue;
    const reply = ws.visible.find((e) => e.sequence > q.sequence && isAgentChat(e) && e.room === q.room);
    out.push(finding({
      id: `AH:${q.id}`, monitor: 'AH', agentId: q.agentId, taskId: q.taskId ?? '',
      title: 'Human question unanswered',
      summary: reply ? `A human question was answered by ${ws.agentName(reply.agentId)} at #${reply.sequence}.` : 'A human asked a question and no agent posted in that room afterwards in the window.',
      explanation: `${q.id} is a human message ending in "?"` + (reply ? `; ${reply.id} is the first agent message in the same room after it.` : '; no agent message in the same room follows it in the window.'),
      detectedAt: q.sequence, detectedEventId: q.id,
      evidence: [{ eventId: q.id, role: 'Human question' }, ...(reply ? [{ eventId: reply.id, role: 'First agent reply in the room' }] : [])],
      missing: reply ? [] : ['No agent message in that room after the question.'],
      state: reply ? 'resolved' : 'active',
      resolution: reply ? { eventId: reply.id, text: `${ws.agentName(reply.agentId)} replied at #${reply.sequence}.` } : undefined,
    }, ws));
  }
  return out;
}

export const AH: MonitorDef = {
  id: 'AH', title: 'Human question unanswered', family: 'human', needs: ['message'], reads: ['claim', 'correction', 'quote', 'directive'], fixture: 'src/data/fixtures/AH.json',
  rule: 'Active when: `USER_TALK` message ending in `?` with no agent message in that room after it in the window\n' +
    'Resolves when: An agent replies',
  run,
};
