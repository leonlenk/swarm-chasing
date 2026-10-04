// Swarm series (owner ruling): per window part, swarm counts at sampled cursor positions, written to
// public/data/series/<part>.json for the Swarm view's sparklines. Counts only, never percentages.
// Usage: npx tsx scripts/build-series.ts   (run by npm run data:build after the parts are written)

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseRecallDocument } from '../src/adapters/syntheticAdapter';
import { reconstruct } from '../src/engine/reconstruct';
import { runMonitors } from '../src/engine/monitors';
import { CASCADE_ALERT, subjectIncidents, swarmRates } from '../src/engine/subjects';

const ROOT = join(import.meta.dirname, '..');
const DATA = join(ROOT, 'public/data');
const POINTS = 16;
if (!existsSync(join(DATA, 'index.json'))) { console.error('No public/data/index.json — run npm run data:build'); process.exit(1); }
mkdirSync(join(DATA, 'series'), { recursive: true });
const index = JSON.parse(readFileSync(join(DATA, 'index.json'), 'utf8')) as { sources: { id: string; file: string }[] };
const t0 = Date.now();
for (const e of index.sources) {
  const doc = parseRecallDocument(JSON.parse(readFileSync(join(DATA, e.file), 'utf8')));
  const seqs = doc.events.filter((x) => !x.carried).map((x) => x.sequence);
  if (!seqs.length) continue;
  const pts = [...new Set(Array.from({ length: POINTS }, (_, i) => seqs[Math.round(((seqs.length - 1) * i) / (POINTS - 1))]))];
  const input = { events: doc.events, withheld: new Set<string>(), agents: doc.agents, referencesSeen: doc.referencesSeen };
  const rows = pts.map((seq) => {
    const ws = reconstruct(input, seq);
    // Only the monitors the rates read (AC, V); subject incidents need no monitor run.
    const f = runMonitors(ws, ['AC', 'V']);
    const inc = subjectIncidents(ws, f);
    const r = swarmRates(ws, f, inc);
    return {
      seq,
      repeatsWithoutCheck: [r.repeatsWithoutCheck.n, r.repeatsWithoutCheck.of],
      checks: r.checksPerAgent.reduce((n, x) => n + x.n, 0),
      checkers: r.checksPerAgent.length,
      consensusWithoutCheck: r.consensusWithoutCheck.n,
      duplicateGoals: r.duplicateGoals.n,
      directiveUptake: [r.directiveUptake.n, r.directiveUptake.of],
      correctionReach: [r.correctionReach.n, r.correctionReach.of],
      cascadesWithoutCheck: inc.filter((i) => i.cascade.n >= CASCADE_ALERT && i.standing === 'unchecked').length,
      singlePoints: r.singlePoints.length,
    };
  });
  writeFileSync(join(DATA, 'series', `${e.id}.json`), JSON.stringify({ id: e.id, points: rows }));
}
console.log(`✓ wrote public/data/series/ for ${index.sources.length} sources in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
