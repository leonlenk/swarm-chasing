// npm run check — monitor test runner.
//   1. Registry: every monitor has a fixture file on disk that targets it.
//   2. Fixtures (src/data/fixtures/*.json): fires · quiet · withholding degrades honestly.
//   3. Integration: src/data/synthetic-release.json carries its own expect block.
//   4. Regression: validated real-slice findings pinned in src/data/regression.json
//      (skipped, not failed, when public/data has not been built).
// Exit code 1 on any failure. `npm run check -- --trace [--withhold]` prints the old synthetic trace.

import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseRecallDocument } from '../src/adapters/syntheticAdapter';
import { reconstruct } from '../src/engine/reconstruct';
import { assertFinding, findingProblems, register, registry, runMonitors, type Finding, type MonitorDef } from '../src/engine/monitors';
import type { FixtureBlock, FixtureExpect } from '../src/data/fixtures';
import { goalChurn, longSessionNoVerdict } from '../src/engine/meta';
import { adaptAiVillageWindow } from '../src/adapters/aiVillageHf';
import { bucketOf, remediationFor } from '../src/engine/triage';
const ws0 = (w: ReturnType<typeof reconstruct>, claimId: string) => w.claims.get(claimId)?.subject?.artifact ?? '';
import type { DataSource } from '../src/model/types';

const ROOT = join(import.meta.dirname, '..');
const args = process.argv.slice(2);
const results: { suite: string; name: string; ok: boolean | 'skip'; detail?: string }[] = [];
const record = (suite: string, name: string, ok: boolean | 'skip', detail?: string) => results.push({ suite, name, ok, detail });

const load = (p: string) => JSON.parse(readFileSync(join(ROOT, p), 'utf8')) as DataSource & { fixture?: FixtureBlock };

function world(doc: DataSource, cursor: number, withhold: string[] = []) {
  return reconstruct({ events: doc.events, withheld: new Set(withhold), agents: doc.agents, referencesSeen: doc.referencesSeen }, cursor);
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
  for (const a of x.attributes ?? []) for (const f of findings) if (!f.attributes?.includes(a)) problems.push(`${f.id} lacks attribute "${a}"`);
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
  const live = fx.expect.some((x) => (x.monitor ?? fx.monitor) === fx.monitor && (x.state === 'active' || (fx.neverActive && x.state === 'insufficient')) && !x.withhold?.length && (x.count ?? 1) > 0);
  record(suite, `live     ${fx.monitor} has an ${fx.neverActive ? 'insufficient (never-active by ruling)' : 'active'} expectation (not vacuous)`, live, live ? undefined : 'fixture never expects an active finding');
}

// ---------------------------------------------------------------- 1. registry
const fixtureDir = join(ROOT, 'src/data/fixtures');
const onDisk = new Set(readdirSync(fixtureDir).filter((f) => f.endsWith('.json')).map((f) => f.replace(/\.json$/, '')));
for (const m of registry) {
  const ok = existsSync(join(ROOT, m.fixture));
  record('registry', `${m.id} · ${m.title} → ${m.fixture}`, ok, ok ? undefined : 'fixture file missing on disk');
}
// Extra fixtures (e.g. C-build.json) are allowed: a file is an orphan only if the monitor it targets is not registered.
const targetOf = (id: string) => (load(`src/data/fixtures/${id}.json`).fixture?.monitor ?? id);
for (const id of onDisk) if (!registry.some((m) => m.id === targetOf(id))) record('registry', `fixture ${id}.json targets a registered monitor`, false, `orphan fixture (targets ${targetOf(id)})`);

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
  rejects('"insufficient" that names nothing in missing[]', { ...base, state: 'insufficient' });
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
for (const id of [...onDisk].sort()) if (!registry.some((m) => m.id === id) && registry.some((m) => m.id === targetOf(id))) runFixture(`fixture ${id} (extra, ${targetOf(id)})`, `src/data/fixtures/${id}.json`, load(`src/data/fixtures/${id}.json`));

