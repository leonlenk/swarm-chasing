// npm run check — monitor test runner.
//   1. Registry: every monitor has a fixture file on disk that targets it.
//   2. Fixtures (src/data/fixtures/*.json): fires · quiet · withholding degrades honestly.
//   3. Integration: src/data/synthetic-release.json carries its own expect block.
//   4. Regression: validated real-slice findings pinned in src/data/regression.json
//      (skipped, not failed, when public/data has not been built).
// Exit code 1 on any failure. `npm run check -- --trace [--withhold]` prints the old synthetic trace.

import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseRecallDocument } from '../src/adapters/syntheticAdapter';
import { reconstruct } from '../src/engine/reconstruct';
import { assertFinding, findingProblems, register, registry, runMonitors, type Finding, type MonitorDef } from '../src/engine/monitors';
import type { FixtureBlock, FixtureExpect } from '../src/data/fixtures';
import type { DataSource } from '../src/model/types';

const ROOT = join(import.meta.dirname, '..');
const args = process.argv.slice(2);
const results: { suite: string; name: string; ok: boolean | 'skip'; detail?: string }[] = [];
const record = (suite: string, name: string, ok: boolean | 'skip', detail?: string) => results.push({ suite, name, ok, detail });

const load = (p: string) => JSON.parse(readFileSync(join(ROOT, p), 'utf8')) as DataSource & { fixture?: FixtureBlock };

function world(doc: DataSource, cursor: number, withhold: string[] = []) {
  return reconstruct({ events: doc.events, withheld: new Set(withhold), agents: doc.agents }, cursor);
}

/** Runs monitors, turning assertFinding violations into a failed assertion instead of a crash. */
function findingsOf(doc: DataSource, cursor: number, withhold: string[], only?: string[]): { findings: Finding[]; error?: string } {
  try {
    return { findings: runMonitors(world(doc, cursor, withhold), only) };
  } catch (e) {
    return { findings: [], error: e instanceof Error ? e.message : String(e) };
  }
}

function checkExpect(doc: DataSource, monitor: string, x: FixtureExpect): string[] {
  const problems: string[] = [];
  const { findings, error } = findingsOf(doc, x.atCursor, x.withhold ?? [], [monitor]);
  if (error) return [error];
  if (x.count !== undefined && findings.length !== x.count) {
    problems.push(`expected ${x.count} finding(s), got ${findings.length}${findings.length ? ` (${findings.map((f) => `${f.id}[${f.state}]`).join(', ')})` : ''}`);
  }
  if (x.state) for (const f of findings) if (f.state !== x.state) problems.push(`${f.id} is "${f.state}", expected "${x.state}"`);
  const evidence = new Set(findings.flatMap((f) => f.evidence.map((e) => e.eventId)));
  for (const id of x.evidenceIds ?? []) if (!evidence.has(id)) problems.push(`evidence[] lacks ${id}`);
  for (const id of x.missing ?? []) if (!findings.some((f) => f.missing.some((m) => m.includes(id)))) problems.push(`missing[] does not name ${id}`);
  return problems;
}

function describe(x: FixtureExpect, monitor: string): { kind: 'fires' | 'withhold'; label: string } {
  const what = x.count === 0 ? 'no finding' : `${x.count ?? '≥1'} ${x.state ?? ''}`.trim();
  return x.withhold?.length
    ? { kind: 'withhold', label: `${monitor} @#${x.atCursor} withhold ${x.withhold.join(',')} → ${what}` }
    : { kind: 'fires', label: `${monitor} @#${x.atCursor} → ${what}` };
}

