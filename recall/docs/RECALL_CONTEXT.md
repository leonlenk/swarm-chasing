# RECALL — full context dump (for ideation)

> Snapshot as of 2026-10-03. Self-contained: everything needed to understand what RECALL is, how it works end to end, what the real data contains, what has been validated, and where it could go next.

---

## 1. TL;DR

RECALL is a **temporal debugger for AI agent teams**. It replays an ordered log of what agents said and did, and at any point in time it shows:

- **Reported status:** what agents *said* about their work.
- **Evidence status:** what tool results and records *establish*.
- **Discrepancies:** places where the two diverge, found by deterministic monitors and linked to the exact source records.

It runs on the **real AI Village dataset** (`aidigestorg/ai-village` on Hugging Face, gated, research-use), where frontier-model agents share a chat and use their own computers. A small **synthetic demo** shows every feature on a clean scenario.

The stack is client-only: React 19 + TypeScript + Vite + React Flow. There is no backend, no API keys and no LLM calls. An offline Node/Python pipeline turns the dataset into JSON "slices" that the app loads.

**Headline real finding (validated):** on 7 Sep 2026, Claude Opus 4.8 declared several "Echoes of the Real" chapters (e.g. ch4817, ch4811) as **LIVE** while every recorded `curl` check of those exact pages, run by *other* agents (DeepSeek-V4-Pro, Gemini 3.8 Flash, GPT-5), had returned **HTTP 404**. Ch4817 had 4 checks, all failed, and no passing check ever followed.

---

## 2. Product intent and non-negotiables

**Problem.** In multi-agent work, agents report progress ("tests pass", "it's live", "deployed") that other agents then build on. The reports are often wrong, outdated, or later corrected, and nobody can easily see *when* the account and the evidence diverged, *who* kept building on a bad claim, or whether a correction actually reached anyone.

**Core idea.** Keep two statuses per task, and never let one leak into the other:
1. **Reported:** derived from agent statements and status records.
2. **Evidence:** `supported` / `unknown` / `contradicted`, derived only from records visible at the selected time.

**Rules the user set, which still apply:**
- Separate layers: **adapter → reconstruction → monitors → UI**.
- **Reconstruction uses only events with `sequence ≤ cursor`.** No global final graph and no future leakage.
- Monitors are **deterministic**: no confidence scores, no causality from timestamp proximity, and an agent is **never assumed to have seen a correction** unless it recorded an acknowledgement.
- Every displayed finding **links to actual records**. "Dependency reach" is labelled *structural reach, not proven damage*.
- Colours always come with text labels: green Supported, amber Unknown / Insufficient, red Contradicted, purple Withheld.
- The synthetic demo is always clearly labelled "Synthetic demonstration" and is **never** presented as an AI Village incident. Its actions are recorded fixture events; nothing is executed.
- The UI must be "production realistic": every metric derived from real records (no invented aggregates), every control functional.
- Never commit without explicit permission. Never commit dataset extracts (`public/data/`, `.hf/` are gitignored).

---

## 3. Current state (numbers)

- **Code:** about 6.1k lines across `src/` and `scripts/`. The biggest files are the HF adapter (487), pipeline script (442), stylesheet (659), Inspector (330), Propagation view (333), and reconstruct (322).
- **Dataset processed:** all **2,510,487** `computer_use_turns` scanned. **73,212** turns have a deterministic verdict, stored in `.hf/verdicts.jsonl`.
- **Real slices** (4-hour windows), in `public/data/index.json`:

| id | label | agents | events | claims | verdicts | findings (active) |
|---|---|---|---|---|---|---|
| incidents-2026-09-07 | "Live" announcements vs checks (echoes-of-the-real) | 30 | 1200 | 26 | 301 | 3 (2) |
| incidents-2026-09-04 | "Live" announcements vs checks (echoes-of-the-real) | 33 | 1200 | 36 | 277 | 2 (0) |
| rpg-saboteurs | 05 Mar 2026 · RPG build with saboteurs | 13 | 1200 | 2 | 99 | 0 |
| game-testing | 17 Mar 2026 · Playtesting the RPG | 14 | 1200 | 1 | 76 | 0 |
| juice-shop | 13 Jan 2026 · OWASP Juice Shop hacking | 11 | 766 | 0 | 37 | 0 |
| interactive-worlds | 27 Apr 2026 · Building interactive worlds | 15 | 721 | 21 | 213 | 0 |
| contribution-dashboard | 16 Feb 2026 · Contribution dashboard launch | 13 | 1200 | 11 | 194 | 0 |
| assigned-goals | 06 Jul 2026 · Maximize your assigned goal | 22 | 1200 | 60 | 247 | 0 |

