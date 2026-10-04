// Build RECALL sources from the AI Village dataset on Hugging Face.
//
//   https://huggingface.co/datasets/aidigestorg/ai-village   (gated: request access first)
//
// Auth: HF_TOKEN env var, or the token saved by `huggingface_hub.login()` (~/.cache/huggingface/token).
// The token is used only here (Node); it never reaches the browser bundle.
//
// Output: public/data/index.json + public/data/<id>.json (gitignored; dataset is research-use only).
//
// Usage:
//   npm run fetch:ai-village                         # --auto: curated + scan-detected windows
//   npm run fetch:ai-village -- --list-goals
//   npm run fetch:ai-village -- --goal 33 --hours 4  # one window from a village goal
//   npm run fetch:ai-village -- --from 2026-03-05T17:00Z --to 2026-03-05T21:00Z
//
// Small tables are cached in .hf/ (gitignored). computer_use_turns (~2.5 GB) is streamed once per
// run and only the rows for the selected sessions are kept (cached as .hf/turns-slice-<hash>.jsonl).

import { createReadStream, createWriteStream, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { PassThrough, Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { createGunzip } from 'node:zlib';
import {
  adaptAiVillageWindow, classifyTurn, hfTime, urlsIn, CLAIM_RE, rawCommand, INTERESTING_CMD,
  type HfAgent, type HfChat, type HfEvent, type HfSession, type HfTurn,
} from '../src/adapters/aiVillageHf';
import { reconstruct } from '../src/engine/reconstruct';
import { runMonitors } from '../src/engine/monitors';

type Row = Record<string, unknown>;
const REPO = 'aidigestorg/ai-village';
const BASE = `https://huggingface.co/datasets/${REPO}/resolve/main`;
const ROOT = join(import.meta.dirname, '..');
const CACHE = join(ROOT, '.hf');
const OUT = join(ROOT, 'public', 'data');
const LOOKBACK_MS = 12 * 3600_000;

const args = process.argv.slice(2);
const flag = (name: string) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : undefined; };
const has = (name: string) => args.includes(`--${name}`);

function fail(msg: string): never { console.error(`\n✗ ${msg}\n`); process.exit(1); }
const log = (...a: unknown[]) => console.log(...a);

function token(): string {
  const env = process.env.HF_TOKEN ?? process.env.HUGGING_FACE_HUB_TOKEN;
  if (env) return env.trim();
  for (const p of [join(homedir(), '.cache/huggingface/token'), join(homedir(), '.huggingface/token')]) {
    if (existsSync(p)) { const t = readFileSync(p, 'utf8').trim(); if (t) return t; }
  }
  fail('No Hugging Face token found. Log in with huggingface_hub (or set HF_TOKEN) after your access request is approved.');
}

async function open(file: string): Promise<NodeJS.ReadableStream> {
  const res = await fetch(`${BASE}/${file}`, { headers: { Authorization: `Bearer ${token()}` } });
  if (res.status === 401 || res.status === 403) {
    fail(`${res.status} on ${file}: token invalid or access to ${REPO} not approved yet (https://huggingface.co/datasets/${REPO}).`);
  }
  if (!res.ok || !res.body) fail(`HTTP ${res.status} fetching ${file}`);
  return Readable.fromWeb(res.body as import('node:stream/web').ReadableStream);
}

/**
 * Resumable download: yields raw bytes, and on a dropped connection re-requests from the
 * current offset with an HTTP Range header (the HF CDN supports ranges). Up to 10 retries.
 */
async function* resumable(file: string): AsyncGenerator<Buffer> {
  let offset = 0;
  let attempt = 0;
  for (;;) {
    try {
      const headers: Record<string, string> = { Authorization: `Bearer ${token()}` };
      if (offset) headers.Range = `bytes=${offset}-`;
      // Stall watchdog: abort if headers take >30s or no bytes arrive for 45s; the catch resumes via Range.
      const ctrl = new AbortController();
      let timer = setTimeout(() => ctrl.abort(new Error('no response headers in 30s')), 30_000);
      const arm = () => { clearTimeout(timer); timer = setTimeout(() => ctrl.abort(new Error('stalled: no bytes for 45s')), 45_000); };
      let res: Response;
      try { res = await fetch(`${BASE}/${file}`, { headers, signal: ctrl.signal }); } catch (e) { clearTimeout(timer); throw e; }
      arm();
      if (res.status === 401 || res.status === 403) fail(`${res.status} on ${file}: token invalid or access not approved.`);
      if (offset && res.status !== 206) throw new Error(`server ignored Range (HTTP ${res.status})`);
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      const range = res.headers.get('content-range');
      const total = range ? Number(range.split('/')[1]) : offset + Number(res.headers.get('content-length') ?? NaN);
      try {
        for await (const chunk of res.body as unknown as AsyncIterable<Uint8Array>) {
          arm();
          offset += chunk.byteLength;
          attempt = 0;
          yield Buffer.from(chunk);
        }
      } finally {
        clearTimeout(timer);
      }
      // A clean end before the full length is still a drop: resume with Range.
      if (Number.isFinite(total) && offset < total) throw new Error(`stream ended early at ${offset} of ${total} bytes`);
      return;
    } catch (err) {
      if (++attempt > 10) throw err;
      const wait = Math.min(30_000, 1000 * 2 ** attempt);
      if ((err as Error).name === 'AbortError' && !(err as Error).message) (err as Error).message = 'aborted';
      process.stdout.write(`\n  connection dropped at ${(offset / 1e9).toFixed(2)} GB (${(err as Error).message}); resuming in ${wait / 1000}s…\n`);
      await new Promise((r) => setTimeout(r, wait));
    }
  }
}

/** Ensure a small table is cached locally; returns its path. */
async function cached(file: string): Promise<string> {
  const p = join(CACHE, file);
  if (existsSync(p)) return p;
  mkdirSync(CACHE, { recursive: true });
  log(`  downloading ${file}…`);
  await pipeline(await open(file), createWriteStream(`${p}.part`));
  await import('node:fs/promises').then((fs) => fs.rename(`${p}.part`, p));
  return p;
}

/** Gunzip a byte source; errors anywhere (download, truncated gzip) reject the line iterator. */
function gunzipped(source: AsyncIterable<Buffer>): NodeJS.ReadableStream {
  const out = new PassThrough();
  pipeline(Readable.from(source), createGunzip(), out).catch((err) => out.destroy(err as Error));
  return out;
}

async function* lines(path: string): AsyncGenerator<string> {
  const input = path.endsWith('.gz') ? gunzipped(createReadStream(path)) : createReadStream(path);
  for await (const line of createInterface({ input, crlfDelay: Infinity })) if (line) yield line;
}

async function readAll<T = Row>(file: string): Promise<T[]> {
  const out: T[] = [];
  for await (const l of lines(await cached(file))) out.push(JSON.parse(l) as T);
  return out;
}

const RAW_TS = /"created_at":\s*"([^"]+)"/;
/** Dataset timestamps sort lexicographically ("YYYY-MM-DD HH:MM:SS.ffffff", UTC). */
const rawTs = (ms: number) => new Date(ms).toISOString().replace('T', ' ').replace('Z', '');

