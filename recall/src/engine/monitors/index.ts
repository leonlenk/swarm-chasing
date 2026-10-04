// Monitor registry. Monitors are files, not edits to a shared module: add a MonitorDef here with a
// fixture under src/data/fixtures/<id>.json, and it appears in the Monitors and Incidents views.
// Registry order = Incidents order within a subject group (claim-evidence family first).

import type { RecallEvent } from '../../model/types';
import type { WorldState } from '../reconstruct';
import { FIXTURES } from '../../data/fixtures';
import { assertFinding } from './assert';
import type { Finding, MonitorDef, MonitorNeed } from './types';
import { A } from './A';
import { B } from './B';
import { C } from './C';
import { G } from './G';
import { J } from './J';
import { X } from './X';
import { Z } from './Z';
import { AE } from './AE';
import { AF } from './AF';
import { AH } from './AH';
import { V } from './V';
import { U } from './U';
import { AM } from './AM';
import { AO } from './AO';
import { AS } from './AS';
import { AW } from './AW';
import { BC } from './BC';
import { BD } from './BD';
import { BP } from './BP';

export type { EvidenceLink, Finding, FindingState, MonitorDef, MonitorFamily, MonitorNeed } from './types';
export { assertFinding, findingProblems, FindingViolation, INVARIANT_PREFIX, invariantProblems, isInvariantFailure } from './assert';

/** Validates and freezes a registry. Throws if any monitor lacks a fixture or duplicates an id. Exported for tests. */
export function register(defs: MonitorDef[], fixtures: Record<string, { fixture?: { monitor?: string; expect?: unknown[] } }> = FIXTURES): readonly MonitorDef[] {
  const seen = new Set<string>();
  for (const d of defs) {
    if (seen.has(d.id)) throw new Error(`Monitor registry: duplicate id "${d.id}"`);
    seen.add(d.id);
    const expected = `src/data/fixtures/${d.id}.json`;
    if (d.fixture !== expected) throw new Error(`Monitor ${d.id} refused: fixture must be "${expected}" (got "${d.fixture}")`);
    const fx = fixtures[d.id];
    if (!fx) throw new Error(`Monitor ${d.id} refused: no fixture registered in src/data/fixtures/index.ts`);
    if (fx.fixture?.monitor !== d.id) throw new Error(`Monitor ${d.id} refused: fixture ${expected} targets "${fx.fixture?.monitor}"`);
    if (!fx.fixture?.expect?.length) throw new Error(`Monitor ${d.id} refused: fixture ${expected} has no expect block`);
  }
  return Object.freeze([...defs]);
}

export const registry: readonly MonitorDef[] = register([A, B, C, G, J, AM, AO, AS, AE, AF, BP, AW, X, Z, U, V, BC, BD, AH]);

export const monitorById = (id: string) => registry.find((m) => m.id === id);
export const MONITOR_LABEL: Record<string, string> = Object.fromEntries(registry.map((m) => [m.id, m.title]));

/** Source-level applicability: true when every need is present in the source's events. Display only; never gates a run. */
export function needsMet(def: MonitorDef, events: readonly RecallEvent[]): { met: boolean; unmet: MonitorNeed[] } {
  const types = new Set(events.map((e) => e.type));
  const has = (n: MonitorNeed) => {
    if (n === 'subject') return events.some((e) => (e.type === 'claim' || e.type === 'tool_result') && !!e.payload.subject);
    if (n === 'dependency') return events.some((e) => e.type === 'dependency_created' || (e.type === 'task_created' && e.payload.tasks.some((t) => t.dependsOn?.length)));
    return types.has(n);
  };
  const unmet = def.needs.filter((n) => !has(n));
  return { met: unmet.length === 0, unmet };
}

/**
 * Runs every registered monitor, validating each finding. Newest first, then registry order.
 * Needs are NOT a gate here: applicability is a property of the whole source (see needsMet, used by
 * the Monitors view). Gating on visible records would let a withheld record silently switch a
 * monitor off instead of degrading its findings to insufficient.
 */
export function runMonitors(ws: WorldState, only?: readonly string[]): Finding[] {
  return runDefs(registry, ws, only);
}

/**
 * Withheld-in-span post-pass. A monitor cannot see a withheld record's content, so it cannot know
 * whether that record would change its finding. If a withheld record's POSITION lies inside a finding's
 * evidence span, RECALL cannot rule that out: the finding becomes insufficient and names the record.
 * Uses positions only (WorldState.withheldSeqs), never content.
 */
export function degradeForWithheldInSpan(f: Finding, ws: WorldState, def?: MonitorDef): Finding {
  if (!ws.withheldSeqs.size) return f;
  const reads = def ? readTypes(def) : null;
  const seqs = f.evidence.map((e) => ws.byId.get(e.eventId)?.sequence).filter((n): n is number => n !== undefined);
  if (!seqs.length) return f;
  const lo = Math.min(...seqs);
  const hi = Math.max(...seqs, f.detectedAt);
  // Only record types the monitor reads (its needs): unrelated withheld chat must not blanket every finding.
  const inside = [...ws.withheldSeqs].filter(([id, seq]) => seq >= lo && seq <= hi && !f.evidence.some((e) => e.eventId === id) &&
    (!reads || reads.has(ws.withheldTypes.get(id)!)));
  if (!inside.length) return f;
  return {
    ...f,
    state: 'insufficient',
    resolution: undefined,
    evidence: [...f.evidence, ...inside.map(([id]) => ({ eventId: id, role: 'Withheld record inside this finding\'s span' }))],
    missing: [...f.missing, ...inside.map(([id]) => `${id} — withheld from this analysis inside the span of this finding; RECALL cannot rule out that it changes the finding.`)],
  };
}

/** Event types a monitor reads: its EventType needs, with 'subject' → claim + tool_result and 'dependency' → task records. */
function readTypes(def: MonitorDef): Set<string> {
  const out = new Set<string>(def.reads ?? []);
  for (const n of def.needs) {
    if (n === 'subject') { out.add('claim'); out.add('tool_result'); }
    else if (n === 'dependency') { out.add('task_created'); out.add('dependency_created'); }
    else out.add(n);
  }
  return out;
}

/** Runs the given monitor definitions (the registry, or candidates under review) with the shared post-passes. */
export function runDefs(defs: readonly MonitorDef[], ws: WorldState, only?: readonly string[]): Finding[] {
  const ranked: { f: Finding; order: number }[] = [];
  defs.forEach((def, order) => {
    if (only && !only.includes(def.id)) return;
    // assertFinding throws in strict mode; in production it converts, so nothing is dropped.
    for (const f of def.run(ws)) ranked.push({ f: assertFinding(degradeForWithheldInSpan(f, ws, def), ws, def), order });
  });
  return ranked.sort((a, b) => b.f.detectedAt - a.f.detectedAt || a.order - b.order).map((r) => r.f);
}
