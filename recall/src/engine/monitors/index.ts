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

export const registry: readonly MonitorDef[] = register([A, B]);

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
  const ranked: { f: Finding; order: number }[] = [];
  registry.forEach((def, order) => {
    if (only && !only.includes(def.id)) return;
    // assertFinding throws in strict mode; in production it converts, so nothing is dropped.
    for (const f of def.run(ws)) ranked.push({ f: assertFinding(f, ws, def), order });
  });
  return ranked.sort((a, b) => b.f.detectedAt - a.f.detectedAt || a.order - b.order).map((r) => r.f);
}
