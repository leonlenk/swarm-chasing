// Meta analyses (reported in the Monitors view, not as incidents).
//   BT · Unverifiable by construction: per agent, claims whose subject type has no verdict rule at all.
//   BU · Single-point findings: for each active finding, the records whose individual withholding flips it
//        to insufficient. A count of 1 means the finding hangs on one record.
// BT is a pure function of WorldState. BU needs the analysis INPUT (it re-runs reconstruction with one
// more record withheld), so it lives here rather than in the monitor registry.

import type { EventOf } from '../model/types';
import { reconstruct, type AnalysisInput, type WorldState } from './reconstruct';
import type { Finding, MonitorDef } from './monitors/types';

/** Subject "type": the version slot for rule-defined subjects (live, working-tree, …), else 'artifact-version'. */
const subjectType = (s?: { artifact: string; version: string }) => {
  if (!s) return 'none';
  if (s.version === 'live' || s.version === 'working-tree' || s.version === 'push') return s.version;
  return 'artifact-version';
};

export interface UnverifiableRow { agentId: string; claimIds: string[]; eventIds: string[]; subjectTypes: string[] }

/** BT: claims whose subject type is never produced by any verdict rule in this source. */
export function unverifiableByConstruction(ws: WorldState): UnverifiableRow[] {
  const verifiable = new Set(ws.visible
    .filter((e): e is EventOf<'tool_result'> => e.type === 'tool_result' && e.payload.category === 'verification')
    .map((e) => subjectType(e.payload.subject)));
  const rows = new Map<string, UnverifiableRow>();
  for (const e of ws.visible) {
    if (e.type !== 'claim') continue;
    const t = subjectType(e.payload.subject);
    if (t !== 'none' && verifiable.has(t)) continue;
    const r = rows.get(e.agentId) ?? { agentId: e.agentId, claimIds: [], eventIds: [], subjectTypes: [] };
    r.claimIds.push(e.payload.claimId); r.eventIds.push(e.id);
    if (!r.subjectTypes.includes(t)) r.subjectTypes.push(t);
    rows.set(e.agentId, r);
  }
  return [...rows.values()];
}

export interface SinglePointRow { findingId: string; monitor: string; flips: string[]; vanishes: string[]; evidence: number }

/**
 * BU: for each active finding at the cursor, withhold each of its evidence records in turn and re-run.
 * `flips` = records whose withholding turns the finding insufficient; `vanishes` = records whose withholding
 * removes it entirely (the monitor can no longer see the pattern).
 */
export function singlePointFindings(
  input: AnalysisInput, cursor: number, run: (ws: WorldState) => Finding[],
): SinglePointRow[] {
  const base = run(reconstruct(input, cursor)).filter((f) => f.state === 'active');
  return base.map((f) => {
    const flips: string[] = [];
    const vanishes: string[] = [];
    for (const ev of f.evidence) {
      const withheld = new Set(input.withheld); withheld.add(ev.eventId);
      const again = run(reconstruct({ ...input, withheld }, cursor)).find((x) => x.id === f.id);
      if (!again) vanishes.push(ev.eventId);
      else if (again.state === 'insufficient') flips.push(ev.eventId);
    }
    return { findingId: f.id, monitor: f.monitor, flips, vanishes, evidence: f.evidence.length };
  });
}

export type { MonitorDef };

/** Token set of a goal key (lowercase words). */
const tokens = (key: string) => new Set(key.split(' ').filter((w) => w.length > 1));
const jaccard = (a: Set<string>, b: Set<string>) => {
  const inter = [...a].filter((x) => b.has(x)).length;
  const union = new Set([...a, ...b]).size;
  return union ? inter / union : 0;
};

export interface GoalChurnRow { agentId: string; sessionIds: string[]; createdIds: string[] }

/**
 * BG · Goal churn (meta count, owner ruling 2026-10-03): an agent with >= 5 sessions whose short goals are
 * pairwise distinct by token set (Jaccard < 0.3), and zero verdicts AND zero claims in the window.
 */
export function goalChurn(ws: WorldState, minSessions = 5, maxJaccard = 0.3): GoalChurnRow[] {
  const rows: GoalChurnRow[] = [];
  const byAgent = new Map<string, { taskId: string; created: string; toks: Set<string> }[]>();
  for (const e of ws.visible) {
    if (e.type !== 'task_created') continue;
    for (const t of e.payload.tasks) if (t.goalKey) byAgent.set(t.owner, [...(byAgent.get(t.owner) ?? []), { taskId: t.taskId, created: e.id, toks: tokens(t.goalKey) }]);
  }
  for (const [agentId, sessions] of byAgent) {
    if (ws.visible.some((e) => e.agentId === agentId && (e.type === 'claim' || (e.type === 'tool_result' && e.payload.outcome !== 'inconclusive')))) continue;
    const picked: typeof sessions = [];
    for (const s of sessions) if (picked.every((p) => jaccard(p.toks, s.toks) < maxJaccard)) picked.push(s);
    if (picked.length >= minSessions) rows.push({ agentId, sessionIds: picked.map((p) => p.taskId), createdIds: picked.map((p) => p.created) });
  }
  return rows;
}

/** BC without a claim (owner ruling): verify-goal sessions that ended with zero verification verdicts and made no claim. A count, not an incident. */
export function verifyGoalNoClaim(ws: WorldState): { taskId: string; createdId: string }[] {
  const out: { taskId: string; createdId: string }[] = [];
  for (const e of ws.visible) {
    if (e.type !== 'task_created') continue;
    for (const t of e.payload.tasks) {
      if (!t.verifyGoal) continue;
      const ended = ws.visible.some((x) => x.taskId === t.taskId && (x.type === 'status_updated' ? x.payload.status === 'ended' : x.statusAfter === 'ended'));
      if (!ended) continue;
      if (ws.visible.some((x) => x.taskId === t.taskId && ((x.type === 'tool_result' && x.payload.category === 'verification' && x.payload.outcome !== 'inconclusive') || x.type === 'claim'))) continue;
      out.push({ taskId: t.taskId, createdId: e.id });
    }
  }
  return out;
}
