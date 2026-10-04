// Deterministic monitors. They read only the reconstructed world at the cursor:
// no confidence scores, no causality from timestamp proximity, and no assumption
// that an agent saw a correction unless an acknowledgement is recorded.

import type { EventOf, RecallEvent } from '../model/types';
import { dependencyReach, type WorldState } from './reconstruct';

export type MonitorId = 'unsupported_completion' | 'superseded_claim_reused';

export interface EvidenceLink {
  eventId: string;
  role: string;
}

export interface Finding {
  id: string;
  monitor: MonitorId;
  title: string;
  /** One plain-language sentence, used as the main-screen headline. */
  summary: string;
  explanation: string;
  detectedAt: number;
  detectedEventId: string;
  taskId: string;
  claimId: string;
  agentId: string;
  evidence: EvidenceLink[];
  reach: string[];
  missing: string[];
  /** insufficient = the analysis lacks the evidence to confirm or clear the finding. */
  state: 'active' | 'resolved' | 'insufficient';
  resolution?: { eventId: string; text: string };
}

export const MONITOR_LABEL: Record<MonitorId, string> = {
  unsupported_completion: 'Unsupported completion',
  superseded_claim_reused: 'Superseded claim reused',
};

const subj = (s?: { artifact: string; version: string }) =>
  (!s ? '?' : s.version === 'live' ? s.artifact : `${s.artifact}@${s.version}`);
const isLive = (s?: { version: string }) => s?.version === 'live';
/** What the claim asserted, in plain words. */
const asserted = (c: EventOf<'claim'>) => (isLive(c.payload.subject)
  ? `${subj(c.payload.subject)} is live`
  : c.payload.asserts === 'verification_passed' ? `verification passed for ${subj(c.payload.subject)}` : `${subj(c.payload.subject)} is complete`);
const doneClause = (c: EventOf<'claim'>) => (c.statusAfter === 'done' ? ' and marked the task done' : '');
/** The kind of check, in plain words. */
const checkNoun = (s?: { version: string }) => (isLive(s) ? 'recorded check of that URL' : 'verification of that exact version');

