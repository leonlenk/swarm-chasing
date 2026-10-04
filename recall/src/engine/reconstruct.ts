// Rebuilds world state from the events visible at a timeline position.
// Pure: takes the prefix of the log, never consults later events.

import type {
  ArtifactRef, EventOf, EvidenceStatus, Provenance, RecallEvent, TaskStatus,
} from '../model/types';

export interface StatusChange {
  status: TaskStatus;
  eventId: string;
  sequence: number;
  timestamp: string;
  agentId: string;
}

export interface TaskState {
  id: string;
  title: string;
  owner: string;
  createdBy: string;
  reportedStatus: TaskStatus;
  evidenceStatus: EvidenceStatus;
  evidenceReason: string;
  /** Event that most recently set the reported status. */
  basisEventId: string | null;
  history: StatusChange[];
  eventIds: string[];
  claimsUsed: string[];
  lastTouchedSeq: number;
}

export interface Edge {
  id: string;
  from: string;
  to: string;
  provenance: Provenance;
  eventId: string;
}

export type ClaimStanding = 'supported' | 'contradicted' | 'superseded' | 'unknown';

export interface ClaimState {
  id: string;
  eventId: string;
  agentId: string;
  taskId: string | null;
  text: string;
  subject?: ArtifactRef;
  standing: ClaimStanding;
  reason: string;
  supersededBy?: string;
  /** Tool result ids that bear on this claim at this time. */
  evidence: string[];
}

/** Why a cited record is or isn't in the analysis at this cursor. */
export type RefStatus = 'available' | 'withheld' | 'missing' | 'future';

/** What the analysis is allowed to see. Withholding happens here, before reconstruction. */
export interface AnalysisInput {
  events: RecallEvent[];
  /** Record ids deliberately removed from the analysis (evidence visibility experiment). */
  withheld: ReadonlySet<string>;
  /** Display names, so monitors can phrase findings from WorldState alone. */
  agents?: ReadonlyArray<{ id: string; name: string }>;
}

export interface WorldState {
  cursor: number;
  /** Ids withheld from this analysis whose time has already passed. Their content is not retained. */
  withheld: string[];
  refStatus: (id: string) => RefStatus;
  /** Agent display name (falls back to the id). */
  agentName: (id: string) => string;
  visible: RecallEvent[];
  byId: Map<string, RecallEvent>;
  tasks: Map<string, TaskState>;
  edges: Edge[];
  claims: Map<string, ClaimState>;
}

const sameSubject = (a?: ArtifactRef, b?: ArtifactRef) =>
  !!a && !!b && a.artifact === b.artifact && a.version === b.version;

const fmtSubject = (s?: ArtifactRef) => (s ? `${s.artifact}@${s.version}` : 'unspecified artifact');

