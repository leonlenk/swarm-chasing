// Prints reconstructed state + findings at every timeline position (sanity check, no UI).
import { readFileSync } from 'node:fs';
import { parseRecallDocument } from '../src/adapters/syntheticAdapter';
import { reconstruct } from '../src/engine/reconstruct';
import { runMonitors } from '../src/engine/monitors';

const src = parseRecallDocument(JSON.parse(readFileSync(new URL('../src/data/synthetic-release.json', import.meta.url), 'utf8')));
const name = (id: string) => src.agents.find((a) => a.id === id)?.name ?? id;
for (const e of src.events) {
  const ws = reconstruct({ events: src.events, withheld: new Set(process.argv.includes('--withhold') ? src.experiment?.withhold : []) }, e.sequence);
  const tasks = [...ws.tasks.values()].map((t) => `${t.id}:${t.reportedStatus}/${t.evidenceStatus[0].toUpperCase()}`).join(' ');
  const f = runMonitors(ws, name).map((x) => `${x.id}[${x.state}] reach=${x.reach.join(',')}`).join(' | ');
  console.log(`#${String(e.sequence).padStart(2)} ${e.type.padEnd(16)} ${tasks}\n     ${f || '—'}`);
}