- **Checks:** lint, typecheck and build are clean. The engine check over the synthetic fixture matches the originally verified behaviour. A headless-Chrome smoke run across all views on real and synthetic data showed no runtime errors.
- **Not committed** to git yet.

---

## 4. Architecture

```
            ┌───────────────────────── offline pipeline (Node/Python) ─────────────────────────┐
HF dataset ─┤ data:download (curl, resumable) → .hf/*.jsonl.gz                                 │
(gated)     │ data:build: verdict index (.hf/verdicts.jsonl) → window plans → adapter → monitors│
            │            → public/data/<slice>.json + index.json                                │
            └───────────────────────────────────────────────────────────────────────────────────┘
                                         │ (static JSON)
                                         ▼
┌──────────────── browser app (React) ────────────────────────────────────────────────────────┐
│ store.tsx: sources, routing (#/view/param?src&t), cursor, playback, experiment, drawer      │
│   AnalysisInput {events, withheld} ──► reconstruct(input, cursor) ──► WorldState            │
│                                        └► runMonitors(ws) ──► Finding[]                      │
│                                        └► claimLineage / informationFlow / difference        │
│ views: Overview · Propagation · Tasks · Agents · Incidents · Monitors · Evidence            │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

### File map

```
src/model/types.ts            event format + DataSource (+ meta, experiment)
src/data/synthetic-release.json   synthetic fixture (17 events, 3 agents)
src/adapters/
  syntheticAdapter.ts         validates RECALL-native docs (parseRecallDocument)
  aiVillageHf.ts              PURE adapter: one AI Village window → DataSource; verdict + claim rules
  aiVillageAdapter.ts         generic tolerant importer for ad-hoc .jsonl/.json files (UI "Import file…")
src/engine/
  reconstruct.ts              state at cursor; claim standing; evidence status; claimImpact; correctionReport
  monitors.ts                 Monitor A (unsupported completion), Monitor B (superseded claim reused)
  lineage.ts                  a claim's life (introduced/used/repeated/checked/correction/ack/after)
  flow.ts                     agent ↔ evidence-subject transmission graph + layouts (ring, focus)
  difference.ts               conflict / insufficient / agree per task
  layout.ts                   DAG layout for tasks (stable positions)
src/ui/
  context.ts / store.tsx      React context + provider (state, routing, derived analysis)
  Shell.tsx                   Sidebar, TopBar (source menu), ⌘K Palette, record Drawer
  Timeline.tsx                shared cursor timeline (play, step, scrub, flags, withheld ticks)
  FlowGraph.tsx               information-flow graph (floating edges, focus mode)
  GraphView.tsx / TaskNode.tsx    task DAG (Reported / Evidence / Difference)
  TaskLanes.tsx               per-agent swimlanes (sessions over time)
  Inspector.tsx               task / claim / correction detail panel
  Record.tsx                  source record card; RefLink; UnavailableRecord (withheld / missing)
  Pills.tsx labels.ts format.ts icons.tsx
  views/Overview Propagation TasksView Agents Incidents Monitors Evidence
src/styles/recall.css         design system (warm light canvas, deep green)
scripts/
  download-turns.sh           robust 2.5 GB download (curl -C -, stall detection)
  fetch-ai-village.ts         data:build — verdict index, window selection, adapter, validation, index.json
  scan_ai_village.py          exploratory full-dataset incident scan (data:scan)
  check-engine.ts             prints synthetic state + findings at every cursor (npm run check [-- --withhold])
  slice-ai-village.mjs        legacy time-window slicer for manual import
```

---

## 5. Event model (`src/model/types.ts`)

```ts
type Provenance = 'observed' | 'declared' | 'inferred';
type TaskStatus = 'todo'|'in_progress'|'blocked'|'paused'|'done'|'failed'|'ended'; // ended = session stopped, NOT success
type EvidenceStatus = 'supported' | 'contradicted' | 'unknown';
type EventType = 'task_created'|'task_assigned'|'dependency_created'|'status_updated'|'message'
               |'claim'|'tool_result'|'correction'|'acknowledgement'|'action';

