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
src/engine/monitors/        one file per monitor (19 registered) + registry, invariants, shared accessors
src/engine/meta.ts          meta counts shown in the Monitors view: BT, BU, BG goal churn, BC without a claim
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
  - Process and session: **X** step repetition (hash of the full command plus output hash) · **Z** phantom reference (never active: always `insufficient` with the reference in `missing`; references seen earlier in the window or the 12 h lookback are carried as `referencesSeen`) · **U** repeated goal, repeated failure · **V** concurrent duplicate goal · **BC** verify-goal, no verification (tightened lexicon; an incident only when the session claimed something) · **BD** ended on failure (end = STOP, CONSOLIDATE or the agent's next START, recorded as an attribute).
  - Human: **AH** human question unanswered.
  - Meta counts, not incidents: **BG** goal churn (≥ 5 pairwise-distinct goals by token Jaccard < 0.3, zero verdicts, zero claims), BC sessions without a claim, **BT** unverifiable by construction.
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
4. **Regression:** validated real findings pinned in `src/data/regression.json` (ch4817 active, ch4770 resolved, and the 27 Apr signal-cartographer non-finding). A window split into parts takes its verdict from the last part containing the finding. These are skipped, not failed, when `public/data` hasn't been built.

**Adding a monitor:** create `src/engine/monitors/<ID>.ts` exporting a `MonitorDef`, write `src/data/fixtures/<ID>.json` with an `expect` block, register both (`monitors/index.ts`, `fixtures/index.ts`), and run `npm run check`. The Monitors and Incidents views pick it up from the registry.

## Known limitations

- AI Village claims are pattern-matched from chat. Only messages naming a URL become checkable claims.
- "Only recorded check" means the only check visible to RECALL (curl-style HTTP checks). Browser verification leaves no machine-readable verdict.
- AI Village sessions have no declared dependencies, so dependency reach is empty there.
- Review state ("Mark reviewed") is stored per browser.
