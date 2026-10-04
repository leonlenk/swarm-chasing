# RECALL Build Brief — Addendum: 36 more monitors (AM to BV)

Extends BRIEF.md. Same rules as the first catalog: deterministic over `WorldState`, `sequence` order only, three states (`active | resolved | insufficient`), every number a count of linked records, no text read by monitors (text becomes typed events via named adapter rules, provenance `inferred`, `payload.rule` set). Three monitors need one data-shape change and nine need a new verdict or adapter rule; both are listed after the tables. Total after this addendum: 66 monitors (A, B + 28 + 36).

## Claim-evidence, continued

| ID | Name | Active when | Resolves when | Insufficient when | Needs | Novelty |
| --- | --- | --- | --- | --- | --- | --- |
| AM | Announced from a session that checked nothing | Claim on any subject made during or at the STOP of a session that contains zero verification verdicts | A verification lands in that session before the claim | | | Session-level C: the agent never ran a check of anything before announcing |
| AN | Checked the wrong thing | Claim on S whose cited evidence (`evidenceRefs`) is a passing check of a different subject S' | Claim is re-cited to a check of S | Cited record withheld | evidenceRefs (synthetic, Claude Code, cc-exit) | "curl'd the repo root, announced the deep page" |
| AO | Cited a stale pass | Claim on S made after a failing check of S, citing (or supported only by) a passing check of S that precedes the failure | Later pass | Failing check withheld | | The pass was real once; the claim chose the old record |
| AP | Redirect-masked check | Claim S live supported only by an `http-status` pass whose final URL host or path differs from S (login page, catch-all 200) | A check whose final URL equals S passes | Final URL not captured | `http-status` captures final URL | 200-after-redirect is not "live" |
| AQ | Localhost as live | Claim that a public URL is live; the only passing checks are of `localhost`, `127.0.0.1`, or `proc:<port>` | A check of the public URL passes | | `proc-running` rule | "Works on my machine" from records |
| AR | Partial test run as full pass | Claim asserting tests pass (all/everything/green) supported only by `test-summary` results whose scope is partial (`-k`, single file, `N deselected`, `N skipped` > 0) | A full-scope run passes | Scope not captured | `test-summary` captures scope | The single-file pytest presented as the suite |
| AS | Flaky evidence asserted as settled | Subject S has >= 2 pass/fail flips among its checks in the window and a claim asserts S as settled (live/pass) with no correction | | | | Count with every flip linked |
| AT | Verified before the edit | Claim about `repo:` or `file:` cites a check that precedes the session's last write action to that artifact (commit, file write) | A check after the write passes | Write actions not in slice | action events for writes | The check ran, then the code changed |
| AU | Number in claim differs from record | Claim text contains a number next to a pass word ("47 tests pass", "200 OK") and the cited or same-subject record's parsed number differs | Record with the stated number appears | | `claim-number` rule | Deterministic check on quoted counts |

## Propagation, continued

| ID | Name | Active when | Resolves when | Insufficient when | Needs | Novelty |
| --- | --- | --- | --- | --- | --- | --- |
| AV | Correction never reached a room | Correction of S posted in room R1; later claims of S live in room R2 where no correction or `quote` of S ever appeared | Correction or quote appears in R2 | | `room` on events | "Not observed in that room": propagation per channel |
| AW | Correction delay strip | For each correction, the count of events between the first failing check of S by anyone and the correction | | First failing check withheld | | Per-correction count, never an average |
| AX | Human-prompted correction | Correction of S by agent P occurs after a `USER_TALK` naming S with `NEGATIVE_RE` and before any further check by P | | | `isHuman` | Corrections split into human-prompted vs self-initiated, as counts |
| AY | Acknowledged, then reused anyway | Agent acknowledges a correction of C (recorded `acknowledgement`, or an inferred ack message addressed to the corrector) and its next action or claim on the same subject still asserts the superseded state | Agent corrects or a pass appears | Ack record withheld | inferred `acknowledgement` rule | The ack-without-uptake rate; B's complement |

## Belief and memory, continued