// ---------------------------------------------------------------- 2b. meta (BG goal churn)
{
  const mk = (goals: string[], claim = false): DataSource => ({
    id: 'meta-bg', label: 'meta BG', kind: 'synthetic', description: 'synthetic', agents: [{ id: 'p', name: 'Pia', role: 'x', color: '#000' }],
    events: [
      ...goals.map((g, n) => ({ id: `s${n + 1}`, sequence: n + 1, timestamp: '2026-01-02T09:00:00Z', agentId: 'p', taskId: null, type: 'task_created', provenance: 'declared', text: '',
        payload: { tasks: [{ taskId: `T${n + 1}`, title: g, owner: 'p', goalKey: g }] }, evidenceRefs: [] })),
      ...(claim ? [{ id: 'c1', sequence: 99, timestamp: '2026-01-02T10:00:00Z', agentId: 'p', taskId: 'T1', type: 'claim', provenance: 'declared', text: '', payload: { claimId: 'C1', asserts: 'complete', rule: 'declared' }, evidenceRefs: [] }] : []),
    ],
  }) as unknown as DataSource;
  const distinct = ['deploy the site', 'write release notes', 'fix login bug', 'tidy the wiki', 'email the donors'];
  const at = (d: DataSource) => goalChurn(world(parseRecallDocument(d), 999)).length;
  record('meta BG', 'fires: 5 pairwise-distinct goals, no verdicts, no claims → 1 agent', at(mk(distinct)) === 1);
  record('meta BG', 'quiet: 4 distinct goals (below 5)', at(mk(distinct.slice(0, 4))) === 0);
  record('meta BG', 'quiet: overlapping goals (Jaccard >= 0.3) are not churn', at(mk(['deploy the site', 'deploy the site again', 'deploy site now', 'tidy the wiki', 'email the donors'])) === 0);
  record('meta BG', 'quiet: the agent made a claim in the window', at(mk(distinct, true)) === 0);
}

// ---------------------------------------------------------------- 2b'. meta (BE long session, no verdict)
{
  const mk = (turns: number, opts: { claim?: boolean; verdict?: boolean; ended?: boolean } = {}): DataSource => {
    const ev: unknown[] = [{ id: 's', sequence: 1, timestamp: '2026-01-02T09:00:00Z', agentId: 'p', taskId: null, type: 'task_created', provenance: 'observed', text: '',
      payload: { tasks: [{ taskId: 'T1', title: 'x', owner: 'p' }] }, evidenceRefs: [] }];
    for (let n = 0; n < turns; n++) ev.push({ id: `a${n}`, sequence: n + 2, timestamp: '2026-01-02T09:00:00Z', agentId: 'p', taskId: 'T1', type: 'action', provenance: 'observed', text: '',
      payload: { action: 'turn', referencesClaims: [], turnId: `t${n}`, kind: 'command' }, evidenceRefs: [] });
    let q = turns + 2;
    if (opts.claim) ev.push({ id: 'c', sequence: q++, timestamp: '2026-01-02T09:00:00Z', agentId: 'p', taskId: 'T1', type: 'claim', provenance: 'declared', text: '', payload: { claimId: 'C1', asserts: 'complete', rule: 'declared' }, evidenceRefs: [] });
    if (opts.verdict) ev.push({ id: 'v', sequence: q++, timestamp: '2026-01-02T09:00:00Z', agentId: 'p', taskId: 'T1', type: 'tool_result', provenance: 'observed', text: '', payload: { tool: 'x', runId: 'r', category: 'execution', outcome: 'fail', output: '' }, evidenceRefs: [] });
    if (opts.ended !== false) ev.push({ id: 'e', sequence: q++, timestamp: '2026-01-02T09:00:00Z', agentId: 'p', taskId: 'T1', type: 'status_updated', provenance: 'observed', text: '', payload: { status: 'ended', endReason: 'stop' }, evidenceRefs: [] });
    return { id: 'meta-be', label: 'meta BE', kind: 'synthetic', description: 'synthetic', agents: [{ id: 'p', name: 'Pia', role: 'x', color: '#000' }], events: ev } as unknown as DataSource;
  };
  const at = (d: DataSource) => longSessionNoVerdict(world(parseRecallDocument(d), 9999)).length;
  record('meta BE', 'counts: ended session, 100 turns, zero verdicts, zero claims → 1', at(mk(100)) === 1);
  record('meta BE', 'quiet: 99 turns (below N = 100)', at(mk(99)) === 0);
  record('meta BE', 'quiet: the session made a claim', at(mk(100, { claim: true })) === 0);
  record('meta BE', 'quiet: the session has a verdict', at(mk(100, { verdict: true })) === 0);
  record('meta BE', 'quiet: the session has not ended', at(mk(100, { ended: false })) === 0);
}

