# RECALL

**A temporal debugger for AI agent teams.** RECALL replays what a team of agents said and did, and shows where an agent's account of its work diverges from what the records establish. Typical cases: a task claimed complete after a failed check, or an agent still acting on a claim that was already withdrawn.

It is built around the real [AI Village dataset](https://huggingface.co/datasets/aidigestorg/ai-village) (AI Digest), and also ships a small synthetic demo. Everything runs client-side: there is no backend, no API keys and no LLM calls. Analysis is deterministic.

```bash
npm install
npm run dev            # http://localhost:5173
```

---

## Using it

| View | Question it answers |
|---|---|
| **Overview** · *Understand the swarm.* | What is happening, and what needs attention right now? Shows count tiles computed from the log, the information flow between agents and evidence, a one-sentence headline, and the Needs-attention list. |
| **Propagation** · *One claim. N retellings.* | Where did a claim come from, who repeated or acted on it, what checked it, and what happened after a correction? |
| **Tasks** · *Where the work stands.* | What agents reported vs. what records establish, in **Reported / Evidence / Difference** modes. Shows swimlanes for sources without dependencies (AI Village sessions) and a dependency graph otherwise. |
| **Agents** | What each agent produced, with their claims, checks, corrections and acknowledgements. |
| **Incidents** · *Signals worth your attention.* | The monitor findings. Repeats of the same claim subject are grouped, and each finding links to its exact records. |
| **Monitors** · *Rules, not guesses.* | The rules as written, how much of the source each record type and provenance makes up, and the evidence visibility experiment. |
| **Evidence** | Every record, searchable and filterable. Opens the record drawer: the record, what it cites, and what cites it. |

All views share one **timeline cursor**. State and findings are reconstructed only from records at or before it. Keyboard: `←` `→` step, `space` play, `⌘K` command palette, `esc` close. URLs are deep links: `#/incidents/<id>?src=<source>&t=<seq>`.

**Colours always come with words:** green *Supported*, amber *Unknown* / *Insufficient evidence*, red *Contradicted*, purple *Withheld*.

---

## Data

### AI Village (primary) — huggingface.co/datasets/aidigestorg/ai-village

These are real records from AI Digest's AI Village, where frontier-model agents share a chat and use their own computers to pursue goals. The dataset is **gated (manual approval)** and released for research use. Extracts go to `public/data/` and caches to `.hf/`. **Both are gitignored and never committed.**

```bash
python3 -c "from huggingface_hub import login; login()"   # once, after access is approved (run from ~)
npm run data:download     # 2.5 GB computer_use_turns → .hf/ with curl byte-level resume (HF drops long streams)
npm run data:build        # verdict index + windows → public/data/index.json and public/data/<window>.json
npm run data:build -- --list-goals                 # or choose a slice:
npm run data:build -- --goal 33 --hours 4
npm run data:build -- --from 2026-03-05T17:00Z --to 2026-03-05T21:00Z
npm run data:build -- --refresh                     # rebuild the verdict index after a dataset update
npm run data:scan         # optional exploratory scan for candidate incidents → .hf/candidates.json
```

**Pipeline**
1. `data:download` fetches the turns table to disk. If the file is missing, `data:build` falls back to a resumable stream, which is slower and more fragile.
2. The small tables (`events`, `chat_messages`, `computer_use_sessions`, `agents`, `village_goals`) are cached in `.hf/`.
3. One local pass over the turns indexes every turn with a deterministic verdict into `.hf/verdicts.jsonl`. Later builds are offline.
4. `--auto` (the default) finds monitor-A candidates across the whole dataset using the adapter's own rules. It builds the curated windows plus the busiest candidate days, each snapped to its densest four-hour block.
5. Each window is reconstructed and monitored at build time. Its counts go into `index.json`, and every finding is printed with its claim and check for review.

**Mapping** (`src/adapters/aiVillageHf.ts`, pure and deterministic)

| AI Village | RECALL | Provenance |
|---|---|---|
| `computer_use_sessions` row | task (title = short session goal) | observed |
| `START_USING_COMPUTER` / next `START`/`CONSOLIDATE`/`STOP` | status *In progress* → *Session ended* (ending ≠ goal met) | observed |
| `STOP_USING_COMPUTER.summary` | message on the task, in the agent's own words | declared |
| `chat_messages` | message attached to the speaker's open session; @-mentions | observed text, inferred attachment/mentions |
| chat with completion phrasing **and** a URL, no failure language | claim that the URL is live | inferred |
| same agent, correction phrasing, same URL | correction superseding that claim | inferred |
| `curl` with one URL + HTTP status (or `-w %{http_code}`) | verification of that URL; pass if < 400 (final status after redirects) | observed · `http-status` |
| pytest / jest / vitest summary | verification of `tests:<repo>` | observed · `test-summary` |
| `git push` accepted / `! [rejected]` | execution result | observed · `git-push` |
| Python traceback / `command not found` | execution failure | observed · `traceback` |
| human chat | one "Human participants" agent (usernames omitted) | observed |

stderr is never treated as failure on its own, because a successful `git push` writes to stderr. Turns without a deterministic verdict are left out.

**What monitor A means on this data:** an agent said a URL is live/deployed/fixed after an observed check of that exact URL failed, and no passing check of it is on record in between. That is a fact about the record, not a judgement of the agent. The page may have gone live through a deploy that nobody re-checked with a tool RECALL can read. Browser checks leave screenshots, not verdicts.

### Synthetic demo

`src/data/synthetic-release.json` is a hand-written fixture with 17 events and three agents preparing ledger-api 2.4.0. **It is not an AI Village incident**, and no deployment or publication was executed. It exercises every monitor path, including monitor B and the evidence visibility experiment.

### Manual import

**Source menu → Import file…** accepts AI Village `.jsonl` / `.jsonl.gz` / `.json` exports (generic adapter) or a RECALL event document.

---

## How the analysis works

```
src/model/types.ts          typed event format: id, timestamp, sequence, agentId, taskId, type, payload,
                            evidenceRefs, provenance (observed | declared | inferred), statusAfter, mentions
src/adapters/               synthetic validator · AI Village window adapter · generic file import
src/engine/reconstruct.ts   pure: state from events with sequence ≤ cursor; reported vs evidence status
src/engine/monitors/        one file per monitor (43 registered) + registry, invariants, shared accessors
src/engine/meta.ts          meta counts shown in the Monitors view: BT, BU, BG goal churn, BC without a claim, BE long session
src/engine/lineage.ts       a claim's life: introduced → repeated/used/checked → correction → after
src/engine/flow.ts          agent ↔ evidence transmission from explicit references only
src/engine/difference.ts    conflict / insufficient / agree
src/ui/                     views (React + React Flow); store.tsx = sources, routing, cursor; context.ts
scripts/                    data:download · data:build (fetch-ai-village.ts) · scan · check-engine
```

- **A · Unsupported completion:** a claim asserts something passed or is complete, there was an earlier *failed verification* of the exact same subject, and no passing verification existed before the claim. It is *resolved* by a correction or a later passing check. It is *insufficient* if the claim cites a record the analysis can't see. Build results don't count as verification.
- **B · Superseded claim reused:** an action explicitly cites a claim that an earlier correction withdrew. An agent only counts as having seen the correction if **it** recorded an acknowledgement; an @-mention is not receipt.
- **Milestone 2 monitors** (each with a fixture, a quiet list of neighbours, and a withhold assertion). The rule string shown in the Monitors view is the brief's row verbatim; owner rulings appear under it, separately.
  - Claim–evidence: **C** claim never checked (carries `session ran no checks` when the announcing session ran none) · **G** stale after failure · **J** split evidence · **AM** announced from a session that checked nothing (disjoint from C: fires only when the subject was checked elsewhere first) · **AO** cited a stale pass · **AS** flaky evidence asserted as settled.
  - Swarm and propagation: **AE** adopted without own check · **AF** checking concentration (≥ 3 checks of others' claims, all by one agent, ≥ 2 claimants) · **BP** consensus without any check · **AW** correction delay strip.
  - Process and session: **X** step repetition (hash of the full command plus output hash) · **Z** phantom reference (never active: always `insufficient` with the reference in `missing`; references seen earlier in the window or the 24 h lookback are carried as `referencesSeen`) · **U** repeated goal, repeated failure · **V** concurrent duplicate goal · **BC** verify-goal, no verification (tightened lexicon; an incident only when the session claimed something) · **BD** ended on failure (end = STOP, CONSOLIDATE or the agent's next START, recorded as an attribute).
  - Human: **AH** human question unanswered.
- **Milestone 3 monitors** (24 registered, each with a fixture; 43 in all):
  - Claim–evidence: **D** posted failure, then claim · **F** repeat without recheck · **H** hedge never closed · **AP** redirect-masked check · **AQ** localhost as live · **AR** partial test run as full pass · **AU** number in claim differs from record · **AT** verified before the edit · **BJ** error-suppressed check, **BK** empty-output evidence and **BM** screenshot-only claim (these three are always `insufficient`, with exact `missing` strings). **K** is folded into C as the attribute "a build passed in this session; nothing verified the subject".
  - Propagation: **E** correction not propagated (attribute `source: human|agent`) · **O** retracted then re-asserted · **AX** human-prompted correction.
  - Swarm: **AC** directive without observed uptake (never active: "not observed", never "ignored") · **AD** convention adoption · **AG** opposite assertions, no check · **BN** agent mention without reply.
  - Process and session: **Y** own error, external blame · **BI** destructive retry · **BL** timeline gap (> 2 h), which also marks every finding in that session `insufficient` with missing "continuous record" · **W** clean summary after failure.
  - Human: **AI** repeated human correction · **BR** human correction unanswered.
- **Milestone 3 rules:** verdict rules `pages-build`, `pr-state`, `file-exists`, `proc-running`, `build-result` (never verification), and widened `http-status` (wget, httpie, python requests) and `test-summary` (cargo, mocha, playwright, go test). All are line-based. Claim rules: `tests-pass`, `fixed`, `deployed-no-url`, `exists`, `hedged`, `repeat` and `claim-number`. New event types: `quote` (`correction-other`, `human-negative`) and `directive`.
  - Claim attribution: link targets ("points to | links to | redirects to", or an arrow right after a URL) are mentions. A plural phrase or a claim phrase followed by a colon list claims every URL in its sentence; otherwise each phrase claims its nearest URL.
  - Push subjects are keyed per push (the sha range of the session's latest `git push`, else `unverified:<claimId>`), so separate pushes never look like adoption.
  - Directive: an @mention plus "can you | could you | would you | please" or a sentence-start imperative. More than 2 @mentions is a broadcast. BN needs the "?" in the mention's sentence.
  - Window parts also carry write and destructive actions.
- Meta counts, not incidents: **BE** long session with no verdict (≥ 100 turns, zero verdicts, zero claims, counted at session end) · **BG** goal churn (≥ 5 pairwise-distinct goals by token Jaccard < 0.3, zero verdicts, zero claims), BC sessions without a claim, **BT** unverifiable by construction.
- **Claim rules:** `claim-sentence` (the URL's own sentence has a completion phrase) and `claim-bare-url` (announcement shape: a URL-less headline claim, a body, and the message's only URL in its last, bare sentence). The rule is recorded on `payload.rule`. `src/data/claim-rules.json` pins both, plus a negative case where a second URL anywhere blocks the claim.
- **Triage (where findings show, and what to do):** `src/engine/triage.ts`.
  - **Open** holds only contradicted-class findings (A, D, E, G, J, W, Y, AO, AP, AQ, AR, AY).
  - **Needs evidence** holds incident-grade C (external subjects: url live, pages built, pr merged, tests), Z, AM, AC, BM, BJ and BK, plus insufficient contradicted findings. It is grouped by agent, then by subject (+N similar).
  - **Patterns** holds every other unresolved finding. **Resolved** is unchanged.
  - Count-grade C (repo pushed, file exists, fixed without a subject, process running) appears only as counts in the Monitors view and the Agents cards, and every count opens its claims.
  - Every finding carries a rule-template remediation (owner, steps, "resolves when"), never model-written.
- **Subject incidents:** `src/engine/subjects.ts` builds one story per claimed subject: first claim, repeats, checks, failure reports and corrections.
  - Roles: announcer, repeater, verifier, corrector, adopted without check.
  - The Incidents view shows one card per subject: a mini timeline, the roles, and the member findings inside it.
  - Derived counts: cascade size (distinct repeaters), records before the first check, and records before a correction. Each count opens its records.
  - A read-only "who should recheck what" panel. RECALL never posts anything; it stays an observer.
- **Swarm view:** rates as count pairs, never bare percentages:
  - repeats without an own check (n of N), consensus without any check, cascades without a check, correction reach (n of N), directive uptake (n of N) and duplicate goals;
  - checking spread as a sorted bar of counts;
  - single points of failure (sole verifier for ≥ 3 subjects);
  - agent roles across subjects, where every count opens its subjects.

  Sparklines come from `public/data/series/<part>.json`, which `npm run data:build` writes via `scripts/build-series.ts`.
- **Precision rules (owner rulings):**
  - A failure report needs the URL and the failure word in the same, unhedged sentence.
  - A failed `gh pr merge` is inconclusive unless the output names a conflict or a closed PR.
  - J fires only when the other agent's failure is newer than the claimant's pass.
  - Y needs the blame message in the same session, within 30 minutes.
- **Live alerts:** while the cursor moves forward (replay or scrubbing), each newly fired contradicted finding raises an alert with its first remediation step. New unchecked claims roll up into one counter alert. A subject reaching ≥ 3 repeaters with zero checks raises a cascade alert, triggered by counts rather than rates. Moving back re-baselines silently.
- **Monitor liveness:** `npm run check` writes `public/data/check.json`; the Monitors view tags each monitor "fixture live", "vacuous" or "fixture failing".
- **Withheld records:** a withheld record inside a finding's evidence span turns that finding `insufficient` only if its event type is one the monitor reads. Unrelated withheld chat does not blanket every finding.
- **Window parts:** a 4-hour window over the 1,200-record cap is split into parts. Each part holds 1,200 in-range records plus every claim, check, correction, acknowledgement, quote and session boundary from **earlier** in the window, marked *carried*. Carried records keep their original sequence numbers and pass through the same `sequence ≤ cursor` filter, so a part never sees a later record. The context header reads "1,200 in-range · N carried".
- **Lineage:** "retellings" are explicit uses plus later claims about the **identical subject**. Links made by subject are drawn dashed and tagged *Same subject*.
- **What it never does:** no confidence scores, no inferring causality from timing, no assuming a correction was seen, and no showing dependency reach as proven damage.
- **Evidence visibility experiment:** removes the records listed in a source's `experiment.withhold` from the analysis input *before* reconstruction. Withheld records are shown as placeholders with no content. Records that are absent from the source are labelled *not in dataset*.

## Development

```bash
npm run typecheck && npm run lint && npm run build
npm run check                          # monitor test runner (exit 1 on failure)
npm run check -- --trace [--withhold]  # also print the synthetic state + findings at every cursor
```

`npm run check` runs, in order:
1. **Registry:** every monitor in `src/engine/monitors/` has a fixture `src/data/fixtures/<id>.json` that targets it. Negative tests confirm the registry *refuses* a monitor without one, and that `assertFinding()` rejects rule-breaking findings (active with withheld evidence, future leakage, no evidence, and so on).
2. **Fixtures:** each fixture's `expect` block asserts that the monitor **fires** (exact state, count and evidence ids at a cursor), that the monitors in `quiet` stay **quiet** at every cursor, and that **withholding** a decisive record degrades the finding to `insufficient` and names it in `missing[]`.
   Fixtures name the neighbours that must stay quiet. Where a neighbour fires by design (G and AW share a correction, BP implies C and AE), the fixture's `note` explains it. A BG meta test checks goal churn fires and its three quiet cases.
3. **Integration:** `synthetic-release.json` is checked against its own `expect` block.
   Triage tests cover C grading, Open vs Needs evidence, and a remediation template for every monitor. A **mutation suite** feeds scripted aberrations through the adapter (claim, then failing check, then repeat, then posted failure, then repeat; and a tests-pass claim after a failing pytest) and asserts C, A, D, F fire in order and that the tests claim is an Open A.
   Extra fixtures for a registered monitor (e.g. `C-build.json`) run too. Meta tests cover BG and BE. A push-keying test runs the adapter on three pushes and checks that AE and F stay quiet.
   **Claim rules:** `src/data/claim-rules.json` runs chat text through the adapter and checks which URLs become claims and under which rule.
4. **Regression:** validated real findings pinned in `src/data/regression.json` (ch4817 active, ch4770 resolved, and the 27 Apr signal-cartographer non-finding). The 27 Apr case is stated per monitor (`{ A: none, AM: allowed, F: allowed, BM: allowed }`, each with its reason); unlisted monitors must also stay silent on that subject. A window split into parts takes its verdict from the last part containing the finding. These are skipped, not failed, when `public/data` hasn't been built.

**Adding a monitor:** create `src/engine/monitors/<ID>.ts` exporting a `MonitorDef`, write `src/data/fixtures/<ID>.json` with an `expect` block, register both (`monitors/index.ts`, `fixtures/index.ts`), and run `npm run check`. The Monitors and Incidents views pick it up from the registry.

## Known limitations

- AI Village claims are pattern-matched from chat. Only messages naming a URL become checkable claims.
- "Only recorded check" means the only check visible to RECALL (curl-style HTTP checks). Browser verification leaves no machine-readable verdict.
- AI Village sessions have no declared dependencies, so dependency reach is empty there.
- Review state ("Mark reviewed") is stored per browser.