// ---------------------------------------------------------------- window plan

interface Plan { id: string; label: string; goal?: string; from: number; to: number; highlight: string; why: string }

const DAY = (iso: string, h0 = 17, hours = 4) => {
  const from = Date.parse(`${iso}T${String(h0).padStart(2, '0')}:00:00Z`);
  return { from, to: from + hours * 3600_000 };
};

/** Curated slices of the village. Bounds are refined from .hf/candidates.json when present. */
const CURATED: (Omit<Plan, 'from' | 'to' | 'goal'> & { date: string; goalIndex: number })[] = [
  { id: 'rpg-saboteurs', date: '2026-03-05', goalIndex: 33, label: 'RPG build with saboteurs', highlight: 'Thirteen agents build an RPG together while hunting for planted "easter egg" saboteurs.', why: 'curated goal + scan candidates' },
  { id: 'game-testing', date: '2026-03-17', goalIndex: 34, label: 'Playtesting the RPG', highlight: 'A team playtesting its own game: test runs, pushes and bug reports.', why: 'curated goal' },
  { id: 'juice-shop', date: '2026-01-13', goalIndex: 26, label: 'OWASP Juice Shop hacking', highlight: 'Competitive OWASP Juice Shop hacking with shell-heavy sessions.', why: 'curated goal' },
  { id: 'interactive-worlds', date: '2026-04-27', goalIndex: 38, label: 'Building interactive worlds', highlight: 'Fifteen agents ship interactive worlds to GitHub Pages; every "live" announcement is checked against recorded curl results.', why: 'scan candidates' },
  { id: 'contribution-dashboard', date: '2026-02-16', goalIndex: 29, label: 'Contribution dashboard launch', highlight: 'A dashboard launch, including a "Live Demo" link posted with an explicit "may take a minute to deploy" caveat (not flagged).', why: 'scan candidates' },
  { id: 'assigned-goals', date: '2026-07-06', goalIndex: 50, label: 'Maximize your assigned goal', highlight: 'A recent, busy village: 22 agents, each maximising an individually assigned goal.', why: 'recent goal + scan candidates' },
];

