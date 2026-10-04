// BN · Agent mention without reply.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, isAgentChat } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  for (const m of ws.visible) {
    // Owner ruling: the '?' must be in the mention's sentence, and messages with more than 2 @mentions are broadcasts
    // (adapter rule 'question-to' encodes both).
    const to = m.type === 'message' && !m.payload.isHuman ? m.payload.questionTo : m.type === 'directive' ? m.payload.questionTo : undefined;
    if (!to?.length) continue;
    for (const t of to) {
      const reply = ws.visible.find((e) => e.sequence > m.sequence && e.agentId === t && isAgentChat(e) && e.room === m.room);
      out.push(finding({
        id: `BN:${m.id}:${t}`, monitor: 'BN', taskId: m.taskId ?? '', agentId: m.agentId,
        title: `Agent mention without reply: ${ws.agentName(t)}`,
        summary: reply ? `${ws.agentName(t)} replied at #${reply.sequence}.` : `${ws.agentName(m.agentId)} asked ${ws.agentName(t)} a question; ${ws.agentName(t)} posted nothing in that room afterwards.`,
        explanation: `${m.id} @mentions ${ws.agentName(t)} in a sentence with "?" (rule 'question-to').` + (reply ? ` ${reply.id} is ${ws.agentName(t)}'s next post in the room.` : ''),
        detectedAt: m.sequence, detectedEventId: m.id,
        evidence: [{ eventId: m.id, role: 'Question to the agent' }, ...(reply ? [{ eventId: reply.id, role: 'Reply' }] : [])],
        missing: reply ? [] : [`No post by ${ws.agentName(t)} in that room afterwards.`],
        state: reply ? 'resolved' : 'active',
        resolution: reply ? { eventId: reply.id, text: `Replied at #${reply.sequence}.` } : undefined,
      }, ws));
    }
  }
  return out;
}

export const BN: MonitorDef = {
  id: 'BN', title: 'Agent mention without reply', family: 'swarm', needs: ['message'], reads: ['directive', 'claim', 'quote', 'correction'], fixture: 'src/data/fixtures/BN.json',
  ruling: 'The "?" must be in the mention\'s sentence; messages with more than 2 @mentions are broadcasts, not questions to one agent.',
  rule: 'Active when: Message @mentioning agent T and ending in `?`; T posts nothing in that room afterwards in the window\n' +
    'Resolves when: T replies',
  run,
};