function evaluateClaim(claim: EventOf<'claim'>, visible: RecallEvent[], refStatus: (id: string) => RefStatus): ClaimState {
  const base = {
    id: claim.payload.claimId,
    eventId: claim.id,
    agentId: claim.agentId,
    taskId: claim.taskId,
    text: claim.text,
    subject: claim.payload.subject,
  };
  const correction = visible.find(
    (e): e is EventOf<'correction'> => e.type === 'correction' && e.payload.supersedes === claim.payload.claimId,
  );
  const results = visible.filter(
    (e): e is EventOf<'tool_result'> => e.type === 'tool_result' && e.payload.category === 'verification' &&
      sameSubject(e.payload.subject, claim.payload.subject),
  );
  const evidence = results.map((r) => r.id);
  const passes = results.filter((r) => r.payload.outcome === 'pass');
  const fails = results.filter((r) => r.payload.outcome === 'fail');

  if (correction) {
    return { ...base, evidence, standing: 'superseded', supersededBy: correction.id,
      reason: `Withdrawn by correction ${correction.id} (#${correction.sequence}).` };
  }
  if (claim.payload.asserts === 'verification_passed' || claim.payload.asserts === 'complete') {
    if (passes.length) {
      return { ...base, evidence, standing: 'supported',
        reason: `Passing verification ${passes.map((p) => p.payload.runId).join(', ')} for ${fmtSubject(claim.payload.subject)}.` };
    }
    if (fails.length) {
      return { ...base, evidence, standing: 'contradicted',
        reason: `Only failing verification on record for ${fmtSubject(claim.payload.subject)}: ${fails.map((f) => f.payload.runId).join(', ')}.` };
    }
  }
  const withheldRefs = claim.evidenceRefs.filter((r) => refStatus(r) === 'withheld');
  const missingRefs = claim.evidenceRefs.filter((r) => refStatus(r) === 'missing');
  if (withheldRefs.length || missingRefs.length) {
    return { ...base, evidence, standing: 'unknown',
      reason: `Insufficient evidence: the claim cites ${[
        ...withheldRefs.map((r) => `${r} (withheld from this analysis)`),
        ...missingRefs.map((r) => `${r} (not present in the source dataset)`),
      ].join(', ')}, and no other verification of ${fmtSubject(claim.payload.subject)} is visible.` };
  }
  return { ...base, evidence, standing: 'unknown',
    reason: claim.payload.subject
      ? `No verification on record for ${fmtSubject(claim.payload.subject)}.`
      : 'Claim names no artifact version, so it cannot be checked against tool results.' };
}

function standingToEvidence(s: ClaimStanding): EvidenceStatus {
  return s === 'supported' ? 'supported' : s === 'unknown' ? 'unknown' : 'contradicted';
}

function evidenceForTask(task: TaskState, ws: Omit<WorldState, 'tasks'>): [EvidenceStatus, string] {
  if (!task.basisEventId) return ['unknown', 'No status-bearing evidence recorded yet.'];
  const basis = ws.byId.get(task.basisEventId)!;
  switch (basis.type) {
    case 'tool_result':
      return ['supported', `Status set directly by tool output ${basis.payload.runId} (${basis.payload.outcome}).`];
    case 'claim': {
      const c = ws.claims.get(basis.payload.claimId)!;
      return [standingToEvidence(c.standing), `Status rests on claim ${c.id}: ${c.reason}`];
    }
    case 'action': {
      const refs = basis.payload.referencesClaims.map((id) => ws.claims.get(id)).filter(Boolean) as ClaimState[];
      const bad = refs.find((c) => c.standing === 'superseded' || c.standing === 'contradicted');
      if (bad) return ['contradicted', `Action cites claim ${bad.id}, which is ${bad.standing}. ${bad.reason}`];
      if (refs.length && refs.every((c) => c.standing === 'supported')) {
        return ['supported', `Action cites ${refs.map((c) => c.id).join(', ')}, backed by passing verification.`];
      }
      return ['unknown', 'Action cites claims with no verification on record.'];
    }
    case 'correction': {
      const fail = basis.evidenceRefs.map((id) => ws.byId.get(id)).find(
        (e) => e?.type === 'tool_result' && e.payload.outcome === 'fail',
      );
      if (fail) return ['supported', `Correction cites failing tool output ${fail.id}.`];
      const gone = basis.evidenceRefs.filter((id) => ws.refStatus(id) === 'withheld' || ws.refStatus(id) === 'missing');
      return ['unknown', gone.length
        ? `Insufficient evidence: correction cites ${gone.map((id) => `${id} (${ws.refStatus(id) === 'withheld' ? 'withheld from this analysis' : 'not in source dataset'})`).join(', ')}.`
        : 'Correction cites no tool output.'];
    }
    case 'acknowledgement':
      return ['supported', `Agent acknowledged correction ${basis.payload.acknowledges} and changed status accordingly.`];
    default: {
      if (basis.provenance === 'observed') {
        // A system-recorded status (e.g. a session ending) says nothing about whether the work succeeded.
        // Roll up the claims the owner made inside this task: those are the agent's account of the work.
        const own = task.claimsUsed.map((id) => ws.claims.get(id)).filter((c): c is ClaimState => !!c && c.agentId === task.owner);
        const bad = own.find((c) => c.standing === 'contradicted');
        if (bad) return ['contradicted', `Session status is a system record, but claim ${bad.id} made in it is contradicted: ${bad.reason}`];
        const open = own.filter((c) => c.standing === 'unknown');
        if (open.length) return ['unknown', `Session status is a system record; ${open.length} claim${open.length > 1 ? 's' : ''} made in it ha${open.length > 1 ? 've' : 's'} no verification on record (${open.map((c) => c.id).slice(0, 3).join(', ')}).`];
        return ['supported', own.length ? `Status is an observed system record, and claims made in it are backed by checks.` : 'Status comes from an observed system record (not an agent statement).'];
      }
      const tool = basis.evidenceRefs.map((id) => ws.byId.get(id)).find((e) => e?.type === 'tool_result');
      if (tool && tool.type === 'tool_result' && tool.payload.outcome === 'pass') {
        return ['supported', `Status update cites passing tool output ${tool.payload.runId}.`];
      }
      return ['unknown', 'Status was declared without verifiable evidence for this task.'];
    }
  }
}

