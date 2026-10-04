# swarm-chasing

Tools for investigating multi-agent swarms: what a team of agents said and did, where an agent's account of its work
diverges from the record, how ideas and work moved between agents, and what your own Claude Code agents are doing
right now.

| Part | What it is | Start here |
|---|---|---|
| [`swarm_mcp/`](swarm_mcp/README.md) | **SwarmScope**: one DuckDB store for any swarm dataset (AI Village, git repos, wikis, mapped data, recorded Claude Code sessions), an MCP server whose findings must cite resolvable evidence ids, LLM rubric sweeps, redacted export, and the explorer and subtask analyses | `swarm-mcp add`, `swarm-mcp info` |
| [`swarm_mcp/src/swarm_mcp/live/`](swarm_mcp/README.md#use-it-as-a-claude-code-plugin-and-record-your-own-sessions) | **swarm-live**: the repo is a Claude Code plugin whose hooks record every session and subagent on the machine | `claude --plugin-dir ~/swarm-chasing` |
| [`recall/`](recall/README.md) | **RECALL**: the UI. A temporal debugger with 43 deterministic monitors, plus views of the SwarmScope store and of live sessions | `npm run dev` |
| [`village_tools/`](docs/PROPOSAL.md) | Research scripts: idea-spread tracers (swarmtrace), scaling and coherence reports | `docs/PROPOSAL.md` |

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
uv run --directory swarm_mcp swarm-mcp add data/repos/rpg-game.git          # a repo the agents built

# 2. RECALL's data and UI (Node 22+)
uv run --directory swarm_mcp swarm-mcp render recall                         # explorer, subtasks, live sessions
cd recall && npm install && npm run data:build && npm run dev               # AI Village replay windows + the UI

# 3. watch your own agents
claude --plugin-dir ~/swarm-chasing                                          # in any project: records sessions
uv run --directory swarm_mcp swarm-mcp render recall --watch                 # RECALL follows them live
uv run --directory swarm_mcp python examples/live_demo.py data/demo.db --drip 1.5   # or a synthetic demo, live
```

Datasets, recordings and everything derived from them stay out of git (`data/`, `recall/public/data`, `recall/.hf`).