function runFixture(suite: string, path: string, doc0: DataSource & { fixture?: FixtureBlock }) {
  const fx = doc0.fixture;
  if (!fx) { record(suite, path, false, 'no fixture block'); return; }
  const doc = parseRecallDocument(doc0);
  for (const x of fx.expect) {
    const monitor = x.monitor ?? fx.monitor;
    const { kind, label } = describe(x, monitor);
    const problems = checkExpect(doc, monitor, x);
    record(suite, `${kind.padEnd(8)} ${label}`, problems.length === 0, problems.join('; '));
  }
  if (fx.quiet?.length) {
    const noisy: string[] = [];
    for (const e of doc.events) {
      const { findings, error } = findingsOf(doc, e.sequence, [], fx.quiet);
      if (error) noisy.push(`#${e.sequence}: ${error}`);
      for (const f of findings) noisy.push(`#${e.sequence}: ${f.id}[${f.state}]`);
    }
    record(suite, `quiet    ${fx.quiet.join(', ')} produce nothing at any cursor`, noisy.length === 0, [...new Set(noisy)].slice(0, 4).join('; '));
  }
  const live = fx.expect.some((x) => (x.monitor ?? fx.monitor) === fx.monitor && x.state === 'active' && !x.withhold?.length && (x.count ?? 1) > 0);
  record(suite, `live     ${fx.monitor} has an active expectation (not vacuous)`, live, live ? undefined : 'fixture never expects an active finding');
}

// ---------------------------------------------------------------- 1. registry
const fixtureDir = join(ROOT, 'src/data/fixtures');
const onDisk = new Set(readdirSync(fixtureDir).filter((f) => f.endsWith('.json')).map((f) => f.replace(/\.json$/, '')));
for (const m of registry) {
  const ok = existsSync(join(ROOT, m.fixture));
  record('registry', `${m.id} · ${m.title} → ${m.fixture}`, ok, ok ? undefined : 'fixture file missing on disk');
}
for (const id of onDisk) if (!registry.some((m) => m.id === id)) record('registry', `fixture ${id}.json has a registered monitor`, false, 'orphan fixture');

// Negative tests: the guarantees must actually refuse bad input.
const fake = (id: string, fixture = `src/data/fixtures/${id}.json`): MonitorDef =>
  ({ id, title: 'fake', family: 'meta', rule: '', needs: [], fixture, run: () => [] });
const refuses = (label: string, fn: () => unknown) => {
  try { fn(); record('registry', `refuses ${label}`, false, 'accepted'); } catch { record('registry', `refuses ${label}`, true); }
};
refuses('a monitor with no registered fixture', () => register([fake('ZZ')]));
refuses('a monitor whose fixture path is wrong', () => register([fake('A', 'fixtures/A.json')]));
refuses('duplicate monitor ids', () => register([registry[0], registry[0]]));
refuses('a fixture without an expect block', () => register([fake('A')], { A: { fixture: { monitor: 'A', expect: [] } } }));
{
  const syn = parseRecallDocument(load('src/data/synthetic-release.json'));
  const ws = world(syn, 12, ['ev-05']);
  const base: Finding = { id: 'A:x', monitor: 'A', title: '', summary: '', explanation: '', detectedAt: 6, detectedEventId: 'ev-06',
    taskId: 'T2', claimId: 'C1', agentId: 'kestrel', evidence: [{ eventId: 'ev-06', role: 'claim' }], reach: [], missing: [], state: 'active' };
  const rejects = (label: string, f: Finding) => record('rules', `assertFinding rejects ${label}`, findingProblems(f, ws, registry[0]).length > 0, 'accepted');
  rejects('an active finding that links a withheld record', { ...base, evidence: [...base.evidence, { eventId: 'ev-05', role: 'check' }], missing: ['ev-05'] });
  rejects('a withheld record not named in missing[]', { ...base, state: 'insufficient', evidence: [...base.evidence, { eventId: 'ev-05', role: 'check' }] });
  rejects('evidence after the cursor (future leakage)', { ...base, evidence: [...base.evidence, { eventId: 'ev-14', role: 'later' }] });
  rejects('a finding with no linked evidence', { ...base, evidence: [] });
  rejects('"insufficient" with nothing unavailable', { ...base, state: 'insufficient' });
  rejects('"resolved" without a resolution record', { ...base, state: 'resolved' });
  rejects('a state outside the three', { ...base, state: 'maybe' as Finding['state'] });
  // Production mode never drops: a violating finding becomes insufficient, naming each failed check.
  const bad = { ...base, evidence: [...base.evidence, { eventId: 'ev-05', role: 'check' }], missing: ['ev-05'] };
  const converted = assertFinding(bad, ws, registry[0], { strict: false });
  record('rules', 'production mode converts a violating finding to insufficient (not dropped)',
    converted.state === 'insufficient' && converted.missing.includes('invariant: partial-is-insufficient'),
    `got ${converted.state} · ${converted.missing.join(' | ')}`);
  let threw = false;
  try { assertFinding(bad, ws, registry[0], { strict: true }); } catch { threw = true; }
  record('rules', 'strict mode throws on the same finding', threw, 'did not throw');
  record('rules', 'assertFinding accepts a valid finding', findingProblems(base, ws, registry[0]).length === 0, findingProblems(base, ws, registry[0]).join('; '));
}

