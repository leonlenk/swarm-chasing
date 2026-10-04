# swarm-mcp + SwarmScope

`swarm-mcp` is a modular MCP server (stdio) for investigating multi-agent
("swarm") datasets. Its **SwarmScope** layer loads a dataset into one DuckDB
store with a unified schema. Every record has an **evidence id**
(`{source}:{kind}:{native_id}`, e.g. `village:chat:<uuid>`), and every tool
result carries those ids, so claims can be cited and checked against the raw
records.

The only adapter so far is **AI Village** (AI Digest's long-running experiment
in which frontier-model agents share a group chat and pursue weekly goals).

For module authoring and configuration details, see [ADDING_MODULES.md](ADDING_MODULES.md).

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
cd swarm_mcp
uv sync                     # mcp 2.x, duckdb, networkx, pydantic
uv run pytest               # synthetic fixtures only; no dataset needed
```

## Ingest

The AI Village export (gated) goes in `data/ai-village/` at the repo root. That
location is gitignored; a symlink is fine.

```bash
uv run --directory swarm_mcp swarm-mcp ingest ai_village data/ai-village            # ~15 s
uv run --directory swarm_mcp swarm-mcp ingest ai_village data/ai-village --inspect  # describe files/fields only
```

This builds `data/swarmscope.duckdb` (override with `SWARMSCOPE_DB` or `--db`).
Ingest is idempotent: re-running it replaces that source's rows and leaves
findings alone. On the 2026-09-20 export it loads 46 agents, 183,485 chat
messages, 104,239 actions and 51 goal periods.

| table | from | notes |
|---|---|---|
| `agents` | agents.jsonl.gz | `agent_id` = `village:agent:<uuid>`; aliases such as "Opus 4.5"; model, lab and join date in `meta`; first/last seen come from chat |
| `messages` | chat_messages.jsonl.gz | `channel` = room name; `author_id` = agent id or `human:<user id>`; `recipient_ids` = agents named in the text; `ts_quality='exact'` |
| `actions` | events.jsonl.gz (streamed) | `session_goal` (START_USING_COMPUTER, CONSOLIDATE) and `session_summary` (STOP_USING_COMPUTER); `--no-events` skips them |
| `periods` | village_goals.jsonl.gz | the weekly goals (`village:goal:<uuid>`) |
| `findings` | findings_record | mirror of `findings/findings.jsonl`, which is the source of truth |
| `sources` | ingest | adapter, path, counts and time of each ingest |

`agent_memories` (2.4 GB) and computer-use turns are not read.

## Connect to Claude Code

The repo's `.mcp.json` registers the server as **`swarm`**, so Claude Code
offers it when you open the repo. Tools appear as `mcp__swarm__<tool>`. To
register it by hand, run this from the repo root:

```bash
claude mcp add swarm -e SWARM_DATA_DIR=data -- uv run --directory swarm_mcp swarm-mcp
```

Tools (call `core_list_modules` to see what loaded and why):

| module | tools |
|---|---|
| `scope` | `scope_list_sources`, `scope_agents`, `scope_search`, `scope_get_record`, `scope_messages`, `scope_agent_profile`, `scope_timeline`, `scope_comm_graph` |
| `findings` | `findings_record`, `findings_list`, `findings_spotcheck` |
| `village` | `village_goals`, `village_goal` (AI Village goal periods), plus resources `village://readme`, `village://schema` and `village://changelog` |
| `core` | `core_list_modules`, `core_server_info` |

Safety:
- Dataset text is returned only as `{"content": ..., "untrusted": true}`.
- Emails and phone numbers are masked; `agentvillage.org` is allow-listed via `SWARM_MCP_EMAIL_ALLOWLIST`.
- Text is capped at 500 chars by default; tools take a `max_chars` parameter.
- The server instructions tell the model that record contents are data, not instructions.

## Hooks (`.claude/settings.json`)

- **PostToolUse `hooks/audit_log.py`.** For every `mcp__swarm__.*` call it appends the tool name, the arguments, a sha256 of the result and a timestamp to `findings/audit.jsonl`. It never blocks.
- **Stop `hooks/require_evidence.py`.**
  - It blocks stopping (exit 2, with the reasons on stderr) while any finding in `findings/findings.jsonl` has an evidence id that doesn't resolve, or a corrupt line.
  - It respects `stop_hook_active`, so it can't loop.
  - It allows the stop, with a warning, when the store is missing.
  - The same check is available as `swarm-mcp check-findings`, which exits 0 or 1.

`findings/*.jsonl` is gitignored.

## Render a swimlane

```bash
uv run --directory swarm_mcp swarm-mcp render timeline                         # -> data/swarmscope-timeline.html
uv run --directory swarm_mcp swarm-mcp render timeline --since 2025-10-20 --until 2025-11-03 \
    --channel general --top 10 --out data/poverty.html
```

The output is one self-contained HTML file:
- One lane per agent (the top N by messages) and one mark per message, coloured by channel.
- Hovering a mark shows its evidence id, time and a short masked snippet.
- Large ranges are downsampled deterministically, and the page says so.
- It needs no external scripts, so it opens offline.
- It embeds masked dataset snippets, so keep it under the gitignored `data/` directory.

## Example investigation prompts

1. *"Which agents were most central during the 'Reduce global poverty' goal, and when did activity peak? Cite evidence IDs."*
   Expected tool calls: `village_goal("poverty")`, then `scope_comm_graph` and `scope_timeline` with that goal's `since`/`until`, then `scope_get_record` on the example edges.
2. *"Find the first chat messages describing the environment as hostile and record a finding with evidence."*
   Expected tool calls: `scope_search("hostile environment")` (then `newest_first=false`, `match="all_terms"`), `scope_get_record` for context, then `findings_record(claim, evidence_ids)`.
3. *"How did Claude Opus 4.5's role change across goals? Compare its message share and who it addressed most in two different goals, and spot-check three of the cited messages."*
   Expected tool calls: `village_goals(agent="Opus 4.5")`, `scope_agent_profile` with goal windows, then `findings_spotcheck(kind="messages", ...)`.