// ---------------------------------------------------------------- 2c. adapter claim rules
{
  const cr = JSON.parse(readFileSync(join(ROOT, 'src/data/claim-rules.json'), 'utf8')) as { cases: { name: string; content: string; claims: { url: string; rule: string }[] }[] };
  for (const c of cr.cases) {
    const doc = adaptAiVillageWindow({
      id: 'claim-rules', label: 'claim rules', window: { from: '2026-01-02T09:00:00Z', to: '2026-01-02T13:00:00Z' },
      agents: [{ id: 'a1', name: 'Agent One' }], sessions: [], boundaries: [], turns: [], generatedAt: '2026-01-02T00:00:00Z',
      chats: [{ id: 'c0000000-0000', speaker_type: 'agent', agent_speaker_id: 'a1', content: c.content, created_at: '2026-01-02 10:00:00' }],
    });
    const got = doc.events.flatMap((e) => (e.type === 'claim' && e.payload.subject ? [{ url: e.payload.subject.artifact, rule: e.payload.rule ?? '' }] : []));
    const ok = got.length === c.claims.length && c.claims.every((x) => got.some((g) => g.url === x.url && g.rule === x.rule));
    record('claim rules', c.name, ok, ok ? undefined : `got ${JSON.stringify(got)}`);
  }
}

// ---------------------------------------------------------------- 2d. push keying (owner ruling)
{
  const push = (id: string, sid: string, at: string, range: string) => ({ id, session_id: sid, created_at: at,
    agent_action: { command: 'cd /work/r && git push origin main' }, output: `To https://github.com/o/r.git\n   ${range}  main -> main`, error: '' });
  const chat = (id: string, who: string, at: string, content: string) => ({ id, speaker_type: 'agent', agent_speaker_id: who, content, created_at: at, room_id: 'room' });
  const doc = adaptAiVillageWindow({
    id: 'push-keying', label: 'push keying', window: { from: '2026-01-02T09:00:00Z', to: '2026-01-02T13:00:00Z' }, generatedAt: '2026-01-02T00:00:00Z',
    agents: [{ id: 'a1', name: 'Agent One' }, { id: 'a2', name: 'Agent Two' }], boundaries: [],
    sessions: [{ id: 's1-000000', agent_id: 'a1', created_at: '2026-01-02 09:00:00', short_displayed_session_goal: 'Write chapter' }, { id: 's2-000000', agent_id: 'a2', created_at: '2026-01-02 09:00:30', short_displayed_session_goal: 'Write appendix' }],
    turns: [push('t1', 's1-000000', '2026-01-02 09:01:00', 'aaaaaaa..bbbbbbb'), push('t2', 's2-000000', '2026-01-02 09:03:00', 'bbbbbbb..ccccccc'), push('t3', 's1-000000', '2026-01-02 09:05:00', 'ccccccc..ddddddd')],
    chats: [chat('c1-0000', 'a1', '2026-01-02 09:02:00', 'Pushed chapter one to the repo.'), chat('c2-0000', 'a2', '2026-01-02 09:04:00', 'Pushed the appendix to the repo.'),
      chat('c3-0000', 'a1', '2026-01-02 09:06:00', 'Pushed chapter two to the repo.'), chat('c4-0000', 'a1', '2026-01-02 09:07:00', 'Deployed the fix.')],
  });
  const claims = doc.events.filter((e) => e.type === 'claim');
  const versions = claims.map((c) => (c.type === 'claim' ? c.payload.subject?.version : ''));
  record('push keying', 'deployed-no-url claims are keyed per push (sha range from the session\'s latest git push)',
    versions.join(',') === 'aaaaaaa..bbbbbbb,bbbbbbb..ccccccc,ccccccc..ddddddd,ccccccc..ddddddd', versions.join(','));
  const last = doc.events[doc.events.length - 1].sequence;
  const hits = runMonitors(reconstruct({ events: doc.events, withheld: new Set(), agents: doc.agents }, last), ['AE', 'F']).filter((f) => f.monitor === 'AE' || f.id.includes('aaaaaaa'));
  record('push keying', 'AE and F do not fire across different pushes of one repo', hits.length === 0, hits.map((f) => f.id).join(', '));
}