function unsupportedCompletion(ws: WorldState, name: (id: string) => string): Finding[] {
  const out: Finding[] = [];
  for (const claim of ws.visible) {
    if (claim.type !== 'claim' || !claim.payload.subject || !claim.taskId) continue;
    const s = claim.payload.subject;
    const before = ws.visible.filter(
      (e): e is EventOf<'tool_result'> =>
        e.type === 'tool_result' && e.payload.category === 'verification' && e.sequence < claim.sequence &&
        e.payload.subject?.artifact === s.artifact && e.payload.subject?.version === s.version,
    );
    const correction = ws.visible.find(
      (e): e is EventOf<'correction'> => e.type === 'correction' && e.payload.supersedes === claim.payload.claimId,
    );

    if (!before.length) {
      // No visible verification either way. If the claim cites records this analysis cannot see,
      // report insufficient evidence rather than clearing it.
      const gone = claim.evidenceRefs.filter((r) => ws.refStatus(r) === 'withheld' || ws.refStatus(r) === 'missing');
      if (!gone.length) continue;
      const why = (r: string) => ws.refStatus(r) === 'withheld'
        ? 'withheld from this analysis by the evidence visibility experiment (present in the source)'
        : 'not present in the source dataset';
      out.push({
        id: `A:${claim.id}`,
        monitor: 'unsupported_completion',
        title: `Cannot verify: ${asserted(claim)}`,
        summary: `${name(claim.agentId)}'s claim that ${asserted(claim)} cannot be confirmed or cleared: the run it cites is ${ws.refStatus(gone[0]) === 'withheld' ? 'withheld from this analysis' : 'missing from the dataset'}.`,
        explanation:
          `${name(claim.agentId)} claimed ${asserted(claim)}${doneClause(claim)}. ` +
          `The claim cites ${gone.join(', ')}, which ${gone.length > 1 ? 'are' : 'is'} unavailable to this analysis, and no other verification of ${subj(s)} is visible. ` +
          `RECALL can neither confirm nor clear this completion.` +
          (correction ? ` A later correction (#${correction.sequence}) withdraws the claim, but the run it cites remains unavailable.` : ''),
        detectedAt: claim.sequence,
        detectedEventId: claim.id,
        taskId: claim.taskId,
        claimId: claim.payload.claimId,
        agentId: claim.agentId,
        evidence: [
          { eventId: claim.id, role: `Completion claim ${claim.payload.claimId}` },
          ...gone.map((r) => ({ eventId: r, role: 'Cited verification' })),
          ...(correction ? [{ eventId: correction.id, role: 'Correction (declared; does not substitute for the run)' }] : []),
        ],
        reach: dependencyReach(ws, claim.taskId),
        missing: gone.map((r) => `${r} — ${why(r)}.`),
        state: 'insufficient',
      });
      continue;
    }
    const fail = before.find((r) => r.payload.outcome === 'fail');
    const passBefore = before.some((r) => r.payload.outcome === 'pass');
    if (!fail || passBefore) continue;

    const laterPass = ws.visible.find(
      (e): e is EventOf<'tool_result'> =>
        e.type === 'tool_result' && e.payload.category === 'verification' &&
        e.sequence > claim.sequence && e.payload.outcome === 'pass' &&
        e.payload.subject?.artifact === s.artifact && e.payload.subject?.version === s.version,
    );
    const citesFail = claim.evidenceRefs.includes(fail.id);
    const resolver = [laterPass, correction].filter(Boolean).sort((a, b) => a!.sequence - b!.sequence)[0];

    out.push({
      id: `A:${claim.id}`,
      monitor: 'unsupported_completion',
      title: isLive(s) ? `Claimed live after a failed check: ${subj(s)}` : `${claim.payload.claimId} reports ${subj(s)} passed after it failed`,
      summary: resolver
        ? `${name(claim.agentId)}'s claim that ${asserted(claim)} contradicted ${fail.payload.runId}; it was ${resolver.type === 'correction' ? `withdrawn at #${resolver.sequence}` : `superseded by a passing run at #${resolver.sequence}`}.`
        : `${name(claim.agentId)} claimed ${asserted(claim)}${doneClause(claim)}, but the only ${checkNoun(s)} (${fail.payload.runId}) failed` +
          (dependencyReach(ws, claim.taskId).length ? `; ${dependencyReach(ws, claim.taskId).length} downstream tasks depend on it.` : '.'),
      explanation:
        `${name(claim.agentId)} claimed ${asserted(claim)}${doneClause(claim)}. ` +
        `The only ${isLive(s) ? 'recorded check of that URL' : 'verification on record for that exact version'}, ${fail.payload.runId}, failed ` +
        `${citesFail ? '— and the claim cites that same failing run as its evidence.' : '.'} ` +
        `No passing ${isLive(s) ? 'check' : 'run'} of ${subj(s)} existed when the claim was made.`,
      detectedAt: claim.sequence,
      detectedEventId: claim.id,
      taskId: claim.taskId,
      claimId: claim.payload.claimId,
      agentId: claim.agentId,
      evidence: [
        { eventId: fail.id, role: `Failed verification · ${fail.payload.runId}` },
        { eventId: claim.id, role: `Completion claim ${claim.payload.claimId}` },
        ...(resolver ? [{ eventId: resolver.id, role: resolver.type === 'correction' ? 'Correction' : 'Passing re-run' }] : []),
      ],
      reach: dependencyReach(ws, claim.taskId),
      missing: [
        `No passing tool result for ${subj(s)}${resolver?.type === 'tool_result' ? ' before the claim' : ''}.`,
        isLive(s) ? 'No record of a later check (e.g. in a browser session) that the agent may have relied on.' : 'No record of why the agent read the run as passing.',
      ],
      state: resolver ? 'resolved' : 'active',
      resolution: resolver
        ? { eventId: resolver.id, text: resolver.type === 'correction'
            ? `Claim withdrawn by ${name(resolver.agentId)} at #${resolver.sequence}.`
            : `Passing run for ${subj(s)} recorded at #${resolver.sequence}.` }
        : undefined,
    });
  }
  return out;
}