export function reconstruct(input: AnalysisInput, cursor: number): WorldState {
  const { events, withheld: hidden } = input;
  const names = new Map((input.agents ?? []).map((a) => [a.id, a.name] as const));
  const agentName = (id: string) => names.get(id) ?? id;
  const visible = events.filter((e) => e.sequence <= cursor && !hidden.has(e.id));
  const byId = new Map(visible.map((e) => [e.id, e] as const));
  // Only sequence numbers are kept for the full log, so withheld/future content can't leak.
  const seqOf = new Map(events.map((e) => [e.id, e.sequence] as const));
  const withheld = [...hidden].filter((id) => (seqOf.get(id) ?? Infinity) <= cursor);
  const refStatus = (id: string): RefStatus => {
    if (byId.has(id)) return 'available';
    const seq = seqOf.get(id);
    if (seq === undefined) return 'missing';
    if (seq > cursor) return 'future';
    return hidden.has(id) ? 'withheld' : 'missing';
  };
  const tasks = new Map<string, TaskState>();
  const edges: Edge[] = [];

  const addEdge = (from: string, to: string, provenance: Provenance, eventId: string) => {
    if (!edges.some((x) => x.from === from && x.to === to)) {
      edges.push({ id: `${from}->${to}`, from, to, provenance, eventId });
    }
  };

  for (const e of visible) {
    if (e.type === 'task_created') {
      for (const spec of e.payload.tasks) {
        tasks.set(spec.taskId, {
          id: spec.taskId, title: spec.title, owner: spec.owner, createdBy: e.agentId,
          reportedStatus: 'todo', evidenceStatus: 'unknown', evidenceReason: '',
          basisEventId: null, history: [{ status: 'todo', eventId: e.id, sequence: e.sequence, timestamp: e.timestamp, agentId: e.agentId }],
          eventIds: [e.id], claimsUsed: [], lastTouchedSeq: e.sequence,
        });
        for (const dep of spec.dependsOn ?? []) addEdge(dep, spec.taskId, e.provenance, e.id);
      }
      continue;
    }
    if (e.type === 'dependency_created') addEdge(e.payload.from, e.payload.to, e.provenance, e.id);

    const t = e.taskId ? tasks.get(e.taskId) : undefined;
    if (!t) continue;
    t.eventIds.push(e.id);
    t.lastTouchedSeq = e.sequence;
    if (e.type === 'task_assigned') t.owner = e.payload.assignee;
    if (e.type === 'action') {
      for (const c of e.payload.referencesClaims) if (!t.claimsUsed.includes(c)) t.claimsUsed.push(c);
    }
    if (e.type === 'claim' && !t.claimsUsed.includes(e.payload.claimId)) t.claimsUsed.push(e.payload.claimId);
    const next = e.type === 'status_updated' ? e.payload.status : e.statusAfter;
    if (next) {
      t.basisEventId = e.id;
      if (next !== t.reportedStatus) {
        t.history.push({ status: next, eventId: e.id, sequence: e.sequence, timestamp: e.timestamp, agentId: e.agentId });
      }
      t.reportedStatus = next;
    }
  }

  const claims = new Map<string, ClaimState>();
  for (const e of visible) if (e.type === 'claim') claims.set(e.payload.claimId, evaluateClaim(e, visible, refStatus));

  const partial = { cursor, withheld, refStatus, agentName, visible, byId, edges, claims };
  for (const t of tasks.values()) {
    const [status, reason] = evidenceForTask(t, partial);
    t.evidenceStatus = status;
    t.evidenceReason = reason;
  }
  return { ...partial, tasks };
}

