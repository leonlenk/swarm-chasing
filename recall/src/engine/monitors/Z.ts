// Z · Phantom reference.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, isAgentChat } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  const outputs = ws.visible.filter((e) => (e.type === 'tool_result' || e.type === 'action') && e.refs?.length);
  const chats = ws.visible.filter((e) => isAgentChat(e) && e.refs?.length);
  for (const m of chats) {
    for (const ref of m.refs ?? []) {
      if (chats.some((x) => x.sequence < m.sequence && x.refs?.includes(ref))) continue; // named in an earlier message
      if (ws.referencesSeen.some((r) => r.ref === ref && r.sequence < m.sequence)) continue; // seen earlier in the window / lookback (carried)
      if (outputs.some((x) => x.sequence < m.sequence && x.refs?.includes(ref))) continue; // appeared in tool output
      const appears = outputs.find((x) => x.sequence > m.sequence && x.refs?.includes(ref));
      out.push(finding({
        id: `Z:${m.id}:${ref}`, monitor: 'Z', taskId: m.taskId ?? '', agentId: m.agentId,
        title: `Phantom reference: ${ref}`,
        summary: `${ws.agentName(m.agentId)} named ${ref}, which appears in no earlier tool output or message` + (appears ? `; it appears in output at #${appears.sequence}.` : '.'),
        explanation: `${m.id} names ${ref}. No tool output or earlier message in the window contains it` + (appears ? `; ${appears.id} later does.` : '.'),
        detectedAt: m.sequence, detectedEventId: m.id,
        evidence: [{ eventId: m.id, role: 'Message naming the reference' }, ...(appears ? [{ eventId: appears.id, role: 'Reference appears in output' }] : [])],
        missing: appears ? [] : [ref],
        state: appears ? 'resolved' : 'insufficient',
        resolution: appears ? { eventId: appears.id, text: `${ref} appears in output at #${appears.sequence}.` } : undefined,
      }, ws));
    }
  }
  return out;
}

export const Z: MonitorDef = {
  id: 'Z', title: 'Phantom reference', family: 'process', needs: ['message'], reads: ['claim', 'correction', 'tool_result', 'action'], fixture: 'src/data/fixtures/Z.json',
  ruling: 'State is always insufficient (never active) with missing: [the reference string]; resolves when the reference appears in any output. Earlier references in the whole 4-hour window and the 12 h lookback come from carried reference_seen records.',
  rule: 'Active when: Chat names a URL or path that appears in no tool output and no earlier message in the window\n' +
    'Resolves when: The reference appears in output\n' +
    'Insufficient when: Always a count; lists the reference as `missing`',
  run,
};