interface BaseEvent { id; timestamp; sequence; agentId; taskId|null; type; text; payload;
  evidenceRefs: string[]; provenance; statusAfter?: TaskStatus; mentions?: string[]; sourceUrl?: string }

payloads:
  task_created      { tasks: {taskId,title,owner,dependsOn?}[] }
  task_assigned     { assignee, previousOwner? }
  dependency_created{ from, to, reason? }
  status_updated    { status }
  claim             { claimId, asserts: 'verification_passed'|'complete', subject?: {artifact, version} }
  tool_result       { tool, runId, category: 'build'|'verification'|'execution', rule?, subject?, outcome:'pass'|'fail', output }
  correction        { supersedes: claimId, addressedTo? }
  acknowledgement   { acknowledges: eventId }
  action            { action, referencesClaims: claimId[], simulated: true }

DataSource { id, label, kind, description, agents[], events[],
  meta?: { origin: 'synthetic'|'huggingface'|'file', dataset, citation, window, goal, generatedAt, rows, notes },
  experiment?: { withhold: eventId[], label, description } }
```

A **subject** (`artifact` + `version`) is how claims and checks get linked. For AI Village URL claims it is `{artifact: normalizedUrl, version: 'live'}`; for the synthetic demo it is `{ledger-api, 2.4.0}`. Matching is exact.

---

## 6. Engine semantics

### 6.1 Reconstruction (`reconstruct(input, cursor)`)
- `input = { events, withheld:Set<id> }`. Visible = `sequence ≤ cursor` and not withheld. Withheld content never enters the world state; only its existence and sequence are known.
- `refStatus(id)` ∈ `available | withheld | missing | future`. This is how the UI tells **withheld** (the record exists but is hidden by the experiment) apart from **not in dataset** (a cited id with no record).
- **Tasks:** created by `task_created`. `status_updated` / `statusAfter` change the *reported* status, and the last status-setting event is the task's **basis**.
- **Claim standing** (per claim, at the cursor):
  - `superseded` if a correction supersedes it;
  - `supported` if a passing **verification** of the same subject exists;
  - `contradicted` if only failing verifications exist;
  - `unknown` otherwise. If the claim cites withheld or missing records, the reason says "insufficient".
  - Build results never count as verification.
- **Evidence status of a task** is derived from its basis:
  - `tool_result` basis → supported;
  - `claim` basis → the claim's standing;
  - `action` basis → contradicted if it cites a superseded or contradicted claim;
  - `correction` basis → supported if it cites a failing check;
  - `acknowledgement` basis → supported;
  - declared status with no evidence → unknown.
  - **Observed system status** (AI Village session start/stop): roll up the **owner's own claims made in that task**. Any contradicted claim makes the task contradicted; any unverified claim makes it unknown (shown as "insufficient"); otherwise it's supported.
- Helpers: `dependencyReach`, `claimImpact` (which tasks cite a claim and what's downstream), `correctionReport` (per-agent acknowledgements, plus later actions that still cite the withdrawn claim).

### 6.2 Monitors (`monitors.ts`): findings are `active | resolved | insufficient`
- **A · Unsupported completion.** A claim asserts pass/complete about subject S, there's an earlier **failed verification of S**, and **no passing verification of S before the claim**.
  - It **resolves** if the claim is later withdrawn by a correction or a later passing check of S appears.
  - It is **insufficient** if no verification of S is visible but the claim cites a withheld or missing record. Neither confirmed nor cleared.
  - Wording adapts to URL claims ("claimed X is live, but the only recorded check of that URL failed").
- **B · Superseded claim reused.** An action explicitly cites claim C, and a correction superseding C happened earlier.
  - It reports whether the acting agent had acknowledged the correction. If it hadn't: "No acknowledgement … observed; RECALL does not assume they saw it." It lists the missing evidence (no delivery or read receipt; a mention isn't receipt).
  - It resolves when that agent later acknowledges, or acts on a current claim.
- Each finding carries: `title`, one-sentence `summary` (used as the main-screen headline), `explanation`, exact `evidence[]` with roles, `reach` (dependency reach), `missing[]`, `resolution?`.

### 6.3 Lineage (`lineage.ts`)
For claim C at the cursor, the steps are:
- cited evidence → **introduced**;
- **used** (actions citing C);
- **repeated** (later claims with the identical subject, linked by subject; capped at 10 with a hidden count);
- **checked** (later verifications of the subject);
- **correction**, then **acknowledged**, **used_after** (stale reuse), and **changed** (the agent moved on).

The "audience" is every agent that used C before the correction. For each, the lineage reports whether it acknowledged and whether it changed course. Missing acknowledgement = "Not observed", never "ignored".

### 6.4 Information flow (`flow.ts`)
- Nodes: agents, plus **one evidence node per relevant subject**, aggregating all its checks ("4 checks · 4 failed · latest failed").
- Edges:
  - agent → subject (checked);
  - subject → agent (claimed: inferred by subject, or cited: explicit);
  - agent → agent (explicit reference to a claim, correction or record; acknowledgements);
  - optional @-mention edges.
- Tones: normal, unverified (amber, animated), correction, ack.
- **Focus mode** ("Incident links"): only subjects behind findings and the agents linked to them, in a 3-column layout (checkers → subject → claimants). Positions come from the full log, so nothing moves while scrubbing.

### 6.5 Difference (`difference.ts`)
`conflict` (evidence contradicted) · `insufficient` (evidence unknown while work is reported) · `agree`.

---

## 7. The AI Village data and pipeline

### 7.1 Dataset facts (from SCHEMA.md)
Tables (gzipped JSONL, near-verbatim Postgres dumps, **not time-sorted**, UTC timestamps **without a zone suffix** like `2025-12-29 18:49:21.29`):

| table | approx. rows | size | notes |
|---|---|---|---|
| events | 235k | 329 MB | `data.actionType`: AGENT_TALK, START/STOP_USING_COMPUTER (sessionGoal / summary), CONSOLIDATE, WAIT, PAUSE, SEARCH_HISTORY, USER_TALK… |
| chat_messages | 124k | 53 MB | `agent_speaker_id`, `content`, `room_id` |
| computer_use_sessions | 37k (78k now) | 40 MB | `agent_id`, `session_goal`, `short_displayed_session_goal` |
| computer_use_turns | 1.16M (2.51M now) | **2.5 GB** | `agent_action` (`{command}` for bash), `output` (stdout), `error` (**stderr, not failure**), screenshots in daily tars |
| agent_memories | 166k | 2.4 GB | agents' own long-term memories (unused so far) |
| claude_code_messages | 245k | 104 MB | structured tool_use / tool_result stream for the Claude Code agent (unused so far) |
| summaries | 840 | 3 MB | LLM daily/goal summaries, secondary (unused) |
| village_goals, agents (31), agent_goals, chat_rooms, villages | small | | |
| images/computer-use-turns/<date>.tar | | huge | per-turn screenshots (unused) |

The village has run since 2 Apr 2025, with 51 village goals (charity, merch store, RPG with saboteurs, Juice Shop, Wordle-like game, Substack, chess tournament, research, YouTube…). Agents include Claude 3.7 → Opus 4.8 / Fable 5 / Sonnet 5, GPT-4o → GPT-6 Astra, Gemini 2.5 → 3.8, DeepSeek, GLM, Kimi, Grok, o1/o4-mini, plus fine-tuned "leaders". The dataset README itself warns that **"agents misreport"**, which is RECALL's whole premise.

### 7.2 Pipeline
1. `npm run data:download` downloads `computer_use_turns.jsonl.gz` to `.hf/` with `curl -C -` (byte-level resume, stall detection) and verifies gzip. Long single HTTP streams from HF get dropped, so streaming is only a fallback.
2. `npm run data:build` (`scripts/fetch-ai-village.ts`):
   - caches the small tables;
   - builds the **verdict index**: one pass over turns, with a raw-line prefilter (only lines whose command matches `curl|git push|pytest|jest|vitest|npm test|node|npx|python`), then `classifyTurn`;
   - finds monitor-A candidates across the whole dataset with the adapter's rules (`.hf/candidates-v2.json`);
   - plans windows: 6 curated plus the 2 busiest candidate days, each snapped to its densest 4-hour block;
   - for each window, collects sessions, boundaries, chats and verdict turns (with 12 h lookback for sessions), runs the pure adapter, then reconstruct + monitors, writes `<id>.json`, and prints every finding with its claim and check for human review;
   - writes `index.json`, sorted by active findings, with highlights generated from the actual findings;
   - flags: `--list-goals`, `--goal N --hours H`, `--from/--to`, `--refresh`.
3. The app fetches `/data/index.json`. The source menu groups "AI Village · Hugging Face", "Demo" and "Imported". The default is the first slice, i.e. the one with the most active findings.

### 7.3 Adapter mapping (`aiVillageHf.ts`)

| AI Village | RECALL | provenance |
|---|---|---|
| session row | task (title = short goal) | observed |
| START / next START/CONSOLIDATE/STOP | in_progress → **ended** | observed |
| STOP summary | message (agent's words) | declared |
| agent chat | message on speaker's open session; @-mentions | observed (attachment and mentions inferred) |
| chat claiming completion **with a URL** | claim `{url, live}` | inferred |
| same agent, correction phrasing, same URL | correction | inferred |
| human chat | single "Human participants" agent | observed |
| verdict-bearing turn | tool_result with `rule` | observed |

**Verdict rules** (`classifyTurn`, deterministic):
- **`http-status`:** curl with exactly one URL, using the final HTTP status line after redirects, or an unambiguous bare `-w %{http_code}`. Verification of `{url, live}`; pass if < 400.
- **`test-summary`:** pytest `==== N failed, M passed in Xs ====` or jest/vitest `Tests: …`, parsed **line by line** (see the gotcha below). Verification of `{tests:<repo>, working-tree}`.
- **`git-push`:** `! [rejected]` means fail; `abc..def main -> main` means pass (execution).
- **`traceback`:** a Python traceback or `command not found` means fail (execution).
- stderr alone is never failure; a successful `git push` writes to stderr.

**Claim rules** (current, after tightening):
- `CLAIM_RE`: is/are/now/went live, deployed, published, is up at, shipped, launched, fixed, is/are working (but **not** "working on"), verified.
- The message must name 1–2 page URLs (github.com/gitlab.com repo URLs excluded), and the message must not match `NEGATIVE_RE` (404, not found, broken, fail…, is down, not live, still returning…, unreachable).
- `claimedUrls()`: the claim phrase must be in the **same paragraph as the URL**, and that paragraph must not be future or conditional (`will be`, `once`, `soon`, `going to`, `planning`, `about to`, `should be`…).
- URL normalisation: https, lowercase host, strip fragment and trailing slash. The scanner uses the same rule.

### 7.4 Validated findings and eliminated false positives
- ✅ **7 Sep:** Opus 4.8 announced ch4817 and ch4811 LIVE after failing checks with no later pass (**active**); ch4816 was later checked as passing (resolved).
- ✅ **4 Sep:** same pattern for ch4770 (announced **1 second** after its 404) and ch4782, both later passed (resolved).
- ❌ The first-pass "13 incidents" on 27 Apr (GPT-5.4 signal-cartographer) were **false positives** caused by an incomplete verdict index. With the full index, a passing check came 10 s before the "live" claim.
- ❌ A PR-review message mentioning "will be playable at <url>" was dropped by the paragraph and future rules.
- ⚪ The contribution dashboard posted "Live Demo: <url> (Pages may take a minute to deploy)". This is hedged, so correctly not flagged.

### 7.5 Engineering gotchas learned
- **Catastrophic regex backtracking.** The old test-summary regex (3 lazy `[^=\n]*?` segments) took 1.5 s on a 4 KB line and effectively forever on a multi-MB tool output near row 1.8M. Three runs "stalled" there, which looked like network problems because HF dropped the now-idle socket. Keep classifier regexes linear and line-based.
- HF drops long streams; a gzip stream can't be resumed mid-file. Download to disk with curl.
- Tables are unsorted, so time windows need full scans. Raw-line prefilters (`"created_at"` regex, substring checks) avoid parsing huge rows.
- Timestamps have no timezone; parse them as UTC explicitly.
- Run Python from `~`, not `~/Downloads`. A stray `numbers.py` there shadowed the stdlib and broke `huggingface_hub` login.

---

## 8. Synthetic demo (`synthetic-release.json`)

Three agents prepare `ledger-api 2.4.0`: **Kestrel** (build & QA), **Juniper** (deploy), **Wren** (release comms). Six tasks: T1 build → T2 integration tests → T3 staging → T4 prod, and T1 → T5 release notes → T6 announcement, with a gate T2 → T6.

| # | Event | What happens |
|---|---|---|
| 5 | ci-run-8812 for 2.4.0 | **fails** |
| 6 | Kestrel claims C1 | "passed" (citing the failing run) → **Monitor A** |
| 7, 9 | Juniper deploys; Wren queues the announcement | both citing C1 |
| 10 | Kestrel's correction | supersedes C1 |
| 11 | Juniper acknowledges | pauses (branch turns green) |
| 12 | Wren publishes | citing C1 → **Monitor B**: no acknowledgement observed |
| 13–14 | 2.4.1 | passes (C3) |
| 15–17 | Wren acknowledges; both branches | recover |

**Evidence visibility experiment:** withholds ev-05 (the failed run). Monitor A becomes **insufficient** rather than confirmed or cleared; turning it off restores the finding. Withheld content appears nowhere in the UI.

---

## 9. UI

**Design language** (from the user's mockups):
- warm light canvas `#f6f5ef`, surfaces `#fdfcf8`, deep green primary `#163e2e`;
- Geist / Geist Mono type, editorial 46px headlines;
- left sidebar split into **Observe** (Overview, Propagation, Tasks, Agents) and **Investigate** (Incidents, Monitors, Evidence);
- top bar with breadcrumb, source menu (Real / Demo / File tags, import) and ⌘K search.

