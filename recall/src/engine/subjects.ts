// SubjectIncident (owner ruling 2026-10-04): one story per claimed subject, pure over WorldState (sequence <= cursor).
// It gathers what monitors report piecemeal (D, E, J, AG … on one URL are one incident, not four) into:
//   first claim → repeats → checks → failure reports → corrections, the participants' roles, and the member findings.
// Every derived number is a count of linked records and carries those records.

import type { EventOf, RecallEvent } from '../model/types';
import type { WorldState } from './reconstruct';
import type { Finding } from './monitors/types';

export type Role = 'announcer' | 'repeater' | 'verifier' | 'corrector' | 'adopter-without-check';
export const ROLE_LABEL: Record<Role, string> = {
  announcer: 'Announcer', repeater: 'Repeater', verifier: 'Verifier', corrector: 'Corrector', 'adopter-without-check': 'Adopted without check',
};

export interface LinkedCount { n: number; records: RecallEvent[]; from?: number; to?: number }

export interface SubjectIncident {
  /** artifact@version */
  subject: string;
  artifact: string;
  firstClaim: EventOf<'claim'>;
  repeats: EventOf<'claim'>[];
  checks: EventOf<'tool_result'>[];
  failureReports: EventOf<'quote'>[];
  corrections: EventOf<'correction'>[];
  participants: { agent: string; role: Role }[];
  findings: Finding[];
  /** Distinct agents other than the announcer who claimed the subject later. */
  cascade: LinkedCount;
  /** Subject records (claims, failure reports) between the first claim and the first check of the subject. */
  toFirstCheck: LinkedCount & { reached: boolean };
  /** Subject records between the first claim and the first correction or failure report. */
  toCorrection: LinkedCount & { reached: boolean };
  /** Latest verdict state of the subject at the cursor. */
  standing: 'unchecked' | 'passing' | 'failing' | 'split';
}

const keyOf = (s?: { artifact: string; version: string }) => (s ? `${s.artifact}@${s.version}` : '');
const isCheck = (e: RecallEvent): e is EventOf<'tool_result'> => e.type === 'tool_result' && e.payload.category === 'verification';

/** The subject a finding is about: its claim's subject, else the first linked record that names one. */
export function subjectOfFinding(f: Finding, ws: WorldState): string {
  const c = ws.claims.get(f.claimId)?.subject;
  if (c) return keyOf(c);
  for (const l of f.evidence) {
    const e = ws.byId.get(l.eventId);
    if (e && (e.type === 'claim' || e.type === 'quote' || e.type === 'tool_result') && e.payload.subject) return keyOf(e.payload.subject);
  }
  return '';
}

