// Real-data review of every registered monitor, plus the meta counts (BT, BG, BC without a claim).
// For each window (all parts), per monitor: findings by state, and the first finding's claim + evidence ids.
// Usage: npx tsx scripts/review-monitors.ts   (writes the same report to .hf/review-m2.txt)

import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseRecallDocument } from '../src/adapters/syntheticAdapter';
import { reconstruct } from '../src/engine/reconstruct';
import { needsMet, runDefs, registry, type Finding, type MonitorDef } from '../src/engine/monitors';
import { BL } from '../src/engine/monitors/BL';
import type { RecallEvent } from '../src/model/types';
import { goalChurn, singlePointFindings, unverifiableByConstruction, verifyGoalNoClaim } from '../src/engine/meta';

const ROOT = join(import.meta.dirname, '..');
const DATA = join(ROOT, 'public/data');
if (!existsSync(join(DATA, 'index.json'))) { console.error('No public/data/index.json — run npm run data:build'); process.exit(1); }
const index = JSON.parse(readFileSync(join(DATA, 'index.json'), 'utf8')).sources as { id: string; file: string; parent?: string; part?: number; parentLabel?: string }[];
const windows = [...new Set(index.map((e) => e.parent ?? e.id))];
const defs: readonly MonitorDef[] = registry;
const M3 = new Set('D E F H O W Y AC AD AG AI AP AQ AR AU BJ BK BR AX BN AT BI BL BM'.split(' '));
const windowEvents = new Map<string, RecallEvent[]>();
const blFlips = new Map<string, Set<string>>();
const m2 = registry.filter((d) => d.id !== 'A' && d.id !== 'B');
const bg = new Map<string, Set<string>>();
const bcNo = new Map<string, Set<string>>();
const lines: string[] = [];
const say = (s = '') => { lines.push(s); console.log(s); };

type Hit = { f: Finding; part: string };
const perMonitor = new Map<string, Map<string, Map<string, Hit>>>(); // monitor → window → findingId → last hit
const errors = new Map<string, string[]>();
const bt = new Map<string, number>();
const bu: { window: string; id: string; flips: number; vanishes: number }[] = [];

for (const w of windows) {
  const parts = index.filter((e) => (e.parent ?? e.id) === w).sort((a, b) => (a.part ?? 1) - (b.part ?? 1));
  let btCount = 0;
  for (const p of parts) {
    const doc = parseRecallDocument(JSON.parse(readFileSync(join(DATA, p.file), 'utf8')));
    const input = { events: doc.events, withheld: new Set<string>(), agents: doc.agents, referencesSeen: doc.referencesSeen };
    const last = doc.events[doc.events.length - 1]?.sequence ?? 0;
    const ws = reconstruct(input, last);
    windowEvents.set(w, [...(windowEvents.get(w) ?? []), ...doc.events]);
    try { for (const f of runDefs(registry.filter((d) => d.id !== 'BL').concat(BL), ws)) if (f.monitor !== 'BL' && f.missing.some((m) => m.startsWith('continuous record'))) blFlips.set(w, (blFlips.get(w) ?? new Set()).add(f.id)); } catch { /* strict errors reported per monitor below */ }
    for (const def of defs) {
      let found: Finding[] = [];
      try { found = runDefs([def], ws); } catch (e) {
        errors.set(def.id, [...(errors.get(def.id) ?? []), `${p.id}: ${(e as Error).message.split('\n').slice(0, 3).join(' / ')}`]);
        continue;
      }
      const byWindow = perMonitor.get(def.id) ?? new Map<string, Map<string, Hit>>();
      const hits = byWindow.get(w) ?? new Map<string, Hit>();
      for (const f of found) hits.set(f.id, { f, part: p.id });
      byWindow.set(w, hits); perMonitor.set(def.id, byWindow);
    }
    btCount += unverifiableByConstruction(ws).reduce((n, r) => n + r.claimIds.length, 0);
    // BU on at most 3 active M2 findings per part (cost: one re-run per evidence record).
    for (const r of goalChurn(ws)) bg.set(w, (bg.get(w) ?? new Set()).add(r.agentId));
    for (const r of verifyGoalNoClaim(ws)) bcNo.set(w, (bcNo.get(w) ?? new Set()).add(r.taskId));
    const runCand = (x: typeof ws) => { try { return runDefs(m2, x); } catch { return []; } };
    const sample = singlePointFindings(input, last, (x) => runCand(x).filter((f) => f.state === 'active').slice(0, 3));
    for (const r of sample) bu.push({ window: w, id: r.findingId, flips: r.flips.length, vanishes: r.vanishes.length });
  }
  bt.set(w, btCount);
}