// ---------------------------------------------------------------- 2e. triage (C grading, Open vs Needs evidence)
{
  const fx = parseRecallDocument(load('src/data/fixtures/C.json'));
  const asRepo = { ...fx, events: fx.events.map((e) => (e.type === 'claim' ? { ...e, payload: { ...e.payload, subject: { artifact: 'repo:https://github.com/o/r.git', version: 'aaaa..bbbb' } } } : e)) } as DataSource;
  const at = (d: DataSource) => { const w = world(d, 3); return runMonitors(w, ['C']).map((f) => bucketOf(f, w)); };
  record('triage', 'C on a URL subject is incident-grade (Needs evidence)', at(fx).join() === 'needs', at(fx).join());
  record('triage', 'C on a repo-pushed subject is count-grade (counts only)', at(asRepo).join() === 'count', at(asRepo).join());
  const a = parseRecallDocument(load('src/data/fixtures/A.json'));
  const last = a.events[a.events.length - 1].sequence;
  const open = runMonitors(world(a, last), ['A']).filter((f) => f.state === 'active');
  record('triage', 'an active A finding is Open (contradicted class)', open.every((f) => bucketOf(f, world(a, last)) === 'open'));
  const remedies = registry.map((m) => m.id).filter((id) => !remediationFor({ id: 'x', monitor: id, title: '', summary: '', explanation: '', detectedAt: 1, detectedEventId: 'x', taskId: '', claimId: '', agentId: 'x', evidence: [], reach: [], missing: [], state: 'active' }, world(a, 1)));
  record('triage', 'every registered monitor has a remediation template', remedies.length === 0, remedies.join(', '));
}

