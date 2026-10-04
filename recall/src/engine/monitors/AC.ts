// AC · Directive without observed uptake. Never "ignored": reported as insufficient ("not observed") until uptake appears.
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, sessionsOf } from './util';

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  const sessions = sessionsOf(ws);
  for (const d of ws.visible) {
    if (d.type !== 'directive') continue;
    const toks = new Set(d.payload.tokens);
    for (const t of d.payload.to) {
      const session = sessions.find((s) => s.spec.owner === t && s.created.sequence > d.sequence && (s.spec.goalKey ?? '').split(' ').some((w) => toks.has(w)));
      const ack = ws.visible.find((e) => e.type === 'acknowledgement' && e.agentId === t && e.payload.acknowledges === d.id);
      const up = [session?.created, ack].filter(Boolean).sort((a, b) => a!.sequence - b!.sequence)[0];
      out.push(finding({
        id: `AC:${d.id}:${t}`, monitor: 'AC', taskId: d.taskId ?? '', agentId: d.agentId,
        title: `Directive without observed uptake: ${ws.agentName(t)}`,
        summary: up ? `${ws.agentName(t)} took up ${ws.agentName(d.agentId)}'s directive at #${up.sequence}.` : `${ws.agentName(d.agentId)} directed ${ws.agentName(t)}; no uptake is observed (no matching session, no acknowledgement). This is "not observed", never "ignored".`,
        explanation: `${d.id} @mentions ${ws.agentName(t)} with an imperative (rule 'directive'). Uptake = a later session by ${ws.agentName(t)} whose goal shares a content token with the directive, or an acknowledgement.`,
        detectedAt: d.sequence, detectedEventId: d.id,
        evidence: [{ eventId: d.id, role: 'Directive' }, ...(up ? [{ eventId: up.id, role: up.type === 'task_created' ? 'Matching session' : 'Acknowledgement' }] : [])],
        missing: up ? [] : [`observed uptake by ${ws.agentName(t)}`],
        state: up ? 'resolved' : 'insufficient',
        resolution: up ? { eventId: up.id, text: `Uptake at #${up.sequence}.` } : undefined,
      }, ws));
    }
  }
  return out;
}

export const AC: MonitorDef = {
  id: 'AC', title: 'Directive without observed uptake', family: 'swarm', needs: ['directive'], reads: ['task_created', 'acknowledgement'], fixture: 'src/data/fixtures/AC.json',
  ruling: 'Never active: insufficient ("not observed") until uptake appears. Directive = an @mention plus, in the same sentence, can you | could you | would you | please, or a sentence-start imperative (check, run, deploy, fix, review, test, update, post, add, merge, verify). More than 2 @mentions is a broadcast, not a directive.',
  rule: 'Active when: `directive` to agent T; no later session by T whose goal shares a content token with the directive and no acknowledgement\n' +
    'Resolves when: Such a session or ack\n' +
    'Insufficient when: Always reported as "not observed", never "ignored"',
  run,
};