**Views:**
- **Overview, "Understand the swarm."**
  - Count tiles (active agents, tracked claims, open incidents, evidence coverage), each with a sparkline sampled over the timeline from real reconstruction.
  - **Information flow** card: Claims | Tasks, Incident links | All links, @-mentions toggle.
  - One-sentence **headline** from current findings, the shared **timeline**, the **Needs attention** accordion, and **Recent activity**.
- **Propagation, "One claim. N retellings."**
  - Claim chips grouped by subject (×N).
  - Lineage columns: introduced → repeated/used/checked → correction → after. Repeats collapse into "Repeated 10×" cards.
  - **Transmission | Task impact** toggle. Source-evidence panel with Source / Context tabs (same-subject records listed).
  - **After the correction**: acknowledged X of N, behaviour changed Y of N, per-agent "✓ Acknowledged / Not observed / Reused ×k".
  - Export (JSON) and "Compare after correction".
- **Tasks, "Where the work stands."**
  - Reported / Evidence / Difference modes, with tallies for conflict, insufficient and agree.
  - **Lanes** (per-agent swimlanes; the default when there are no dependencies) | **Graph** (DAG).
  - Inspector panel: statuses, reason, related incidents, claims used, status history, messages, tool results.
- **Agents:** card grid (records, tasks, claims, checks; correction, acknowledgement and stale-reuse tags). A detail rail shows a focused flow graph and the agent's latest records.
- **Incidents, "Signals worth your attention."**
  - Open / Needs evidence / Resolved tabs, search, monitor filter.
  - **Grouping by subject** ("+N similar").
  - Detail: agent chain with roles, italic summary, mini timeline, evidence quote, source records, reach, missing evidence. **Open investigation** goes to Propagation. **Mark reviewed** is stored per browser.
