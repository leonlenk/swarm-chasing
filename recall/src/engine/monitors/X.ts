// X · Step repetition.
import type { EventOf } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding } from './util';

const MIN_RUN = 3;

function run(ws: WorldState): Finding[] {
  const out: Finding[] = [];
  const bySession = new Map<string, EventOf<'action'>[]>();
  for (const e of ws.visible) {
    if (e.type !== 'action' || !e.taskId || !e.payload.turnId) continue;
    bySession.set(e.taskId, [...(bySession.get(e.taskId) ?? []), e]);
  }
  for (const [taskId, turns] of bySession) {
    let i = 0;
    while (i < turns.length) {
      const t = turns[i];
      let j = i + 1;
      // Identity is the hash of the FULL command (displayed commands are clipped), plus the output hash.
      const same = (x: EventOf<'action'>) => x.payload.kind === 'command' && !!x.payload.commandHash && x.payload.commandHash === t.payload.commandHash && x.payload.outputHash === t.payload.outputHash;
      if (t.payload.kind === 'command' && t.payload.commandHash) {
        while (j < turns.length && same(turns[j])) j++;
      }
      const runLen = j - i;
      if (runLen >= MIN_RUN) {
        const turnsRun = turns.slice(i, j);
        const next = turns[j];
        const changed = next && next.payload.commandHash === t.payload.commandHash && next.payload.outputHash !== t.payload.outputHash ? next : undefined;
        out.push(finding({
          id: `X:${t.id}`, monitor: 'X', taskId, agentId: t.agentId,
          title: `Step repetition ×${runLen}: ${t.payload.command}`,
          summary: `${ws.agentName(t.agentId)} ran the same command ${runLen} times in a row with identical output` + (changed ? `; the output changed at #${changed.sequence}.` : '.'),
          explanation: `${runLen} consecutive turns in session ${taskId} ran \`${t.payload.command}\` and produced the same output (hash ${t.payload.outputHash}).` +
            (changed ? ` The next run of the same command produced different output (${changed.id}).` : ''),
          detectedAt: turnsRun[MIN_RUN - 1].sequence, detectedEventId: turnsRun[MIN_RUN - 1].id,
          evidence: [...turnsRun.map((x, n) => ({ eventId: x.id, role: `Repetition ${n + 1}` })), ...(changed ? [{ eventId: changed.id, role: 'Output changed' }] : [])],
          state: changed ? 'resolved' : 'active',
          resolution: changed ? { eventId: changed.id, text: `Output changed at #${changed.sequence}.` } : undefined,
        }, ws));
      }
      i = Math.max(j, i + 1);
    }
  }
  return out;
}

export const X: MonitorDef = {
  id: 'X', title: 'Step repetition', family: 'process', needs: ['action'], fixture: 'src/data/fixtures/X.json',
  rule: 'Active when: >= 3 consecutive turns in one session with identical command and identical output\n' +
    'Resolves when: Output changes',
  run,
};
