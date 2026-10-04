# LLM prompts for the village_tools pipelines

Three analyses in `village_tools/` include an LLM step that the scripts do not run themselves. In the original
runs a Sonnet subagent ran each step on one batch file and wrote one label file. This directory holds the
instructions for those steps.

**These are reconstructions.** The prompts used in the original runs lived in the gitignored `out/` tree and
were not preserved. Each file here was rebuilt from the code that writes the batch files and the code that
reads the labels back, so its output matches what that code parses. Wording, examples and edge-case rules may
differ from the originals, so labels made with them are not guaranteed to reproduce the published numbers. All
examples are synthetic (agents `AgentA`, `AgentB`, …; invented repos and rules). There is no dataset text in
these files.

## The prompts

All paths are relative to `village_tools/out/`, which is gitignored.

| prompt | used between | input (written by) | output (read by) |
|---|---|---|---|
| [`hostility_stance_rubric.md`](hostility_stance_rubric.md) | `tracer_hostility.py sample` / `sample2` and `analyze` | `sprint_idea/hostility/label_batches/batch_<k>.json`, k = 0 to 7 (`sample()`, `sample2()`) | `label_batches/labels_<k>.json`, a JSON array of `{id, label, confidence, reason}` (`tracer_hostility.load_labels()`, `analyze()`) |
| [`onboarding_rule_extraction.md`](onboarding_rule_extraction.md) | `tracer_onboarding.py guides` and `rules` | `sprint_idea/onboarding/evidence/rules_batch_<i>.txt`, i = 1 to 3 (`stage_guides()`) | `evidence/rules_extracted_<i>.json` with `{rules, tips}`. A human merges it by hand into `RULES` and `TIP_EDGES` in `tracer_onboarding.py`; no code parses it. |
| [`onboarding_labelling.md`](onboarding_labelling.md) | `tracer_onboarding.py items` and `analyze` | `sprint_idea/onboarding/label_batches/RULES.txt` (`stage_rules()`) and `batch_<b>.jsonl`, b = 1 to 5 (`stage_items()`) | `label_batches/labels_<b>.jsonl`, one `{item_id, guide_ref, labels: [{rule, label, conf, version}]}` per line (`tracer_onboarding.load_labels()`) |
| [`coherence_judge.md`](coherence_judge.md) | the first and second runs of `coherence.py` | `coherence/judge_items_<j>.jsonl`, j = 1 to 3 (`make_items()`) | `coherence/judge_ratings_<j>.json`, a JSON array of `{item_id, addresses, no_misunderstanding, not_degenerate, overall}` (`analyse_judged()`) |

`swarmtrace/tests/test_prompts.py` checks that each prompt names every label or scale its reader accepts, and
that each prompt's output example parses into the shape the reader expects.

## Running a step

Start one subagent per batch, with a brief such as:

> Read `village_tools/prompts/hostility_stance_rubric.md` and follow it exactly. Label
> `village_tools/out/sprint_idea/hostility/label_batches/batch_3.json` and write
> `village_tools/out/sprint_idea/hostility/label_batches/labels_3.json`.

Then rerun the next stage of the script (`analyze`, or `coherence.py` again). Check the output before
rerunning:
- every input id appears exactly once;
- every value is one the prompt allows;
- the file is valid JSON (or JSON Lines for the onboarding labels).

The readers fail with a `KeyError` on a missing required field. A duplicate id is silently overwritten (hostility
labels, coherence ratings) or counted twice (onboarding labels).

## From labels to trace stances

`swarmtrace/adapters/aivillage.py` turns the labels into the `stance` of a trace event. Not every stance comes
from an LLM label:

| trace stance | where it comes from |
|---|---|
| `originates` | hostility: an origin agent's adoption item (`H_ORIGINS`); terms: the coining message found by `ideas.py` (no LLM) |
| `endorses` | hostility `ENDORSES` (also unlabelled keyword-positive Gemini chat, with `conf` null); onboarding `STATES` |
| `acts_on` | hostility `ACTS_ON`; onboarding `FOLLOWS` |
| `mentions` | hostility `NEUTRAL_MENTION` |
| `questions` | hostility `QUESTIONS` |
| `rejects` | hostility `REJECTS`; onboarding `VIOLATES` |
| `mutates` | onboarding `MUTATED` (its `version` becomes a quote) |
| `uses` | terms: a later message using a coined term (`ideas.py`, regex only, no LLM) |

The adapter drops hostility `ORDINARY_BUG` and `UNRELATED` labels. `tracer_hostility.analyze()` still uses
them, for the bug-control and common-cause checks.

## Local-only hand-label files

These files hold labels or keys derived from the dataset. They stay in the gitignored `out/` tree and are never
committed. Their formats are documented here so that they can be rebuilt.

**`sprint_idea/hostility/label_batches/labels_manual.json`.** Hand labels for origin-critical items (3 in the
original run: early Gemini 2.5 Pro items).
- It has the same shape as `labels_<k>.json`: a JSON array of `{"id", "label", "confidence", "reason"}`, plus
  `"src": "manual"`. `load_labels()` keeps a given `src` and adds `"llm:<file stem>"` only when `src` is missing.
- Files load in sorted name order, and `labels_manual.json` sorts after `labels_<digit>.json`. A manual label
  therefore overrides an LLM label for the same `id`.
- An `id` that is in `candidates.csv` but in neither `sample.csv` nor `sample2.csv` joins the analysis with
  `why = "manual_origin"`.

**`sprint_idea/hostility/handcheck.json`.** The hand check of LLM hostility labels. `analyze()` copies three
keys into `results.json["handcheck"]`, and needs all three when the file exists:

```json
{"n": 30, "exact_agree": 0.8, "binary_pos_agree": 0.9}
```

- `n`: the number of hand-checked items.
- `exact_agree`: the share where the hand label equals the LLM label.
- `binary_pos_agree`: the share that agree on positive (`ENDORSES` or `ACTS_ON`) versus everything else.

The values above are placeholders. The two agreement values are copied verbatim, so the reader does not enforce
shares (0 to 1) rather than counts; shares are the recommended form. Any other keys are ignored. Recording the
per-item judgements as `"items": [{"id", "llm", "hand"}]` keeps the check auditable.

**Onboarding hand check.** No file. It is the `HANDCHECK` dict in `tracer_onboarding.py`: `"I0042/R07": 1` (1
means agree, 0 means disagree with the Sonnet label, judged against the item text). It holds only ids, never
text. `stage_analyze()` summarises it as `results.json["handcheck"]`, which measures the precision of non-NA
labels only.

**`coherence/judge_key.json`.** Not a hand-label file, but local-only for the same reason. It is the unblinding
key that `make_items()` writes: `{"key": [{item_id, ex_idx, A, B, lab_A, lab_B, same, replier_group, stratum,
t_B, room}], "assignment": {judge: [item_id, …]}, "sampling": {…}}`. Never show it to a judge.