export function subjectIncidents(ws: WorldState, findings: Finding[]): SubjectIncident[] {
  const claims = new Map<string, EventOf<'claim'>[]>();
  const checks = new Map<string, EventOf<'tool_result'>[]>();
  const quotes = new Map<string, EventOf<'quote'>[]>();
  const corrections = new Map<string, EventOf<'correction'>[]>();
  const push = <T>(m: Map<string, T[]>, k: string, v: T) => { if (k) m.set(k, [...(m.get(k) ?? []), v]); };
  for (const e of ws.visible) {
    if (e.type === 'claim') push(claims, keyOf(e.payload.subject), e);
    else if (isCheck(e)) push(checks, keyOf(e.payload.subject), e);
    else if (e.type === 'quote') push(quotes, keyOf(e.payload.subject), e);
    else if (e.type === 'correction') push(corrections, keyOf(ws.claims.get(e.payload.supersedes)?.subject), e);
  }
  const bySubject = new Map<string, Finding[]>();
  for (const f of findings) push(bySubject, subjectOfFinding(f, ws), f);

  const out: SubjectIncident[] = [];
  for (const [subject, cl] of claims) {
    const [firstClaim, ...repeats] = cl;
    const ck = checks.get(subject) ?? [];
    const fr = quotes.get(subject) ?? [];
    const co = corrections.get(subject) ?? [];
    const participants: { agent: string; role: Role }[] = [];
    const add = (agent: string, role: Role) => { if (!participants.some((p) => p.agent === agent && p.role === role)) participants.push({ agent, role }); };
    add(firstClaim.agentId, 'announcer');
    for (const r of repeats) {
      if (r.agentId !== firstClaim.agentId) add(r.agentId, 'repeater');
      if (r.agentId !== firstClaim.agentId && !ck.some((k) => k.agentId === r.agentId && k.sequence < r.sequence && k.payload.outcome !== 'inconclusive')) add(r.agentId, 'adopter-without-check');
    }
    for (const k of ck) if (k.payload.outcome !== 'inconclusive') add(k.agentId, 'verifier');
    for (const x of [...co, ...fr]) add(x.agentId, 'corrector');

    const repeaters = [...new Set(repeats.filter((r) => r.agentId !== firstClaim.agentId).map((r) => r.agentId))];
    const cascadeRecords = repeaters.map((a) => repeats.find((r) => r.agentId === a)!);
    const firstCheck = ck.find((k) => k.sequence > firstClaim.sequence && k.payload.outcome !== 'inconclusive');
    const firstCorr = [...co, ...fr].filter((x) => x.sequence > firstClaim.sequence).sort((a, b) => a.sequence - b.sequence)[0];
    const linked = (to?: number): RecallEvent[] => [...repeats, ...fr].filter((e) => e.sequence > firstClaim.sequence && (to === undefined || e.sequence < to)).sort((a, b) => a.sequence - b.sequence);
    const toCheck = linked(firstCheck?.sequence);
    const toCorr = linked(firstCorr?.sequence);
    const verdicts = ck.filter((k) => k.payload.outcome !== 'inconclusive');
    const last = verdicts[verdicts.length - 1];
    const lastByAgent = new Map<string, string>();
    for (const k of verdicts) lastByAgent.set(k.agentId, k.payload.outcome);
    const outcomes = new Set(lastByAgent.values());
    out.push({
      subject, artifact: firstClaim.payload.subject!.artifact, firstClaim, repeats, checks: ck, failureReports: fr, corrections: co, participants,
      findings: bySubject.get(subject) ?? [],
      cascade: { n: repeaters.length, records: cascadeRecords },
      toFirstCheck: { n: toCheck.length, records: toCheck, from: firstClaim.sequence, to: firstCheck?.sequence, reached: !!firstCheck },
      toCorrection: { n: toCorr.length, records: toCorr, from: firstClaim.sequence, to: firstCorr?.sequence, reached: !!firstCorr },
      standing: !last ? 'unchecked' : outcomes.size > 1 ? 'split' : last.payload.outcome === 'pass' ? 'passing' : 'failing',
    });
  }
  return out;
}

// ---------------------------------------------------------------- read-only remediation (owner ruling: observer only)

export interface RecheckSuggestion { agent: string; action: string; why: string; records: string[] }

/**
 * Which agent should recheck what, derived only from the incident and the window's records. Suggestions are for
 * a human operator to read; RECALL never posts them anywhere.
 */
export function recheckSuggestions(inc: SubjectIncident, ws: WorldState): RecheckSuggestion[] {
  const out: RecheckSuggestion[] = [];
  // A neutral verifier: the agent with the most verification records in the window who is not the announcer.
  const tally = new Map<string, number>();
  for (const e of ws.visible) if (isCheck(e) && e.payload.outcome !== 'inconclusive') tally.set(e.agentId, (tally.get(e.agentId) ?? 0) + 1);
  const failing = inc.checks.filter((k) => k.payload.outcome === 'fail').map((k) => k.agentId);
  const neutral = [...tally.entries()].filter(([a]) => a !== inc.firstClaim.agentId && !failing.includes(a)).sort((a, b) => b[1] - a[1])[0]?.[0];
  const label = inc.artifact.replace(/^(repo|file|pr|pages|proc|tests):/, '');
  if (inc.standing === 'unchecked') {
    const who = neutral ?? inc.firstClaim.agentId;
    out.push({ agent: who, action: `Run one verification of ${label}.`, why: `No check of the subject exists; ${inc.repeats.length + 1} claim${inc.repeats.length ? 's' : ''} rest on none.`, records: [inc.firstClaim.id] });
  }
  if (inc.standing === 'split' && neutral) {
    out.push({ agent: neutral, action: `Run a tie-breaking check of ${label}.`, why: 'Agents\' latest checks disagree.', records: inc.checks.slice(-3).map((k) => k.id) });
  }
  if (inc.standing === 'failing') {
    out.push({ agent: inc.firstClaim.agentId, action: `Correct the claim about ${label}, or post a passing record.`, why: 'The latest verdict of the subject is a fail.', records: [inc.firstClaim.id, inc.checks[inc.checks.length - 1].id] });
  }
  for (const p of inc.participants.filter((x) => x.role === 'adopter-without-check')) {
    const r = inc.repeats.find((x) => x.agentId === p.agent);
    out.push({ agent: p.agent, action: `Check ${label} yourself, or cite the record you relied on.`, why: 'Repeated the claim without an own check.', records: r ? [r.id] : [] });
  }
  if (inc.failureReports.length && inc.repeats.some((r) => r.sequence > inc.failureReports[0].sequence && r.agentId !== inc.failureReports[0].agentId)) {
    out.push({ agent: inc.failureReports[0].agentId, action: `Re-share the failing record for ${label} in the room.`, why: 'Claims continued after the failure report.', records: [inc.failureReports[0].id] });
  }
  return out.slice(0, 6);
}