function supersededReuse(ws: WorldState, name: (id: string) => string): Finding[] {
  const out: Finding[] = [];
  const corrections = ws.visible.filter((e): e is EventOf<'correction'> => e.type === 'correction');
  for (const act of ws.visible) {
    if (act.type !== 'action' || !act.taskId) continue;
    for (const claimId of act.payload.referencesClaims) {
      const corr = corrections.find((c) => c.payload.supersedes === claimId && c.sequence < act.sequence);
      if (!corr) continue;
      const ackedBefore = ws.visible.find(
        (e) => e.type === 'acknowledgement' && e.agentId === act.agentId &&
          e.payload.acknowledges === corr.id && e.sequence < act.sequence,
      );
      const otherAcks = ws.visible.filter(
        (e) => e.type === 'acknowledgement' && e.payload.acknowledges === corr.id &&
          e.agentId !== act.agentId && e.sequence < act.sequence,
      );
      const resolver: RecallEvent | undefined = ws.visible.find(
        (e) => e.sequence > act.sequence && e.agentId === act.agentId &&
          ((e.type === 'acknowledgement' && e.payload.acknowledges === corr.id) ||
           (e.type === 'action' && e.taskId === act.taskId &&
            !e.payload.referencesClaims.some((c) => corrections.some((k) => k.payload.supersedes === c)))),
      );
      const addressed = corr.payload.addressedTo?.includes(act.agentId);
      out.push({
        id: `B:${act.id}:${claimId}`,
        monitor: 'superseded_claim_reused',
        title: `${name(act.agentId)} acted on withdrawn claim ${claimId}`,
        summary:
          `${name(act.agentId)} performed "${act.payload.action}" citing ${claimId}, which ${name(corr.agentId)} withdrew at #${corr.sequence}; ` +
          (ackedBefore ? `${name(act.agentId)} had acknowledged that correction` : `no acknowledgement from ${name(act.agentId)} was observed before acting`) +
          (resolver ? ` (recovered at #${resolver.sequence}).` : '.'),
        explanation:
          `${name(act.agentId)} performed "${act.payload.action}" and explicitly cited ${claimId}. ` +
          `${name(corr.agentId)} had already withdrawn ${claimId} at #${corr.sequence}. ` +
          (ackedBefore
            ? `${name(act.agentId)} had acknowledged that correction at #${ackedBefore.sequence}, yet still cited the old claim.`
            : `No acknowledgement from ${name(act.agentId)} is recorded before this action, so RECALL does not assume they saw the correction.`) +
          (otherAcks.length ? ` Meanwhile ${otherAcks.map((a) => name(a.agentId)).join(', ')} acknowledged it and paused.` : ''),
        detectedAt: act.sequence,
        detectedEventId: act.id,
        taskId: act.taskId,
        claimId,
        agentId: act.agentId,
        evidence: [
          { eventId: ws.claims.get(claimId)?.eventId ?? '', role: `Original claim ${claimId}` },
          { eventId: corr.id, role: `Correction superseding ${claimId}` },
          ...otherAcks.map((a) => ({ eventId: a.id, role: `Acknowledged by ${name(a.agentId)}` })),
          { eventId: act.id, role: 'Action citing the withdrawn claim' },
          ...(resolver ? [{ eventId: resolver.id, role: 'Recovery' }] : []),
        ].filter((x) => x.eventId),
        reach: dependencyReach(ws, act.taskId),
        missing: ackedBefore ? [] : [
          `No delivery or read receipt for ${corr.id} to ${name(act.agentId)}` +
            (addressed ? ' (the correction @-mentions them, but mention ≠ receipt).' : '.'),
        ],
        state: resolver ? 'resolved' : 'active',
        resolution: resolver
          ? { eventId: resolver.id, text: `${name(resolver.agentId)} ${resolver.type === 'acknowledgement' ? 'acknowledged the correction' : 'acted on a current claim'} at #${resolver.sequence}.` }
          : undefined,
      });
    }
  }
  return out;
}

export function runMonitors(ws: WorldState, name: (id: string) => string): Finding[] {
  return [...unsupportedCompletion(ws, name), ...supersededReuse(ws, name)].sort(
    (a, b) => b.detectedAt - a.detectedAt,
  );
}