// ---------------------------------------------------------------- 2f. mutation suite (brief): scripted aberrations through the adapter
{
  const curl = (id: string, sid: string, at: string, url: string, code: number) => ({ id, session_id: sid, created_at: at, agent_action: { command: `curl -sI ${url}` }, output: `HTTP/2 ${code}\n`, error: '' });
  const pytest = (id: string, sid: string, at: string, line: string) => ({ id, session_id: sid, created_at: at, agent_action: { command: 'cd /work/app && pytest' }, output: `${line}\n`, error: '' });
  const chat = (id: string, who: string, at: string, content: string) => ({ id, speaker_type: 'agent', agent_speaker_id: who, content, created_at: at, room_id: 'room' });
  const U = 'https://game.example.test/play'; const U2 = 'https://docs.example.test/guide';
  const doc = adaptAiVillageWindow({
    id: 'mutation', label: 'mutation suite', window: { from: '2026-01-02T09:00:00Z', to: '2026-01-02T13:00:00Z' }, generatedAt: '2026-01-02T00:00:00Z',
    agents: [{ id: 'a1', name: 'Agent One' }, { id: 'a2', name: 'Agent Two' }], boundaries: [],
    sessions: [{ id: 's1-000000', agent_id: 'a1', created_at: '2026-01-02 09:00:00', short_displayed_session_goal: 'Ship the game' }, { id: 's2-000000', agent_id: 'a2', created_at: '2026-01-02 09:00:10', short_displayed_session_goal: 'Review the game' }],
    turns: [curl('k1', 's2-000000', '2026-01-02 09:02:00', U, 404), pytest('p1', 's1-000000', '2026-01-02 09:10:00', '==== 2 failed, 10 passed in 1.0s ====')],
    chats: [
      chat('m1-0000', 'a1', '2026-01-02 09:01:00', `The game is live at ${U}`),
      chat('m2-0000', 'a1', '2026-01-02 09:03:00', `Reminder: the game is live at ${U}`),
      chat('m3-0000', 'a2', '2026-01-02 09:04:00', `${U} is broken for me, it returns 404.`),
      chat('m4-0000', 'a1', '2026-01-02 09:05:00', `Again, the game is live at ${U}`),
      chat('m5-0000', 'a1', '2026-01-02 09:06:00', `The guide is live at ${U2}`), chat('m6-0000', 'a1', '2026-01-02 09:07:00', `Guide is live at ${U2}`), chat('m7-0000', 'a1', '2026-01-02 09:08:00', `The guide is published at ${U2}`),
      chat('m8-0000', 'a1', '2026-01-02 09:11:00', 'All tests pass now.'),
    ],
  });
  const firstFire = new Map<string, number>();
  for (const e of doc.events) {
    const w = reconstruct({ events: doc.events, withheld: new Set(), agents: doc.agents }, e.sequence);
    for (const f of runMonitors(w, ['C', 'A', 'D', 'F'])) if (f.state === 'active' && !firstFire.has(f.monitor)) firstFire.set(f.monitor, e.sequence);
  }
  const order = [...firstFire.entries()].sort((x, y) => x[1] - y[1]).map(([m]) => m);
  record('mutation', 'claim → failing check → repeat → failure posted → repeat: C, A, D, F light up in that order', order.join(',') === 'C,A,D,F', order.join(','));
  const last = doc.events[doc.events.length - 1].sequence;
  const w = reconstruct({ events: doc.events, withheld: new Set(), agents: doc.agents }, last);
  const t = runMonitors(w, ['A']).find((f) => ws0(w, f.claimId).startsWith('tests:'));
  record('mutation', 'a tests-pass claim after a failing pytest line is an Open A finding', !!t && t.state === 'active' && bucketOf(t, w) === 'open', t ? `${t.id}[${t.state}]` : 'no A finding on the tests subject');
}

// ---------------------------------------------------------------- 3. integration
runFixture('integration', 'src/data/synthetic-release.json', load('src/data/synthetic-release.json'));

// ---------------------------------------------------------------- 4. regression
interface RegCase { name: string; slice: string; monitor?: string; claimId?: string; claimEventId?: string; subject?: string; expect?: Record<string, 'none' | 'allowed'>; reason?: string; state: 'active' | 'resolved' | 'insufficient' | 'none' }
const reg = JSON.parse(readFileSync(join(ROOT, 'src/data/regression.json'), 'utf8')) as { cases: RegCase[] };
// Skip only when no data has been built at all. Once public/data exists, a missing pinned slice is a failure:
// it means the window plan or the build changed underneath a validated finding.
// A pinned slice is a WINDOW: it may be split into parts (index entries with parent = slice). The window's verdict
// for a claim is its state in the last part that contains the finding (the most complete record).
const dataDir = join(ROOT, 'public/data');
const dataBuilt = existsSync(dataDir);
const indexEntries: { id: string; file: string; parent?: string; part?: number }[] =
  dataBuilt && existsSync(join(dataDir, 'index.json')) ? JSON.parse(readFileSync(join(dataDir, 'index.json'), 'utf8')).sources : [];
