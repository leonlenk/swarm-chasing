# swarm-chasing

Tools for investigating multi-agent swarms: what a team of agents said and did, where an agent's account of its work
diverges from the record, how ideas and work moved between agents, and what your own Claude Code agents are doing
right now.

| Part | What it is | Start here |
|---|---|---|
| [`swarm_mcp/`](swarm_mcp/README.md) | **SwarmScope**: one DuckDB store for any swarm dataset (AI Village, git repos, wikis, mapped data, recorded Claude Code sessions), an MCP server whose findings must cite resolvable evidence ids, LLM rubric sweeps, redacted export, and the explorer and subtask analyses | `swarm-mcp add`, `swarm-mcp info` |
| [`swarm_mcp/src/swarm_mcp/live/`](swarm_mcp/README.md#use-it-as-a-claude-code-plugin-and-record-your-own-sessions) | **swarm-live**: the repo is a Claude Code plugin whose hooks record every session and subagent on the machine | `claude --plugin-dir ~/swarm-chasing` |
| [`recall/`](recall/README.md) | **RECALL**: the UI. A temporal debugger with 43 deterministic monitors, plus views of the SwarmScope store and of live sessions | `npm run dev` |
| [`village_tools/`](village_tools/README.md) | Research scripts: the Idea Flow page, idea-spread tracers and the Idea Spread Viewer (swarmtrace), scaling and coherence pilots; the question is in [`docs/PROPOSAL.md`](docs/PROPOSAL.md) | `village_tools/README.md` |

## Features at a glance

- **Store and ingest**: `swarm-mcp add` for AI Village, git repos, wikis, swarm-live recordings and any other
  dataset through a drafted mapping (`--agent none|api|claude-code`, the `/swarm-setup` command):
  [Add a dataset](swarm_mcp/README.md#add-a-dataset); `swarm-mcp info` for what is loaded.
- **MCP server `swarm`**: 21 tools (`core_*`, `scope_*`, `findings_*`, `sweep_*`, `subtasks_*`), the
  `investigate` prompt and the AI Village doc resources: [Tools and prompt](swarm_mcp/README.md#tools-and-prompt).
  Findings are rejected unless every cited id resolves.
- **Rubric sweeps** with a dry-run cost estimate and hand-label precision review:
  [Rubric sweeps](swarm_mcp/README.md#rubric-sweeps).
- **Redaction and export**: one masking engine for every output; `swarm-mcp export` rescans its own output:
  [Export](swarm_mcp/README.md#export), [Privacy](swarm_mcp/README.md#privacy).
- **HTML views**: `swarm-mcp render timeline|subtasks|recall`: [Commands](swarm_mcp/README.md#commands).
- **Claude Code plugin and swarm-live**: records every session and subagent; `.mcp.json` and
  `.claude-plugin/plugin.json` register the server:
  [plugin](swarm_mcp/README.md#use-it-as-a-claude-code-plugin-and-record-your-own-sessions).
- **Hooks in this repo**: an audit log of `mcp__swarm__*` calls and a Stop hook that blocks on unresolvable
  findings: [Hooks](swarm_mcp/README.md#hooks-claudesettingsjson).
- **Configuration**: `swarm.toml` and environment variables: [Configuration](swarm_mcp/README.md#configuration).
- **Examples and benchmark**: [Examples](swarm_mcp/README.md#examples);
  `python -m swarm_mcp.bench generate|reference|score` ([ADDING_MODULES.md](swarm_mcp/ADDING_MODULES.md)).
- **RECALL**: replay, 43 deterministic monitors, triage, Explorer, Subtasks and Live sessions views:
  [recall/README.md](recall/README.md).
- **Idea spread research**: Idea Flow page, Idea Spread Viewer, swarmtrace, scaling and coherence pilots,
  paperfig, LLM labelling prompts: [village_tools/README.md](village_tools/README.md).

## Setup

- **Python 3.11+** and **[uv](https://docs.astral.sh/uv/)** for `swarm_mcp/` and `village_tools/`.
- **Node 22+** and npm for RECALL.
- **[Claude Code](https://docs.claude.com/en/docs/claude-code)** for the MCP server, the plugin and the hooks
  (`python3` on PATH for the hooks).
- **The AI Village data is gated.** It comes from AI Digest; request access on
  [huggingface.co/datasets/aidigestorg/ai-village](https://huggingface.co/datasets/aidigestorg/ai-village)
  and keep to the research-use terms stated there. Nothing derived from it goes into git.
  - For `swarm-mcp add` and `village_tools/`: unpack the export into `data/ai-village/` at the repo root
    (`chat_messages.jsonl.gz`, `events.jsonl.gz`, `README.md`, …; a symlink is fine).
  - For RECALL's replay windows: `npm run data:build` downloads from Hugging Face itself and needs a token
    (`HF_TOKEN`, or log in once; see [recall/README.md](recall/README.md#data)).
- **For `village_tools/` only:** `/usr/share/dict/words` (package `words` or `wamerican`) for `ideas.py`, and
  `village_tools/model_metadata.csv` for `scaling.py` and `coherence.py`, which is not in git.
- **Optional:** `ANTHROPIC_API_KEY` for sweeps, `add --agent api` and `render subtasks --llm-names`.

## How the pieces fit

```
 datasets ──► swarm-mcp add ──► SwarmScope store ──► MCP tools (Claude investigates, cites ids)
 (village, git, wiki, …)              │
                                      ├─► swarm-mcp render recall ──► recall/public/data/scope/ ──► RECALL
 Claude Code hooks ──► swarm-live recordings ─┘   (explorer, subtasks,            (Explorer, Subtasks,
                       (SQLite, WAL)              live sessions; --watch)          Live sessions + replay)
 AI Village HF export ──► npm run data:build ──► recall/public/data/*.json ──► RECALL replay + monitors
```

RECALL views, by where their data comes from:

| RECALL view | Data | Built by |
|---|---|---|
| Overview, Propagation, Tasks, Agents, Incidents, Swarm, Monitors, Evidence | any replay source: an AI Village window, a live Claude Code session, the synthetic demo, an imported file | `npm run data:build`; `render recall` |
| **Live sessions** | Claude Code sessions and their subagents, as recorded | `render recall [--watch]` from the swarm-live recordings |
| **Explorer** | a store source: activity per agent, notable moments, goal recaps, agent arcs, metrics | `render recall` (the `render timeline` payload) |
| **Subtasks** | a store source with artifact touches: subtasks, names, typed handoffs | `render recall` (the `render subtasks` payload) |

Ids match across all of it. A live session's records carry their SwarmScope evidence ids (`claude-code:msg:…`), and
an AI Village chat record `chat/<uuid>` is `village:msg:<uuid>` in the store. The record drawer shows the store id, so
anything RECALL flags can be cited with `findings_record` and re-read with `core_get`.

## Quick start

```bash
# 1. the store (Python 3.11+, uv)
cd swarm_mcp && uv sync && cd ..
uv run --directory swarm_mcp swarm-mcp add data/ai-village                   # AI Village export
uv run --directory swarm_mcp swarm-mcp add data/repos/rpg-game.git          # optional: a repo the agents built (bare clone)

# 2. RECALL's data and UI (Node 22+)
uv run --directory swarm_mcp swarm-mcp render recall                         # explorer, subtasks, live sessions
cd recall && npm install && npm run data:build && npm run dev               # AI Village replay windows (needs HF access) + the UI

# 3. watch your own agents
claude --plugin-dir ~/swarm-chasing                                          # in any project (path to this clone): records sessions
uv run --directory swarm_mcp swarm-mcp render recall --watch                 # RECALL follows them live
uv run --directory swarm_mcp python examples/live_demo.py data/demo.db --drip 1.5   # or a synthetic demo, live; in a 2nd shell:
uv run --directory swarm_mcp swarm-mcp render recall --watch --recordings data/demo.db
```

Datasets, recordings and everything derived from them stay out of git (`data/`, `recall/public/data`, `recall/.hf`).