/** Tasks reachable downstream of `taskId` via dependency edges known at this time. */
export function dependencyReach(ws: WorldState, taskId: string): string[] {
  const out: string[] = [];
  const queue = [taskId];
  while (queue.length) {
    const cur = queue.shift()!;
    for (const e of ws.edges) {
      if (e.from === cur && !out.includes(e.to) && e.to !== taskId) {
        out.push(e.to);
        queue.push(e.to);
      }
    }
  }
  return out;
}

export interface ClaimImpact {
  claimId: string;
  /** Tasks with an event that explicitly states or cites the claim. */
  referencing: { taskId: string; eventIds: string[] }[];
  /** Downstream of referencing tasks, excluding them. Structural only. */
  reach: string[];
}

export function claimImpact(ws: WorldState, claimId: string): ClaimImpact {
  const byTask = new Map<string, string[]>();
  for (const e of ws.visible) {
    if (!e.taskId) continue;
    const cites = (e.type === 'claim' && e.payload.claimId === claimId) ||
      (e.type === 'action' && e.payload.referencesClaims.includes(claimId));
    if (cites) byTask.set(e.taskId, [...(byTask.get(e.taskId) ?? []), e.id]);
  }
  const referencing = [...byTask].map(([taskId, eventIds]) => ({ taskId, eventIds }));
  const refIds = new Set(byTask.keys());
  const reach = [...new Set(referencing.flatMap((r) => dependencyReach(ws, r.taskId)))].filter((t) => !refIds.has(t));
  return { claimId, referencing, reach };
}

export interface CorrectionReport {
  correction: EventOf<'correction'>;
  claimId: string;
  /** One row per agent other than the author. ackEventId null = no acknowledgement observed. */
  agents: { agentId: string; addressed: boolean; ackEventId: string | null; ackSeq?: number }[];
  /** Actions after the correction (up to the cursor) still citing the superseded claim. */
  staleActions: { eventId: string; agentId: string; taskId: string | null; sequence: number; ackedBefore: boolean }[];
}

export function correctionReport(ws: WorldState, correctionId: string, agentIds: string[]): CorrectionReport | null {
  const c = ws.byId.get(correctionId);
  if (!c || c.type !== 'correction') return null;
  const acks = ws.visible.filter(
    (e): e is EventOf<'acknowledgement'> => e.type === 'acknowledgement' && e.payload.acknowledges === c.id,
  );
  const ackOf = (agentId: string) => acks.find((a) => a.agentId === agentId);
  const agents = agentIds.filter((a) => a !== c.agentId).map((agentId) => {
    const a = ackOf(agentId);
    return { agentId, addressed: !!c.payload.addressedTo?.includes(agentId), ackEventId: a?.id ?? null, ackSeq: a?.sequence };
  });
  const staleActions = ws.visible
    .filter((e): e is EventOf<'action'> =>
      e.type === 'action' && e.sequence > c.sequence && e.payload.referencesClaims.includes(c.payload.supersedes))
    .map((e) => ({
      eventId: e.id, agentId: e.agentId, taskId: e.taskId, sequence: e.sequence,
      ackedBefore: acks.some((a) => a.agentId === e.agentId && a.sequence < e.sequence),
    }));
  return { correction: c, claimId: c.payload.supersedes, agents, staleActions };
}
