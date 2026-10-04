// BL · Timeline gap. A monitor (one finding per gap) and a post-pass (see index.ts: blPostPass) that marks every
// finding in a gapped session insufficient with missing "continuous record". The post-pass runs only when BL is
// among the definitions being run, so registering BL is what switches it on.
import type { RecallEvent } from '../../model/types';
import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { finding, sessionsOf } from './util';

export const BL_MAX_GAP_MS = 2 * 3600_000;

export interface TimelineGap { taskId: string; before: RecallEvent; after: RecallEvent; kind: 'backwards' | 'jump' }

/** Gaps between consecutive turn/verdict records of each session, in sequence order. */
export function timelineGaps(ws: WorldState): TimelineGap[] {
  const out: TimelineGap[] = [];
  const bySession = new Map<string, RecallEvent[]>();
  for (const e of ws.visible) {
    if (!e.taskId || !((e.type === 'action' && e.payload.turnId) || e.type === 'tool_result')) continue;
    bySession.set(e.taskId, [...(bySession.get(e.taskId) ?? []), e]);
  }
  for (const [taskId, list] of bySession) {
    for (let i = 1; i < list.length; i++) {
      const dt = Date.parse(list[i].timestamp) - Date.parse(list[i - 1].timestamp);
      if (dt < 0 || dt > BL_MAX_GAP_MS) { out.push({ taskId, before: list[i - 1], after: list[i], kind: dt < 0 ? 'backwards' : 'jump' }); break; }
    }
  }
  return out;
}

function run(ws: WorldState): Finding[] {
  const owners = new Map(sessionsOf(ws).map((s) => [s.spec.taskId, s.spec.owner]));
  return timelineGaps(ws).map((g) => finding({
    id: `BL:${g.taskId}`, monitor: 'BL', taskId: g.taskId, agentId: owners.get(g.taskId) ?? g.after.agentId,
    title: g.kind === 'backwards' ? 'Timeline gap: timestamps go backwards' : 'Timeline gap: jump of more than 2 h',
    summary: `Session ${g.taskId}'s record ${g.kind === 'backwards' ? 'goes back in time' : 'jumps forward more than 2 hours'} between #${g.before.sequence} and #${g.after.sequence}; findings in this session say so.`,
    explanation: `${g.before.id} at ${g.before.timestamp}, then ${g.after.id} at ${g.after.timestamp} (container reset or missing records).`,
    detectedAt: g.after.sequence, detectedEventId: g.after.id,
    evidence: [{ eventId: g.before.id, role: 'Last record before the gap' }, { eventId: g.after.id, role: 'First record after the gap' }],
    state: 'active',
  }, ws));
}

export const BL: MonitorDef = {
  id: 'BL', title: 'Timeline gap', family: 'process', needs: ['action'], reads: ['tool_result'], fixture: 'src/data/fixtures/BL.json',
  rule: 'Active when: Within one session, turn timestamps go backwards or jump > N hours (container reset)\n' +
    'Insufficient when: Marks every finding in that session `insufficient` with missing: "continuous record"',
  run,
};