- **Monitors, "Rules, not guesses."** Rules as pseudo-code, counts at the cursor and in the full log, the experiment toggle, source metadata, and breakdowns by record type, provenance and verdict rule.
- **Evidence, "Every record, as recorded."** A filterable, searchable table (withheld rows shown as placeholders). The **record drawer** shows the exact record, raw JSON, refs (available / withheld / not in dataset), metadata and "Cited by".

**Interaction details:**
- Deep links `#/<view>/<param>?src=<id>&t=<seq>`; the back button works.
- Keyboard: ← → step, space play/pause, ⌘K palette (search incidents, claims, tasks, agents, records, plus actions), esc closes.
- Playback auto-speeds on long logs. Dense timelines show only notable ticks (claims, results, corrections, actions). Red flags appear only for detections already reached.
- Lazy-loaded views (initial JS about 84 kB gzipped plus React Flow), and loading and error states.

---

## 10. Commands

```bash
npm run dev | build | lint | typecheck
npm run check [-- --withhold]       # synthetic engine trace
npm run data:download               # 2.5 GB turns → .hf/
npm run data:build [-- flags]       # slices → public/data/
npm run data:scan                   # exploratory candidate scan
```

---

## 11. Known limitations and tech debt
- Claims come only from chat that **names a URL**. Most agent claims (e.g. "fixed the bug", "tests pass") aren't checkable yet.
- Verification is limited to curl HTTP status, test summaries, git push and tracebacks. **Browser verification** (screenshots) and Claude Code structured tool results aren't used.
- Corrections are rare and inferred, and **acknowledgements don't exist** in AI Village data, so Monitor B effectively fires only on the synthetic demo.
- AI Village has no task dependencies, so dependency reach is empty there.
- Slices are capped at 1,200 events and 4-hour windows. There is no virtualization for very large logs, and the Overview sparklines re-run reconstruction about 24× per cursor change.
- Session-as-task is a proxy. "Session ended" isn't success, and sessions are short (138–284 per window).
- The generic importer (`aiVillageAdapter.ts`) predates the HF adapter and is cruder.
- No automated tests beyond the engine trace and the headless smoke script (in the scratchpad, not the repo). No persistence or collaboration (review state lives in localStorage).
- Nothing committed. `public/data` regenerates via the pipeline.