interface Candidate { claim_ts: string; fail_ts: string; url: string; text: string }

function refine(date: string, cands: Candidate[]): { from: number; to: number } {
  const ms = (s: string) => Date.parse(hfTime(s));
  const day = cands
    .filter((c) => c.claim_ts.startsWith(date) && ms(c.claim_ts) - ms(c.fail_ts) < 4 * 3600_000)
    .sort((a, b) => a.claim_ts.localeCompare(b.claim_ts));
  if (!day.length) return DAY(date);
  // Densest 4h block containing failing check → claim pairs.
  let best = { from: 0, to: 0, n: -1 };
  for (const c of day) {
    const from = Math.floor((ms(c.fail_ts) - 20 * 60_000) / 900_000) * 900_000;
    const to = from + 4 * 3600_000;
    const n = day.filter((d) => ms(d.fail_ts) >= from && ms(d.claim_ts) < to).length;
    if (n > best.n) best = { from, to, n };
  }
  return { from: best.from, to: best.to };
}

// ---------------------------------------------------------------- verdict index

const VERDICTS = join(CACHE, 'verdicts.jsonl');
const AUTO_DAYS = 2;
const clipLabel = (s: string) => (s.length > 48 ? `${s.slice(0, 47).trimEnd()}…` : s);
/** Keep head and tail (status lines come first, test summaries last). */
const trim = (s: unknown, n: number) => (typeof s === 'string' ? (s.length > n ? `${s.slice(0, n / 2)}\n…[trimmed]…\n${s.slice(-n / 2)}` : s) : null);

/**
 * Every computer_use_turns row that carries a deterministic verdict, trimmed, in one local file.
 * Built with one resumable pass over the 2.5 GB table; later runs are offline.
 */
async function verdictIndex(): Promise<HfTurn[]> {
  if (!existsSync(VERDICTS) || has('refresh')) {
    log('→ building verdict index from computer_use_turns (~2.5 GB, one pass)…');
    // Resume an interrupted index: gzip can't be entered mid-stream, so we re-read from the start but
    // skip (without parsing) until the last row already written to the .part file.
    const part = `${VERDICTS}.part`;
    let resumeAfter: string | null = null;
    let kept = 0;
    if (existsSync(part) && !has('refresh')) {
      const text = readFileSync(part, 'utf8');
      const complete = text.slice(0, text.lastIndexOf('\n') + 1);
      writeFileSync(part, complete);
      const rows = complete.split('\n').filter(Boolean);
      kept = rows.length;
      if (rows.length) resumeAfter = (JSON.parse(rows[rows.length - 1]) as HfTurn).id;
      if (resumeAfter) log(`  resuming: ${kept.toLocaleString()} rows already indexed; skipping ahead to ${resumeAfter.slice(0, 8)}…`);
    }
    const out = createWriteStream(part, { flags: resumeAfter ? 'a' : 'w' });
    // Prefer the local copy (download with .hf/dl-turns.sh: curl with byte-level resume); stream only as a fallback.
    const localTurns = join(CACHE, 'computer_use_turns.jsonl.gz');
    const input = existsSync(localTurns) ? gunzipped(createReadStream(localTurns)) : gunzipped(resumable('computer_use_turns.jsonl.gz'));
    if (existsSync(localTurns)) log('  reading local .hf/computer_use_turns.jsonl.gz');
    let n = 0; let malformed = 0;
    for await (const l of createInterface({ input, crlfDelay: Infinity })) {
      if (++n % 200000 === 0) process.stdout.write(`\r  scanned ${n.toLocaleString()} turns, kept ${kept.toLocaleString()}${resumeAfter ? ' (skipping to resume point)' : ''}`);
      if (resumeAfter) {
        if (l.includes(resumeAfter)) resumeAfter = null;
        continue;
      }
      const cmd0 = rawCommand(l);
      if (!cmd0 || !INTERESTING_CMD.test(cmd0)) continue;
      let r: HfTurn;
      try { r = JSON.parse(l) as HfTurn; } catch { malformed++; continue; }
      if (!classifyTurn(r)) continue;
      const cmd = typeof r.agent_action?.command === 'string' ? r.agent_action.command : '';
      out.write(`${JSON.stringify({ id: r.id, session_id: r.session_id, created_at: r.created_at,
        agent_action: { command: trim(cmd, 1500) }, output: trim(r.output, 4000), error: trim(r.error, 1500) })}\n`);
      kept++;
    }
    await new Promise<void>((res) => out.end(res));
    await import('node:fs/promises').then((fs) => fs.rename(part, VERDICTS));
    log(`\r  scanned ${n.toLocaleString()} turns; indexed ${kept.toLocaleString()} verdict-bearing turns → .hf/verdicts.jsonl${malformed ? ` (${malformed} malformed lines skipped)` : ''}`);
  }
  const rows: HfTurn[] = [];
  for await (const l of lines(VERDICTS)) rows.push(JSON.parse(l));
  return rows;
}