// ---------------------------------------------------------------- 2. fixtures
for (const m of registry) if (existsSync(join(ROOT, m.fixture))) runFixture(`fixture ${m.id}`, m.fixture, load(m.fixture));

// ---------------------------------------------------------------- 3. integration
runFixture('integration', 'src/data/synthetic-release.json', load('src/data/synthetic-release.json'));

// ---------------------------------------------------------------- 4. regression
interface RegCase { name: string; slice: string; monitor?: string; claimId?: string; claimEventId?: string; subject?: string; state: 'active' | 'resolved' | 'insufficient' | 'none' }
const reg = JSON.parse(readFileSync(join(ROOT, 'src/data/regression.json'), 'utf8')) as { cases: RegCase[] };
// Skip only when no data has been built at all. Once public/data exists, a missing pinned slice is a failure:
// it means the window plan or the build changed underneath a validated finding.
const dataBuilt = existsSync(join(ROOT, 'public/data'));
for (const c of reg.cases) {
  const file = join(ROOT, 'public/data', `${c.slice}.json`);
  if (!dataBuilt) { record('regression', c.name, 'skip', 'public/data/ not built (npm run data:build)'); continue; }
  if (!existsSync(file)) { record('regression', c.name, false, `public/data exists but ${c.slice}.json is missing: the pinned slice was not built`); continue; }
  const doc = parseRecallDocument(JSON.parse(readFileSync(file, 'utf8')));
  const last = doc.events[doc.events.length - 1]?.sequence ?? 0;
  const { findings, error } = findingsOf(doc, last, []);
  if (error) { record('regression', c.name, false, error); continue; }
  if (c.state === 'none') {
    const ws = world(doc, last);
    const hits = findings.filter((f) => ws.claims.get(f.claimId)?.subject?.artifact === c.subject);
    record('regression', c.name, hits.length === 0, hits.map((f) => `${f.id}[${f.state}]`).join(', '));
    continue;
  }
  const hit = findings.find((f) => f.monitor === c.monitor && (f.claimId === c.claimId || f.detectedEventId === c.claimEventId));
  record('regression', c.name, !!hit && hit.state === c.state, hit ? `got "${hit.state}"` : `no ${c.monitor} finding for ${c.claimId}`);
}

// ---------------------------------------------------------------- report
const pass = results.filter((r) => r.ok === true).length;
const fail = results.filter((r) => r.ok === false);
const skip = results.filter((r) => r.ok === 'skip').length;
let suite = '';
console.log(`\nRECALL check · ${registry.length} monitor${registry.length === 1 ? '' : 's'} registered (${registry.map((m) => m.id).join(', ')})`);
for (const r of results) {
  if (r.suite !== suite) { suite = r.suite; console.log(`\n${suite}`); }
  const mark = r.ok === true ? '  ✓' : r.ok === 'skip' ? '  –' : '  ✗';
  console.log(`${mark} ${r.name}${r.ok !== true && r.detail ? `\n      ${r.detail}` : ''}`);
}
console.log(`\n${pass} passed · ${fail.length} failed · ${skip} skipped\n`);

// ---------------------------------------------------------------- optional trace
if (args.includes('--trace')) {
  const src = parseRecallDocument(load('src/data/synthetic-release.json'));
  const withhold = args.includes('--withhold') ? src.experiment?.withhold ?? [] : [];
  for (const e of src.events) {
    const ws = world(src, e.sequence, withhold);
    const tasks = [...ws.tasks.values()].map((t) => `${t.id}:${t.reportedStatus}/${t.evidenceStatus[0].toUpperCase()}`).join(' ');
    const f = runMonitors(ws).map((x) => `${x.id}[${x.state}] reach=${x.reach.join(',')}`).join(' | ');
    console.log(`#${String(e.sequence).padStart(2)} ${e.type.padEnd(16)} ${tasks}\n     ${f || '—'}`);
  }
}

process.exit(fail.length ? 1 : 0);