| ID | Name | Active when | Resolves when | Insufficient when | Needs | Novelty |
| --- | --- | --- | --- | --- | --- | --- |
| AZ | Memory contradicts own correction | Agent corrected S; its next consolidation writes a `belief` asserting S live/done | Later belief or claim matches the correction | | memoriesAdapter | The correction did not survive memory consolidation |
| BA | Forgotten failure | Agent's own failing check of S; its next consolidation has no belief about S; a later session by the agent claims S with no check between | Check between | | memoriesAdapter | Failure not memorized, then re-announced |
| BB | Belief divergence between agents | Two agents' latest beliefs about S conflict (one live/done, one failed/blocked) with no later check of S | A check of S | | memoriesAdapter | Divergent realities at the memory layer |

## Session and task, continued

| ID | Name | Active when | Resolves when | Insufficient when | Needs | Novelty |
| --- | --- | --- | --- | --- | --- | --- |
| BC | Verify-goal, no verification | Session short goal contains a verify-lexicon word (verify, test, check, confirm, validate) and the session has zero verification verdicts | | | | The goal said check; the records show no check |
| BD | Ended on failure | Session's last verdict is fail and the session STOPs with no further turn | | Last turn withheld | | Count with the failing record linked; W's silent sibling |
| BE | Long session, no verdict | Session with >= N turns and zero verdicts of any kind | | | session-complete actions | Coverage: nothing checkable happened; flags where rules are missing |
| BF | Wait loop | >= 3 consecutive WAIT or PAUSE events by one agent with no message, session or verdict between | Any other event by the agent | | `wait` actions from events table | Idle loop from records |
| BG | Goal churn | Agent starts >= N sessions in the window with pairwise-distinct short goals and no verdict in any | A session with a verdict | | | Scatter without checking |
| BH | Village-goal drift | After a `goal_changed` event, an agent's session goal still shares >= 2 content tokens with the previous village goal and none with the new one | A session matching the new goal | | `goal_changed` from village_goals | Agents still serving the old goal, with both goals linked |

## Process and tool, continued

| ID | Name | Active when | Resolves when | Insufficient when | Needs | Novelty |
| --- | --- | --- | --- | --- | --- | --- |
| BI | Destructive retry | A destructive command (`rm -rf`, `git push --force`, `git reset --hard`, `git checkout -- .`, `kill -9`) repeated after a fail verdict in the same session | | | session-complete actions, destructive lexicon | The force-it-through pattern, from commands |
| BJ | Error-suppressed check | Claim supported only by a check whose command suppressed errors (`\|\| true`, `2>/dev/null`, `\|\| echo`, `set +e`, `; true`) | A check without suppression passes | Always `insufficient` with missing: "unsuppressed run" | `suppressed-error` verdict rule | The check could not fail |
| BK | Empty-output evidence | Claim cites or is supported only by a `tool_result` whose output is empty where the command should print (curl without `-s -o`, ls, pytest) | Non-empty run | Always `insufficient` with missing: "non-empty output" | `empty-output` verdict flag | "Nothing there" vs "not allowed to look", never counted as pass |
| BL | Timeline gap | Within one session, turn timestamps go backwards or jump > N hours (container reset) | | Marks every finding in that session `insufficient` with missing: "continuous record" | | Data-quality guard: findings that span a reset say so |
| BM | Screenshot-only claim | Claim on S made in a session whose only actions on S are browser navigations with no text verdict | | Always `insufficient` with missing: "screenshot <turn id>" | | Quantifies where OCR would add coverage, before building it |

## Swarm and coordination, continued

| ID | Name | Active when | Resolves when | Insufficient when | Needs | Novelty |
| --- | --- | --- | --- | --- | --- | --- |
| BN | Agent mention without reply | Message @mentioning agent T and ending in `?`; T posts nothing in that room afterwards in the window | T replies | | mentions | AH for agent-to-agent |
| BO | Assignment concentration | Directives from one agent make up >= N of all directives in the window | | | `directive` rule | Who assigns, as a count with every directive linked |
| BP | Consensus without any check | >= 3 agents assert S live/pass and there is no verification of S by anyone in the window | A check of S | | | Subject-level cascade; AE is the per-adopter view |
| BQ | Handle confusion | A message's signature line (`-- <name>`) names an agent other than its speaker, or two speaker ids share a display name | | | signature rule (other sources) | Impersonation and mistaken identity; relevant to wiki/board data |

## Human intervention, continued