/** Monitor-A candidates using the adapter's own rules (claim filters, URL normalisation, verdicts). */
async function findCandidates(verdicts: HfTurn[]): Promise<Candidate[]> {
  const checks = new Map<string, { ts: string; pass: boolean }[]>();
  for (const t of verdicts) {
    const v = classifyTurn(t);
    if (v?.rule !== 'http-status' || !v.subject) continue;
    const list = checks.get(v.subject.artifact) ?? [];
    list.push({ ts: hfTime(t.created_at), pass: v.outcome === 'pass' });
    checks.set(v.subject.artifact, list);
  }
  for (const l of checks.values()) l.sort((a, b) => a.ts.localeCompare(b.ts));
  const NEG = /\b(404|not found|broken|fail(?:ed|ing|s|ure)?|errors?|is down|are down|isn['’]?t live|not (?:yet )?live|still (?:returning|showing)|unreachable)\b/i;
  const out: Candidate[] = [];
  for await (const l of lines(await cached('chat_messages.jsonl.gz'))) {
    if (!l.includes('"agent"') || !l.includes('http')) continue;
    const c = JSON.parse(l) as HfChat;
    const text = c.content ?? '';
    if (c.speaker_type !== 'agent' || !CLAIM_RE.test(text) || NEG.test(text)) continue;
    const urls = urlsIn(text).filter((u) => !/^https:\/\/(www\.)?(github|gitlab)\.com(\/|$)/.test(u));
    if (!urls.length || urls.length > 2) continue;
    const ts = hfTime(c.created_at);
    for (const url of urls) {
      const prior = (checks.get(url) ?? []).filter((k) => k.ts < ts);
      const lastFail = [...prior].reverse().find((k) => !k.pass);
      if (!lastFail || prior.some((k) => k.pass && k.ts > lastFail.ts)) continue;
      if (Date.parse(ts) - Date.parse(lastFail.ts) > 4 * 3600_000) continue;
      out.push({ claim_ts: ts.replace('T', ' ').replace('Z', ''), fail_ts: lastFail.ts.replace('T', ' ').replace('Z', ''), url, text: text.slice(0, 200) });
    }
  }
  return out;
}

// ---------------------------------------------------------------- main

async function main() {
  mkdirSync(CACHE, { recursive: true });
  log(`→ ${REPO}`);
  const goals = (await readAll<Row>('village_goals.jsonl.gz'))
    .map((g) => ({ goal: String(g.goal ?? ''), start: Date.parse(hfTime(String(g.start_time ?? g.created_at))), end: g.end_time ? Date.parse(hfTime(String(g.end_time))) : null }))
    .sort((a, b) => a.start - b.start);
  const goalAt = (t: number) => [...goals].reverse().find((g) => g.start <= t)?.goal;

  if (has('list-goals')) {
    goals.forEach((g, i) => log(`${String(i).padStart(3)}  ${new Date(g.start).toISOString().slice(0, 16)}  ${g.goal.slice(0, 100)}`));
    return;
  }

  const verdicts = await verdictIndex();

  let plans: Plan[];
  if (flag('from') && flag('to')) {
    const from = Date.parse(flag('from')!); const to = Date.parse(flag('to')!);
    if (Number.isNaN(from) || Number.isNaN(to)) fail('Could not parse --from/--to.');
    plans = [{ id: 'custom', label: 'Custom window', from, to, goal: goalAt(from), highlight: '', why: 'cli' }];
  } else if (flag('goal') !== undefined) {
    const sorted = goals; // same order as --list-goals
    const g = sorted[Number(flag('goal'))];
    if (!g) fail(`No goal #${flag('goal')}. Use --list-goals.`);
    const from = g.start;
    plans = [{ id: `goal-${flag('goal')}`, label: g.goal.slice(0, 60), from, to: from + Number(flag('hours') ?? 4) * 3600_000, goal: g.goal, highlight: '', why: 'cli' }];
  } else {
    const cands = await findCandidates(verdicts);
    writeFileSync(join(CACHE, 'candidates-v2.json'), JSON.stringify(cands, null, 1));
    const perDay = new Map<string, number>();
    for (const c of cands) perDay.set(c.claim_ts.slice(0, 10), (perDay.get(c.claim_ts.slice(0, 10)) ?? 0) + 1);
    log(`  ${cands.length} candidate incidents across ${perDay.size} days (same rules as the adapter)`);
    const curatedDays = new Set(CURATED.map((c) => c.date));
    const topDays = [...perDay.entries()].filter(([d]) => !curatedDays.has(d)).sort((a, b) => b[1] - a[1]).slice(0, AUTO_DAYS);
    const all = [
      ...CURATED,
      ...topDays.map(([date, n]) => ({
        id: `incidents-${date}`, date, goalIndex: -1, label: 'Claims after failing checks',
        highlight: `${n} candidate claim${n > 1 ? 's' : ''} of a page being live after a failing check of that page.`, why: 'auto: candidates',
      })),
    ];
    plans = all.map((c) => {
      const { from, to } = refine(c.date, cands);
      const g = goalAt(from);
      const label = c.goalIndex === -1 && g ? clipLabel(g) : c.label;
      return { id: c.id, label, goal: g ?? goals[c.goalIndex]?.goal, from, to, highlight: c.highlight, why: c.why };
    });
  }

  const minFrom = Math.min(...plans.map((p) => p.from)) - LOOKBACK_MS;
  const maxTo = Math.max(...plans.map((p) => p.to));
  const lo = rawTs(minFrom); const hi = rawTs(maxTo);
  const inAny = (raw: string, lookback: boolean) => plans.some((p) => raw >= rawTs(p.from - (lookback ? LOOKBACK_MS : 0)) && raw < rawTs(p.to));
  for (const p of plans) log(`  window ${p.id}: ${new Date(p.from).toISOString().slice(0, 16)} → ${new Date(p.to).toISOString().slice(11, 16)} UTC`);

  const agents = await readAll<HfAgent>('agents.jsonl.gz');

  log('→ scanning computer_use_sessions…');
  const sessions: HfSession[] = [];
  for await (const l of lines(await cached('computer_use_sessions.jsonl.gz'))) {
    const ts = RAW_TS.exec(l)?.[1];
    if (ts && ts >= lo && ts < hi && inAny(ts, true)) sessions.push(JSON.parse(l));
  }
  log(`  ${sessions.length} sessions in range`);

  log('→ scanning events (session boundaries)…');
  const boundaries: HfEvent[] = [];
  const BOUNDARY = /"actionType":\s*"(START_USING_COMPUTER|STOP_USING_COMPUTER|CONSOLIDATE)"/;
  for await (const l of lines(await cached('events.jsonl.gz'))) {
    if (!BOUNDARY.test(l)) continue;
    const ts = RAW_TS.exec(l)?.[1];
    if (!ts || ts < lo || ts >= hi || !inAny(ts, true)) continue;
    const e = JSON.parse(l) as HfEvent;
    delete (e.data as Row).output; // raw model output: large and not used
    boundaries.push(e);
  }
  log(`  ${boundaries.length} boundary events`);

  log('→ scanning chat_messages…');
  const chats: HfChat[] = [];
  for await (const l of lines(await cached('chat_messages.jsonl.gz'))) {
    const ts = RAW_TS.exec(l)?.[1];
    if (ts && ts >= lo && ts < hi && inAny(ts, false)) chats.push(JSON.parse(l));
  }
  log(`  ${chats.length} chat messages`);

  const wanted = new Set(sessions.map((s) => s.id));
  const turns = verdicts.filter((t) => wanted.has(t.session_id));
  log(`→ ${turns.length} verdict-bearing turns in selected sessions (from local index)`);

  // ---- build + validate each window
  mkdirSync(OUT, { recursive: true });
  const generatedAt = new Date().toISOString();
  const index: Row[] = [];
  for (const p of plans) {
    const fromIso = new Date(p.from).toISOString(); const toIso = new Date(p.to).toISOString();
    const pFrom = rawTs(p.from - LOOKBACK_MS); const pTo = rawTs(p.to);
    const ses = sessions.filter((s) => s.created_at >= pFrom && s.created_at < pTo);
    const sid = new Set(ses.map((s) => s.id));
    const input = {
      id: p.id,
      label: `${new Date(p.from).toUTCString().slice(5, 16)} · ${p.label}`,
      goal: p.goal,
      window: { from: fromIso, to: toIso },
      agents,
      sessions: ses,
      boundaries: boundaries.filter((b) => b.created_at >= pFrom && b.created_at < pTo),
      chats: chats.filter((c) => c.created_at >= rawTs(p.from) && c.created_at < pTo),
      turns: turns.filter((t) => sid.has(t.session_id)),
      generatedAt,
      rows: {} as Record<string, number>,
    };
    input.rows = {
      computer_use_sessions: input.sessions.length, events: input.boundaries.length,
      chat_messages: input.chats.length, computer_use_turns: input.turns.length,
    };
    const doc = adaptAiVillageWindow(input);
    const last = doc.events.length ? doc.events[doc.events.length - 1].sequence : 0;
    const ws = reconstruct({ events: doc.events, withheld: new Set(), agents: doc.agents }, last);
    const findings = runMonitors(ws);
    writeFileSync(join(OUT, `${p.id}.json`), JSON.stringify(doc));
    const counts = {
      events: doc.events.length,
      tasks: ws.tasks.size,
      agents: doc.agents.length,
      claims: ws.claims.size,
      toolResults: doc.events.filter((e) => e.type === 'tool_result').length,
      findingsActive: findings.filter((f) => f.state === 'active').length,
      findingsTotal: findings.length,
    };
    // Highlights must describe what the analysis actually found in this build, not what we expected.
    const active = findings.filter((f) => f.state === 'active');
    const highlight = findings.length
      ? `${findings.length} finding${findings.length > 1 ? 's' : ''}${active.length ? ` (${active.length} active)` : ', all resolved'}: ${(active[0] ?? findings[0]).summary}`
      : p.why.startsWith('auto') ? 'No findings with the current rules.' : p.highlight;
    // Auto-picked windows are named after what they contain, not just the village goal.
    const subjectHost = (f: (typeof findings)[number]) => {
      const a = ws.claims.get(f.claimId)?.subject?.artifact ?? '';
      return a.replace(/^https?:\/\//, '').split('/')[0].split('.')[0].replace(/-[0-9a-f]{6}$/, '');
    };
    const label = p.why.startsWith('auto') && findings.length
      ? `${new Date(p.from).toUTCString().slice(5, 16)} · "Live" announcements vs checks (${subjectHost(active[0] ?? findings[0])})`
      : input.label;
    index.push({ id: p.id, file: `${p.id}.json`, label, goal: p.goal, window: input.window, counts, highlight });
    if (label !== input.label) {
      doc.label = label;
      writeFileSync(join(OUT, `${p.id}.json`), JSON.stringify(doc));
    }
    log(`\n✓ ${p.id}: ${counts.events} events · ${counts.tasks} tasks · ${counts.agents} agents · ${counts.claims} claims · ${counts.toolResults} verdicts · findings ${counts.findingsTotal} (${counts.findingsActive} active)`);
    for (const f of findings) {
      const claimEv = ws.byId.get(f.evidence.find((x) => x.role.startsWith('Completion'))?.eventId ?? f.detectedEventId);
      const failEv = ws.byId.get(f.evidence[0]?.eventId ?? '');
      log(`  [${f.monitor} · ${f.state}] ${f.summary}`);
      if (claimEv) log(`     claim  ${claimEv.id} @ ${claimEv.timestamp.slice(11, 19)}: ${claimEv.text.slice(0, 160).replace(/\n/g, ' ')}`);
      if (failEv && failEv.type === 'tool_result') log(`     check  ${failEv.id} @ ${failEv.timestamp.slice(11, 19)}: ${failEv.text.slice(0, 160)}`);
    }
  }
  // Most informative first: active findings, then any findings; curated order otherwise (stable sort).
  const score = (r: Row) => { const c = r.counts as Record<string, number>; return c.findingsActive * 1000 + c.findingsTotal; };
  index.sort((a, b) => score(b) - score(a));
  writeFileSync(join(OUT, 'index.json'), JSON.stringify({ generatedAt, dataset: REPO, citation: 'AI Digest, AI Village dataset', sources: index }, null, 1));
  log(`\n✓ wrote public/data/index.json (${index.length} sources)`);
}

main().catch((e) => fail(e instanceof Error ? e.stack ?? e.message : String(e)));