// ---------------------------------------------------------------- swarm rates (count pairs, never bare percentages)

export interface SwarmRates {
  repeatsWithoutCheck: { n: number; of: number; records: string[] };
  checksPerAgent: { agent: string; n: number }[];
  consensusWithoutCheck: { n: number; subjects: string[] };
  duplicateGoals: { n: number; findings: string[] };
  directiveUptake: { n: number; of: number; findings: string[] };
  correctionReach: { n: number; of: number; records: string[] };
  singlePoints: { agent: string; subjects: string[] }[];
}

export const SPOF_MIN_SUBJECTS = 3;
export const CASCADE_ALERT = 3;

export function swarmRates(ws: WorldState, findings: Finding[], incidents = subjectIncidents(ws, findings)): SwarmRates {
  let repeatsN = 0; let repeatsOf = 0; const repeatRecs: string[] = [];
  let reachN = 0; let reachOf = 0; const reachRecs: string[] = [];
  const consensus: string[] = [];
  const soleVerifier = new Map<string, string[]>();
  for (const inc of incidents) {
    for (const r of inc.repeats) {
      if (r.agentId === inc.firstClaim.agentId) continue;
      repeatsOf++;
      if (!inc.checks.some((k) => k.agentId === r.agentId && k.sequence < r.sequence && k.payload.outcome !== 'inconclusive')) { repeatsN++; repeatRecs.push(r.id); }
    }
    const claimants = new Set([inc.firstClaim, ...inc.repeats].map((c) => c.agentId));
    if (claimants.size >= 3 && !inc.checks.some((k) => k.payload.outcome !== 'inconclusive')) consensus.push(inc.subject);
    for (const x of [...inc.corrections, ...inc.failureReports]) {
      const audience = new Set([inc.firstClaim, ...inc.repeats].filter((c) => c.sequence < x.sequence && c.agentId !== x.agentId).map((c) => c.agentId));
      const after = new Set([inc.firstClaim, ...inc.repeats].filter((c) => c.sequence > x.sequence && audience.has(c.agentId)).map((c) => c.agentId));
      reachOf += audience.size; reachN += after.size;
      if (after.size) reachRecs.push(x.id);
    }
    const verifiers = [...new Set(inc.checks.filter((k) => k.payload.outcome !== 'inconclusive').map((k) => k.agentId))];
    if (verifiers.length === 1) soleVerifier.set(verifiers[0], [...(soleVerifier.get(verifiers[0]) ?? []), inc.subject]);
  }
  const perAgent = new Map<string, number>();
  for (const e of ws.visible) if (isCheck(e) && e.payload.outcome !== 'inconclusive') perAgent.set(e.agentId, (perAgent.get(e.agentId) ?? 0) + 1);
  const ac = findings.filter((f) => f.monitor === 'AC');
  const v = findings.filter((f) => f.monitor === 'V');
  return {
    repeatsWithoutCheck: { n: repeatsN, of: repeatsOf, records: repeatRecs },
    checksPerAgent: [...perAgent.entries()].map(([agent, n]) => ({ agent, n })).sort((a, b) => b.n - a.n),
    consensusWithoutCheck: { n: consensus.length, subjects: consensus },
    duplicateGoals: { n: v.length, findings: v.map((f) => f.id) },
    directiveUptake: { n: ac.filter((f) => f.state === 'resolved').length, of: ac.length, findings: ac.map((f) => f.id) },
    correctionReach: { n: reachN, of: reachOf, records: reachRecs },
    singlePoints: [...soleVerifier.entries()].filter(([, s]) => s.length >= SPOF_MIN_SUBJECTS).map(([agent, subjects]) => ({ agent, subjects })),
  };
}

/** Per-agent role counts across subjects; every count carries its subjects. */
export function agentRoles(incidents: SubjectIncident[]): Map<string, Map<Role, string[]>> {
  const out = new Map<string, Map<Role, string[]>>();
  for (const inc of incidents) for (const p of inc.participants) {
    const m = out.get(p.agent) ?? new Map<Role, string[]>();
    m.set(p.role, [...(m.get(p.role) ?? []), inc.subject]);
    out.set(p.agent, m);
  }
  return out;
}
