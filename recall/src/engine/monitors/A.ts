// A · Unsupported completion.

import type { EventOf } from '../../model/types';
import { dependencyReach, type WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';
import { asserted, checkNoun, doneClause, isLive, subj, unavailableWhy } from './text';

function run(ws: WorldState): Finding[] {
  const name = ws.agentName;
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
      out.push({
        id: `A:${claim.id}`,
        monitor: 'A',
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
        missing: gone.map((r) => `${r} — ${unavailableWhy(ws, r)}.`),
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
    const reach = dependencyReach(ws, claim.taskId);

    out.push({
      id: `A:${claim.id}`,
      monitor: 'A',
      title: isLive(s) ? `Claimed live after a failed check: ${subj(s)}` : `${claim.payload.claimId} reports ${subj(s)} passed after it failed`,
      summary: resolver
        ? `${name(claim.agentId)}'s claim that ${asserted(claim)} contradicted ${fail.payload.runId}; it was ${resolver.type === 'correction' ? `withdrawn at #${resolver.sequence}` : `superseded by a passing run at #${resolver.sequence}`}.`
        : `${name(claim.agentId)} claimed ${asserted(claim)}${doneClause(claim)}, but the only ${checkNoun(s)} (${fail.payload.runId}) failed` +
          (reach.length ? `; ${reach.length} downstream tasks depend on it.` : '.'),
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
      reach,
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

export const A: MonitorDef = {
  id: 'A',
  title: 'Unsupported completion',
  family: 'claim-evidence',
  needs: ['claim', 'tool_result', 'subject'],
  fixture: 'src/data/fixtures/A.json',
  rule:
    'claim.asserts ∈ {verification_passed, complete} AND claim.subject = S\n' +
    'AND ∃ verification tool_result r: r.subject = S ∧ r.outcome = fail ∧ r.seq < claim.seq\n' +
    'AND ∄ passing verification of S before the claim\n' +
    '→ ACTIVE · RESOLVED once withdrawn by a correction or a later pass of S exists\n' +
    '→ INSUFFICIENT if no verification of S is visible but the claim cites a withheld/missing record',
  run,
};
