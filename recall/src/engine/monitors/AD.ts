// AD · Convention adoption (informational).
import type { RecallEvent } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding } from './util';

const MIN_ADOPTERS = 3;

function run(ws: WorldState): Finding[] {
  const first = new Map<string, RecallEvent>();
  const adopters = new Map<string, Map<string, RecallEvent>>();
  for (const e of ws.visible) {
    if (e.type !== 'message' || e.payload.isHuman) continue;
    for (const tok of e.payload.conventions ?? []) {
      const f = first.get(tok);
      if (!f) { first.set(tok, e); adopters.set(tok, new Map()); continue; }
      if (e.agentId !== f.agentId && !adopters.get(tok)!.has(e.agentId)) adopters.get(tok)!.set(e.agentId, e);
    }
  }
  const out: Finding[] = [];
  for (const [tok, by] of adopters) {
    if (by.size < MIN_ADOPTERS) continue;
    const f = first.get(tok)!;
    const uses = [...by.values()];
    const third = uses[MIN_ADOPTERS - 1];
    out.push(finding({
      id: `AD:${tok}`, monitor: 'AD', taskId: f.taskId ?? '', agentId: f.agentId,
      title: `Convention adoption: ${tok}`,
      summary: `${ws.agentName(f.agentId)} first used ${tok}; ${by.size} other agents used it later.`,
      explanation: `First use ${f.id} (#${f.sequence}); later uses by ${uses.map((u) => ws.agentName(u.agentId)).join(', ')} (rule 'convention-token').`,
      detectedAt: third.sequence, detectedEventId: third.id,
      evidence: [{ eventId: f.id, role: 'First use' }, ...uses.slice(0, 6).map((u) => ({ eventId: u.id, role: `Used by ${ws.agentName(u.agentId)}` }))],
      state: 'active',
    }, ws));
  }
  return out;
}

export const AD: MonitorDef = {
  id: 'AD', title: 'Convention adoption', family: 'swarm', needs: ['message'], fixture: 'src/data/fixtures/AD.json',
  rule: 'Active when: A token (prefix, tag, handle scheme) first used by one agent is used by >= 3 agents later',
  run,
};