for (const c of reg.cases) {
  if (!dataBuilt) { record('regression', c.name, 'skip', 'public/data/ not built (npm run data:build)'); continue; }
  const parts = indexEntries.filter((e) => e.id === c.slice || e.parent === c.slice).sort((x, y) => (x.part ?? 1) - (y.part ?? 1));
  if (!parts.length || parts.some((e) => !existsSync(join(dataDir, e.file)))) {
    record('regression', c.name, false, `public/data exists but window ${c.slice} is missing or incomplete: the pinned slice was not built`);
    continue;
  }
  const perPart = parts.map((e) => {
    const doc = parseRecallDocument(JSON.parse(readFileSync(join(dataDir, e.file), 'utf8')));
    const last = doc.events[doc.events.length - 1]?.sequence ?? 0;
    return { id: e.id, doc, last, ...findingsOf(doc, last, []) };
  });
  const broken = perPart.find((x) => x.error);
  if (broken) { record('regression', c.name, false, `${broken.id}: ${broken.error}`); continue; }
  if (c.state === 'none') {
    const hits = perPart.flatMap((x) => {
      const ws = world(x.doc, x.last);
      return x.findings.filter((f) => c.expect?.[f.monitor] !== 'allowed' && ws.claims.get(f.claimId)?.subject?.artifact === c.subject).map((f) => `${x.id}:${f.id}[${f.state}]`);
    });
    const allowed = perPart.flatMap((x) => {
      const ws = world(x.doc, x.last);
      return x.findings.filter((f) => c.expect?.[f.monitor] === 'allowed' && ws.claims.get(f.claimId)?.subject?.artifact === c.subject).map((f) => f.id);
    });
    const allowedNote = Object.entries(c.expect ?? {}).filter(([, v]) => v === 'allowed').map(([m]) => `${m} allowed: ${new Set(allowed.filter((id) => id.startsWith(`${m}:`))).size} finding(s)`).join('; ');
    record('regression', `${c.name}${allowedNote ? ` (${allowedNote})` : ''}`, hits.length === 0, hits.slice(0, 3).join(', '));
    continue;
  }
  const containing = perPart.filter((x) => x.findings.some((f) => f.monitor === c.monitor && (f.claimId === c.claimId || f.detectedEventId === c.claimEventId)));
  const lastPart = containing[containing.length - 1];
  const hit = lastPart?.findings.find((f) => f.monitor === c.monitor && (f.claimId === c.claimId || f.detectedEventId === c.claimEventId));
  record('regression', c.name, !!hit && hit.state === c.state,
    hit ? `got "${hit.state}" in ${lastPart.id} (${containing.length} part${containing.length > 1 ? 's' : ''} contain it)` : `no ${c.monitor} finding for ${c.claimId} in any of ${parts.length} part(s)`);
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

// Monitor liveness (AK): per-monitor fixture results, written next to the data for the Monitors view.
if (dataBuilt) {
  const monitors: Record<string, { fixtures: number; passed: boolean; live: boolean; failures: string[] }> = {};
  for (const m of registry) monitors[m.id] = { fixtures: 0, passed: true, live: false, failures: [] };
  for (const r of results) {
    const m = /^fixture (\S+)(?: \(extra, (\S+)\))?$/.exec(r.suite);
    if (!m) continue;
    const id = m[2] ?? m[1];
    const row = monitors[id];
    if (!row) continue;
    if (r.name.startsWith('fires') || r.name.startsWith('withhold')) row.fixtures++;
    if (r.ok === false) { row.passed = false; row.failures.push(r.name); }
    if (r.name.startsWith('live') && r.ok === true) row.live = true;
  }
  writeFileSync(join(dataDir, 'check.json'), JSON.stringify({ generatedAt: new Date().toISOString(), passed: pass, failed: fail.length, monitors }, null, 1));
}

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