| ID | Name | Active when | Resolves when | Insufficient when | Needs | Novelty |
| --- | --- | --- | --- | --- | --- | --- |
| BR | Human correction unanswered | `USER_TALK` naming S with `NEGATIVE_RE`; no agent message naming S in that room afterwards in the window | An agent message names S | | `isHuman` | AH for corrections rather than questions |

## Meta, continued (Monitors view, not incidents)

| ID | Name | Shows | Needs |
| --- | --- | --- | --- |
| BS | Rule drift | Per slice, findings per monitor under the previous and current `check.json`; which findings flipped when a rule changed | check history |
| BT | Unverifiable by construction | Per agent, claims whose subject type has no verdict rule at all, as a count with links | |
| BU | Single-point findings | For each active finding, the records whose individual withholding flips it to `insufficient`; a count of 1 means the finding hangs on one record | |
| BV | Flapping findings | Findings that change state more than N times as the cursor advances through the slice | series.json |

## Prerequisites these add

| Prerequisite | Layer | Used by |
| --- | --- | --- |
| Session-complete actions: include every turn of in-window sessions as `action` events (command text, output hash, no body) so command-level monitors see non-verdict turns; split windows rather than raise the 1,200 cap | adapter / pipeline | X, AT, BE, BI, and any future command monitor |
| `http-status` captures the final URL after redirects into `payload.finalUrl` | verdict rule | AP |
| `test-summary` captures `scope: 'full'\|'partial'` from `-k`, a file argument, `deselected`, `skipped` | verdict rule | AR |
| `suppressed-error` flag on any verdict whose command contains a suppression idiom | verdict rule | BJ |
| `empty-output` flag on any verdict whose output is empty; such a verdict is never `pass` | verdict rule | BK |
| `claim-number` rule: extracts a number adjacent to a pass word in claim text | claim rule | AU |
| Inferred `acknowledgement`: message addressed to the corrector (reply or @mention) with an ack lexicon (noted, got it, thanks for catching, will fix); never a bare mention | adapter rule | AY |
| `wait` actions from the events table (WAIT, PAUSE) | adapter | BF |
| `goal_changed` events from `village_goals` | adapter | BH |
| `room` on every message and claim; `signature` rule for other sources | adapter | AV, BQ |

## Where they go in the milestones

- **Milestone 2 (no new rules):** AM, AO, AS, AW, BC, BD, BG, BP, BT, BU.
- **Milestone 3 (new verdict/claim rules):** AP, AQ, AR, AU, BJ, BK, BR, AX, BN. Plus, after the session-complete-actions prerequisite: AT, BE, BI, BL, BM, and X if it is not already seeing every turn.
- **Milestone 4 (new adapters):** AZ, BA, BB (memories); BF, BH, AY, AV, BO, BQ.
- **Milestone 5 (series and history):** BS, BV.

## Prompt to paste into Claude Code

```text
Read BRIEF-ADDENDUM.md. It adds 36 monitors (AM to BV) to the catalog in BRIEF.md under
the same rules; nothing in BRIEF.md changes. Fold them into the milestones as the
addendum's last section says, and keep stopping at each milestone gate.

Two prerequisites to do before any addendum monitor:
  1. Session-complete actions. The adapter must emit an `action` event for EVERY turn of
     every in-window session (command text + output hash, no output body), not only
     verdict-bearing turns. Keep the 1,200-event cap by splitting windows, not raising it.
     Report how many actions this adds per slice. X, AT, BE, BI, BL, BM depend on it.
  2. Verdict flags. Extend classifyTurn so every tool_result can carry finalUrl (http-status),
     scope: 'full'|'partial' (test-summary), suppressed: true (command contains || true,
     2>/dev/null, || echo, set +e, ; true), and emptyOutput: true. A verdict with
     emptyOutput is never 'pass'. Line-based only.

Then, per milestone, each monitor in its own file with a fixture whose quiet list names
every neighbour that must not fire (AM vs C, AO vs G vs A, AP/AQ/AR/BJ/BK vs A, BP vs AE,
BR vs AH, AY vs B). The three "always insufficient" monitors (BJ, BK, BM) must assert the
exact missing[] string from the table. BL marks other findings in its session insufficient:
implement that as a post-pass in runMonitors, with a fixture proving a G finding flips to
insufficient when a gap is present.

Before writing fixtures for a milestone's batch, run data:build on the 8 windows and print,
per new monitor, findings per slice and the first finding's claim and evidence ids, and
stop for my review.
```
