// assertFinding(): the brief's monitor rules, enforced in code.
// Strict (dev server, npm run check): a violating finding throws.
// Production build: nothing disappears. A violating finding is converted to 'insufficient' with
// missing: ['invariant: <check>'] so it shows under "Needs evidence" and is counted in the Monitors view.

import type { WorldState } from '../reconstruct';
import type { Finding, MonitorDef } from './types';

const STATES = new Set(['active', 'resolved', 'insufficient']);
export const INVARIANT_PREFIX = 'invariant: ';

/** Strict unless running inside a production Vite build. */
const STRICT_DEFAULT = (() => {
  try {
    return !(import.meta as unknown as { env?: { PROD?: boolean } }).env?.PROD;
  } catch {
    return true;
  }
})();

/** Named invariant checks. The name is what appears in missing[] when a production finding is converted. */
export type InvariantCheck =
  | 'state-valid' | 'monitor-id' | 'id-prefix' | 'evidence-linked' | 'no-future-detection' | 'detected-record-visible'
  | 'evidence-has-id' | 'no-future-evidence' | 'unavailable-named' | 'partial-is-insufficient'
  | 'insufficient-needs-unavailable' | 'resolution-matches-state' | 'resolution-in-evidence' | 'resolved-has-resolution'
  | 'reach-known-tasks';

export interface InvariantProblem {
  check: InvariantCheck;
  message: string;
}

export class FindingViolation extends Error {
  readonly finding: Finding;
  readonly problems: InvariantProblem[];
  constructor(finding: Finding, problems: InvariantProblem[]) {
    super(`Finding ${finding.id} violates monitor rules:\n  - ${problems.map((p) => `[${p.check}] ${p.message}`).join('\n  - ')}`);
    this.finding = finding;
    this.problems = problems;
  }
}

/** Every invariant the finding breaks (empty when valid). */
export function invariantProblems(f: Finding, ws: WorldState, def?: MonitorDef): InvariantProblem[] {
  const p: InvariantProblem[] = [];
  const add = (check: InvariantCheck, message: string) => p.push({ check, message });
  if (!STATES.has(f.state)) add('state-valid', `state "${f.state}" is not one of active | resolved | insufficient`);
  if (def && f.monitor !== def.id) add('monitor-id', `monitor "${f.monitor}" does not match registry id "${def.id}"`);
  if (def && !f.id.startsWith(`${def.id}:`)) add('id-prefix', `id "${f.id}" must start with "${def.id}:"`);
  if (!f.evidence.length) add('evidence-linked', 'no linked evidence: every finding must link to records');
  if (f.detectedAt > ws.cursor) add('no-future-detection', `detected at #${f.detectedAt}, after the cursor #${ws.cursor}`);
  if (ws.refStatus(f.detectedEventId) !== 'available') add('detected-record-visible', `detectedEventId ${f.detectedEventId} is not a visible record`);

  const unavailable: string[] = [];
  for (const ev of f.evidence) {
    if (!ev.eventId) { add('evidence-has-id', `evidence "${ev.role}" has no record id`); continue; }
    const status = ws.refStatus(ev.eventId);
    if (status === 'future') add('no-future-evidence', `evidence ${ev.eventId} is after the cursor`);
    if (status === 'withheld' || status === 'missing') {
      unavailable.push(ev.eventId);
      if (!f.missing.some((m) => m.includes(ev.eventId))) add('unavailable-named', `evidence ${ev.eventId} is ${status} but not named in missing[]`);
    }
  }
  if (unavailable.length && f.state !== 'insufficient') {
    add('partial-is-insufficient', `state is "${f.state}" while ${unavailable.join(', ')} ${unavailable.length > 1 ? 'are' : 'is'} unavailable`);
  }
  if (f.state === 'insufficient' && !f.missing.length) {
    add('insufficient-needs-unavailable', 'state is "insufficient" but missing[] does not name what is missing');
  }
  if (f.resolution) {
    if (f.state !== 'resolved') add('resolution-matches-state', 'has a resolution but state is not "resolved"');
    if (!f.evidence.some((e) => e.eventId === f.resolution!.eventId)) add('resolution-in-evidence', `resolution ${f.resolution.eventId} is not in evidence[]`);
  } else if (f.state === 'resolved') add('resolved-has-resolution', 'state is "resolved" but no resolution record is given');
  for (const t of f.reach) if (!ws.tasks.has(t)) add('reach-known-tasks', `reach names unknown task ${t}`);
  return p;
}

/** Back-compat helper: problems as plain sentences. */
export function findingProblems(f: Finding, ws: WorldState, def?: MonitorDef): string[] {
  return invariantProblems(f, ws, def).map((x) => `[${x.check}] ${x.message}`);
}

/** True when a finding was converted because it broke an invariant (production only). */
export const isInvariantFailure = (f: Finding) => f.missing.some((m) => m.startsWith(INVARIANT_PREFIX));

/**
 * Validates a finding. Strict: throws FindingViolation. Non-strict (production): returns the finding
 * converted to 'insufficient' with one missing[] entry per failed check. Never returns nothing.
 */
export function assertFinding(f: Finding, ws: WorldState, def?: MonitorDef, opts: { strict?: boolean } = {}): Finding {
  const problems = invariantProblems(f, ws, def);
  if (!problems.length) return f;
  const strict = opts.strict ?? STRICT_DEFAULT;
  if (strict) throw new FindingViolation(f, problems);
  console.error(new FindingViolation(f, problems).message);
  const checks = [...new Set(problems.map((x) => x.check))];
  return {
    ...f,
    state: 'insufficient',
    resolution: undefined,
    missing: [...f.missing, ...checks.map((c) => `${INVARIANT_PREFIX}${c}`)],
  };
}
