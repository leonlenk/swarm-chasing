// The life of one claim, reconstructed from explicit references only:
// who introduced it, which records used it, what corrected it, who acknowledged the
// correction, and who kept using it afterwards.

import type { EventOf, RecallEvent } from '../model/types';
import type { ClaimState, WorldState } from './reconstruct';

export type StepKind = 'introduced' | 'evidence' | 'used' | 'repeated' | 'checked' | 'correction' | 'acknowledged' | 'used_after' | 'changed';

export interface LineageStep {
  kind: StepKind;
  eventId: string;
  agentId: string;
  sequence: number;
  timestamp: string;
  taskId: string | null;
  text: string;
  /** Steps this one explicitly follows from (drawn as connectors). */
  from: string[];
  /** True when linked by identical claim subject rather than an explicit reference. */
  bySubject?: boolean;
}

export interface AgentOutcome {
  agentId: string;
  /** Acknowledgement of the correction, if recorded. */
  ackEventId?: string;
  /** First later record showing the agent acting on something other than the withdrawn claim. */
  changedEventId?: string;
  /** Actions after the correction that still cite the claim. */
  staleEventIds: string[];
}

export interface Lineage {
  claim: ClaimState;
  steps: LineageStep[];
  correction?: EventOf<'correction'>;
  /** Agents other than the corrector who used the claim before the correction. */
  audience: AgentOutcome[];
  firstSeen: string;
  agentsReached: string[];
  /** Same-subject repeats beyond the display cap. */
  hiddenRepeats: number;
}

const step = (kind: StepKind, e: RecallEvent, from: string[] = []): LineageStep => ({
  kind, eventId: e.id, agentId: e.agentId, sequence: e.sequence, timestamp: e.timestamp, taskId: e.taskId, text: e.text, from,
});

export function claimLineage(ws: WorldState, claimId: string): Lineage | null {
  const claim = ws.claims.get(claimId);
  if (!claim) return null;
  const intro = ws.byId.get(claim.eventId)!;
  const steps: LineageStep[] = [];

  for (const r of intro.evidenceRefs) {
    const e = ws.byId.get(r);
    if (e?.type === 'tool_result') steps.push(step('evidence', e));
  }
  steps.push(step('introduced', intro, steps.map((s) => s.eventId)));

  const correction = ws.visible.find(
    (e): e is EventOf<'correction'> => e.type === 'correction' && e.payload.supersedes === claimId,
  );
  const cites = (e: RecallEvent) => e.type === 'action' && e.payload.referencesClaims.includes(claimId);

  // Same exact subject (artifact + version): later claims are retellings, later verifications are checks.
  const subj = claim.subject;
  const same = (a?: { artifact: string; version: string }) => !!subj && !!a && a.artifact === subj.artifact && a.version === subj.version;
  const MAX_REPEATS = 10;
  let repeats = 0;
  let hiddenRepeats = 0;
  for (const e of ws.visible) {
    if (e.sequence <= intro.sequence) continue;
    if (e.type === 'claim' && e.id !== intro.id && same(e.payload.subject)) {
      if (repeats++ < MAX_REPEATS) steps.push({ ...step('repeated', e, [intro.id]), bySubject: true });
      else hiddenRepeats++;
    } else if (e.type === 'tool_result' && e.payload.category === 'verification' && same(e.payload.subject)) {
      steps.push({ ...step('checked', e, [intro.id]), bySubject: true });
    }
  }

  for (const e of ws.visible) {
    if (!cites(e)) continue;
    const after = correction && e.sequence > correction.sequence;
    steps.push(step(after ? 'used_after' : 'used', e, [intro.id]));
  }

  const audienceIds = [...new Set(steps.filter((s) => s.kind === 'used').map((s) => s.agentId))]
    .filter((a) => a !== correction?.agentId);
  const audience: AgentOutcome[] = audienceIds.map((agentId) => ({ agentId, staleEventIds: [] }));

  if (correction) {
    steps.push(step('correction', correction, [intro.id]));
    for (const e of ws.visible) {
      if (e.type === 'acknowledgement' && e.payload.acknowledges === correction.id) {
        steps.push(step('acknowledged', e, [correction.id]));
      }
    }
    for (const o of audience) {
      const later = ws.visible.filter((e) => e.agentId === o.agentId && e.sequence > correction.sequence);
      o.ackEventId = later.find((e) => e.type === 'acknowledgement' && e.payload.acknowledges === correction.id)?.id;
      o.staleEventIds = later.filter(cites).map((e) => e.id);
      const changed = later.find((e) =>
        (e.type === 'acknowledgement' && e.payload.acknowledges === correction.id && !!e.statusAfter) ||
        (e.type === 'action' && !e.payload.referencesClaims.includes(claimId)));
      o.changedEventId = changed?.id;
      if (changed && changed.type === 'action') steps.push(step('changed', changed, o.ackEventId ? [o.ackEventId] : [correction.id]));
    }
  }

  steps.sort((a, b) => a.sequence - b.sequence);
  const agentsReached = [...new Set(steps.filter((s) => s.kind !== 'evidence' && s.kind !== 'checked').map((s) => s.agentId))];
  return { claim, steps, correction, audience, firstSeen: intro.timestamp, agentsReached, hiddenRepeats };
}

/** Claims worth tracing first: superseded or contradicted, then most-cited. */
export function rankedClaims(ws: WorldState): ClaimState[] {
  const citeCount = new Map<string, number>();
  for (const e of ws.visible) {
    if (e.type === 'action') for (const c of e.payload.referencesClaims) citeCount.set(c, (citeCount.get(c) ?? 0) + 1);
  }
  const rank = (c: ClaimState) => (c.standing === 'superseded' ? 0 : c.standing === 'contradicted' ? 1 : c.standing === 'unknown' ? 3 : 2);
  return [...ws.claims.values()].sort((a, b) => rank(a) - rank(b) || (citeCount.get(b.id) ?? 0) - (citeCount.get(a.id) ?? 0));
}