say(`Real-data review (registry, ${registry.length} monitors) · ${windows.length} windows · ${index.length} parts · generated ${new Date().toISOString()}`);
say('Window verdict = state in the last part that contains the finding. Counts are findings (deduplicated across parts).\n');
const short = (w: string) => w.replace('incidents-', '').slice(0, 22);
say(`${'monitor'.padEnd(8)}${windows.map((w) => short(w).padStart(24)).join('')}`);
for (const def of defs) {
  const byWindow = perMonitor.get(def.id);
  const cells = windows.map((w) => {
    const hits = [...(byWindow?.get(w)?.values() ?? [])];
    if (!hits.length) { const app = needsMet(def, windowEvents.get(w) ?? []); return (app.met ? '·' : `n/a (${app.unmet.join(',')})`).padStart(24); }
    const a = hits.filter((h) => h.f.state === 'active').length;
    const r = hits.filter((h) => h.f.state === 'resolved').length;
    const i = hits.filter((h) => h.f.state === 'insufficient').length;
    return `${hits.length} (${a}a ${r}r ${i}i)`.padStart(24);
  });
  say(`${(M3.has(def.id) ? `${def.id}*` : def.id).padEnd(8)}${cells.join('')}${errors.has(def.id) ? '   ⚠ errors' : ''}`);
}
say('\nFirst finding per monitor (window · part · claim/anchor · evidence ids):');
for (const def of defs) {
  const byWindow = perMonitor.get(def.id);
  const first = windows.flatMap((w) => [...(byWindow?.get(w)?.values() ?? [])].map((h) => ({ w, ...h })))[0];
  if (!first) {
    const unmet = [...new Set(windows.flatMap((w) => needsMet(def, windowEvents.get(w) ?? []).unmet))];
    const allNa = windows.every((w) => !needsMet(def, windowEvents.get(w) ?? []).met);
    say(`  ${def.id.padEnd(4)} — no findings on real data${allNa ? ` (not applicable: no ${unmet.join(' / ')} records in any window)` : ''}`);
    continue;
  }
  const ev = first.f.evidence.map((e) => e.eventId);
  say(`  ${def.id.padEnd(4)} [${first.f.state}] ${first.part}`);
  say(`       claim/anchor: ${first.f.claimId || first.f.detectedEventId}`);
  say(`       evidence: ${ev.slice(0, 6).join(', ')}${ev.length > 6 ? ` … (+${ev.length - 6})` : ''}`);
  say(`       ${first.f.summary.slice(0, 220)}`);
}
if (errors.size) {
  say('\nInvariant violations (strict mode):');
  for (const [id, list] of errors) say(`  ${id}: ${list.length} part(s) — ${list[0]}`);
}
say('\nMeta · BT (claims whose subject type has no verdict rule):');
say(`  ${windows.map((w) => `${short(w)}: ${bt.get(w)}`).join(' · ')}`);
say('Meta · BG (agents with goal churn: >= 5 pairwise-distinct goals, zero verdicts, zero claims):');
say(`  ${windows.map((w) => `${short(w)}: ${bg.get(w)?.size ?? 0}`).join(' · ')}`);
say('Meta · BC without a claim (verify-goal sessions ended with no verdict and no claim; a count, not an incident):');
say(`  ${windows.map((w) => `${short(w)}: ${bcNo.get(w)?.size ?? 0}`).join(' · ')}`);
const one = bu.filter((r) => r.flips + r.vanishes === 1).length;
say(`Meta · BU (sampled ${bu.length} active candidate findings): ${one} hang on a single record; median records that flip-or-remove = ${bu.length ? [...bu].map((r) => r.flips + r.vanishes).sort((a, b) => a - b)[Math.floor(bu.length / 2)] : 0}`);
say('Post-pass · BL (registry findings that would turn insufficient for a timeline gap once BL is registered):');
say(`  ${windows.map((w) => `${short(w)}: ${blFlips.get(w)?.size ?? 0}`).join(' · ')}`);
say('* = registered in Milestone 3.');
writeFileSync(join(ROOT, '.hf/review-m3.txt'), lines.join('\n'));