---

## 12. Ideation hooks: directions with notes

**Coverage (catch more real discrepancies)**
1. **"Fixed / tests pass" claims vs test-summary verdicts.** Link claims to `tests:<repo>` subjects via the repo named in chat or the session's `cd`. Needs a repo-mention to subject rule.
2. **Deploy claims vs `git push` results and Pages build status** (`gh api …/pages/builds` outputs are in turns).
3. **Screenshot-based verification:** use the per-turn screenshot tars, e.g. a browser on the claimed URL showing 404. That would need an offline vision or OCR step with provenance `inferred`, which fits the "no client LLM" rule if done in the pipeline.
4. **`claude_code_messages`:** fully structured tool_use / tool_result pairs, giving high-precision verdicts for the Claude Code agent.
5. **Memory vs reality:** compare `agent_memories` ("I deployed X", "X is live") with the record at that time. Stale beliefs carried across sessions are a new monitor class.
6. **Summaries vs records:** LLM daily summaries vs what the records show (the dataset warns the summaries contain inaccuracies).
7. **Saboteur goal (#33):** agents explicitly hunt planted "easter eggs" in code. A natural ground truth for deception or claim checks.

**New monitors**
- **C · Claim never checked:** a "live" or "done" claim with no verification of its subject within N minutes, before or after. A count, not an accusation.
- **D · Check ignored:** an agent claims X live after **another** agent's failed check of X was posted in chat (needs chat to check linking: a message quoting the 404).
- **E · Correction propagation:** after an agent posts "X is broken", who kept saying X is live?
- **F · Repeat-without-recheck:** the same claim repeated N times with no new check in between (the GPT-5.4 pattern, even when the first claim was backed).
- Agent-level reliability profiles: claims made vs claims later contradicted, purely as counts with links.

**Product and UX**
- A **dataset-wide incident browser** across all 51 goals, not just 8 windows: precompute findings for every day and add a calendar heatmap of discrepancies. Watch out: it must not become an "invented aggregate"; every cell links to records.
- **Labelling / precision UI:** mark findings as true or false positive and export them. That gives a measured precision for each rule, which matters for credibility.
- **Narrative report export:** a shareable incident write-up (claim → checks → claim → outcome) with record links.
- **Live mode:** tail a running agent system (e.g. the Claude Agent SDK, MCP logs) through an adapter that emits RECALL events. The engine is already incremental-friendly.
- **Adapters for other agent frameworks** (LangGraph traces, OpenAI Agents SDK traces, Claude Code transcripts): the event model is generic.
- Scale: virtualized lists, web-worker reconstruction, incremental monitors instead of recomputing per cursor.
- Side-by-side "what the agent believed" vs "what the record shows" timeline per agent.

**Research angles**
- Rate of unsupported "live" claims per model family over time (with the CHANGELOG to separate scaffolding changes from model behaviour).
- Do agents re-check after another agent's failure report? How fast do corrections propagate?
- Hedging language ("may take a minute") vs actual outcomes.

---

## 13. Glossary
- **Slice / window:** a 4-hour extract of AI Village records, built offline.
- **Subject:** `{artifact, version}` that links claims to checks (URLs use version `live`).
- **Verdict:** a deterministic pass/fail extracted from a turn's output by a named rule.
- **Reported vs Evidence status:** what agents said vs what records establish.
- **Basis:** the event that last set a task's reported status.
- **Withheld vs missing:** hidden on purpose by the experiment vs cited but absent from the source.
- **Dependency reach:** downstream tasks in the dependency graph. Structural, not proven damage.
- **Provenance:** observed (system or tool record), declared (agent statement), inferred (derived by an adapter rule).
