# village_tools

Research scripts over the AI Village export: how coined terms, beliefs and norms spread between agents
(the Idea Flow page and the Idea Spread Viewer, built on the `swarmtrace` trace format), and two pilots on
cooperation (scaling) and coherence. The research question and test cases are in
[docs/PROPOSAL.md](../docs/PROPOSAL.md).

## Before you run anything

- **Data.** `common.py` reads `data/ai-village/*.jsonl.gz` at the repo root (the gated export; see the root
  README's Setup).
- **Run from this folder** (`cd village_tools`): the scripts import each other by module name. The stdlib
  scripts run with `python3`; the ones that plot say which packages they need, e.g.
  `uv run --no-project --with numpy --with scipy --with matplotlib python scaling.py`.
- **`/usr/share/dict/words`** must exist for `ideas.py` (and so for `memories.py` and the `terms` trace).
  Fedora: `dnf install words`; Debian/Ubuntu: `apt install wamerican`.
- **`model_metadata.csv`** (here, next to `scaling.py`) is needed by `scaling.py` and `coherence.py`. It is not
  in git (`*.csv` is gitignored); its source and columns are in the docstring of `scaling.py`. Without it both
  stop with an error that says so.
- Outputs go to `out/` (gitignored) except the reports and figures under `out/scaling/` and `out/coherence/`.

## Idea Flow page (`out/village_idea_flow.html`)

Run in this order:

| step | writes |
|---|---|
| `python3 ideas.py` | `out/ideas.json`: coined terms (non-dictionary words, distinctive phrases), who used each first, when every other agent picked it up, and whether the goal text or changelog seeded it |
| `python3 memories.py` | `out/memories.json`: how much of each agent's memory is about other agents, and memory text shared verbatim (needs `ideas.py` first; caches its memory sample after the first run) |
| `python3 cooperation.py` | `out/cooperation.json`: who addresses whom and how cooperative the chat language is, weekly and per goal |
| `python3 build_viz.py` | `out/village_idea_flow.html`: one self-contained page from `out/*.json` |

## Idea Spread Viewer (`out/sprint_idea/idea_spread.html`)

Three traces, each a chain of stages. The LLM steps are not run by the scripts: one subagent per batch file
labels it, following the prompts in [`prompts/`](prompts/README.md).

1. **Hostility belief**: `python3 tracer_hostility.py build`, `sample`, `sample2`, then the labelling step
   (`prompts/hostility_stance_rubric.md`), then `analyze`. Outputs `out/sprint_idea/hostility/`.
2. **Onboarding norms**: `python3 tracer_onboarding.py events`, `guides`, rule extraction
   (`prompts/onboarding_rule_extraction.md`), `rules`, `items`, labelling (`prompts/onboarding_labelling.md`),
   `analyze` (needs matplotlib), `plots`. Outputs `out/sprint_idea/onboarding/`.
3. **Coined terms**: from `ideas.py` and `memories.py` above.

Then export and build:

```bash
python3 trace_export.py [--source hostility|onboarding|terms|all]   # = python3 -m swarmtrace.cli export --source all --out out/sprint_idea/traces
python3 build_trace_viz.py [TRACES ...] [-o OUT.html] [--day-one YYYY-MM-DD]   # default: out/sprint_idea/traces/ -> out/sprint_idea/idea_spread.html
```

A missing input stops the export with a one-line message naming the stage to run first.

## swarmtrace

A small, source-agnostic "idea trace" format (`swarmtrace/trace.schema.json`, format v0) with a validator and
adapters (`swarmtrace/adapters/aivillage.py`).

```bash
python3 -m swarmtrace.cli export --source hostility|onboarding|terms|all [--adapter aivillage] [--out DIR] [--allow-domain DOMAIN ...]
python3 -m swarmtrace.cli validate FILE... [--adapter NAME] [--allow-domain DOMAIN ...]
uv run --no-project --with pytest python -m pytest -q swarmtrace/tests
```

`export` scrubs emails and phone numbers from free text, checks every trace, writes `<id>.json` and rebuilds
`index.json`. `validate` checks trace and index files. Both exit non-zero on any error.

## Pilots

| script | question | outputs |
|---|---|---|
| `scaling.py` | do more capable or newer models cooperate more (addressing, reciprocity, prosocial language vs ECI and release date)? `--plot-only` redraws the figure | `out/scaling/` (`REPORT.md` and the figure are in git) |
| `coherence.py` | is same-lab agent-to-agent communication less coherent than cross-lab? Has an LLM judge step (`prompts/coherence_judge.md`). `--plot-only` redraws the figure | `out/coherence/` |

## Shared pieces

- `common.py`: loaders and lookups for the export (agents, chat, goals, Village days, mentions).
- `pagekit.py`: assembles a self-contained page (the shared paper style, PaperKit SVG/PNG export, vendored d3).
- `paperfig.py`: the NeurIPS paper style for static matplotlib figures (`paperfig.use()`,
  `paperfig.size(...)`), with colours read from the same `paper.css` as the HTML pages.
- `prompts/`: the labelling and judging prompts for the LLM steps above (reconstructions; see its README).
