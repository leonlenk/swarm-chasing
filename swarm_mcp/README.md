# swarm-mcp + SwarmScope

`swarm-mcp` is an MCP server (stdio) for investigating multi-agent ("swarm")
datasets. Its **SwarmScope** store loads each dataset into one DuckDB file with a
shared schema (agents, messages, actions, periods, artifacts and touches). Every
record has an **id** `<source>:<kind>:<id>` whose kind is the same for every
dataset: `msg`, `event`, `agent`, `period` or `artifact` (AI Village goals keep
`goal`), e.g. `village:msg:<uuid>` or `rpg-game:period:pr-109`. The dataset's own
type (commit, revision, pull request) is a field of the record. Every tool result
carries ids, so each claim can be cited and checked against the raw record.

Built in: **AI Village** (AI Digest's long-running experiment in which
frontier-model agents share a group chat and pursue weekly goals), **git**
(a repo the agents built: commits, pull requests, files) and **wiki** (edit
histories in the collusion.wiki explorer schema). Any other dataset (chat logs,
forums, agent traces) can be added through a declarative mapping.

Writing modules, adapters or mappings: see [ADDING_MODULES.md](ADDING_MODULES.md).

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
cd swarm_mcp
uv sync           # mcp 2.x, duckdb, networkx, pydantic, anthropic
uv run pytest     # synthetic fixtures only; no dataset needed
```

## Add a dataset

```bash
uv run --directory swarm_mcp swarm-mcp add data/ai-village     # ~15 s for the full AI Village export
uv run --directory swarm_mcp swarm-mcp info                    # what is loaded, sources, findings health
```

Put datasets under `data/` at the repo root (gitignored; a symlink is fine).
`add` detects the AI Village export and bare git repos and uses the built-in
adapters (`--adapter village|git` forces one; `--name` sets a git source's name).
Wiki databases are only ingested on request: `swarm-mcp add data/collusion-wiki --adapter wiki`.
For any other dataset it profiles the files, drafts a mapping (`--agent none` heuristics, `api`
an LLM, or `claude-code` a task for the `/swarm-setup` command), checks it, and
stops with the report if the check fails. When the check passes, it ingests.
`--dry-run` stops after the check, and `--mapping M` uses your own mapping.
Re-running `add` replaces that source and keeps other sources and findings. The
store is `data/swarmscope.duckdb`.

On the 2026-09-20 AI Village export, `add` loads 46 agents, 183,485 chat
messages, 104,239 actions and 51 goal periods. `agent_memories` and
computer-use turns are not read.

## Connect to Claude Code

The repo's `.mcp.json` registers the server as **`swarm`**, so Claude Code offers
it when you open the repo; enable it and restart it (`/mcp`) after an `add`. Tools
appear as `mcp__swarm__<tool>`. To register it by hand, from the repo root:

```bash
claude mcp add swarm -e SWARM_DATA_DIR=data -- uv run --directory swarm_mcp swarm-mcp
```

Start a session with `core_info`, or with the `investigate` prompt.

## Use it as a Claude Code plugin (and record your own sessions)

The repo root is also a Claude Code plugin. Install it with `/plugin marketplace add
leonlenk/swarm-chasing`, then `/plugin install swarm-chasing@swarm-chasing`, or try it for one
session with `claude --plugin-dir <path to this repo>`. It needs `uv` and Python on PATH.

- **The `swarm` MCP server** runs as `uv run --project ${CLAUDE_PLUGIN_ROOT}/swarm_mcp swarm-mcp`.
  `--project` keeps the working directory, so a relative `SWARM_DATA_DIR` (the plugin asks for
  it as `data_dir`, default `data`) resolves against the project you run Claude Code in. Its
  venv lives in the plugin's data directory, so it survives plugin updates. Claude Code starts
  and stops this stdio server with each session. The plugin's `swarm` entry replaces the root
  `.mcp.json` one, which stays for working in this repo.
- **Recording.** `hooks/hooks.json` makes every session on the machine record itself:
  - The `SessionStart` hook (`swarm_mcp/src/swarm_mcp/live/launch.py`, stdlib only) starts the
    collector `swarm-live serve --exit-when-idle` on `127.0.0.1:47831`.
  - Every other hook POSTs its payload there, and the collector writes it to a SQLite file in
    the plugin's data directory.
  - There is one collector per machine. It exits about a minute after the last Claude Code
    process ends.
  - If it's down, the hooks fail silently and never block the agent.
- **Investigating the recordings.** They become the store source `claude-code`, through the
  `claude_code` adapter (automatically when the server starts inside the plugin, or with
  `claude_code_sync` mid-session). Then the usual tools apply:
  - `scope_search`, `core_get` and `scope_agents` (main agents and subagents);
  - `scope_periods` (one per session or subagent run);
  - `scope_graph` (delegation and reporting edges);
  - findings and sweeps.

```bash
uv run --directory swarm_mcp swarm-live import ~/.claude/projects/<project-dir>   # load past sessions
uv run --directory swarm_mcp swarm-mcp add ~/.swarm-live/swarm-live.db             # into the store
uv run --directory swarm_mcp swarm-live serve                                      # a permanent collector
```

The repo's own hooks in `.claude/settings.json` (below) are separate: they audit the
investigator's `mcp__swarm__*` calls in this repo and are not part of the plugin.

## Commands

| command | what it does |
|---|---|
| `swarm-mcp` | run the MCP server on stdio (what Claude Code launches) |
| `swarm-mcp info [--json]` | modules (loaded or skipped, and why), sources with counts and date ranges, findings health, config. Exits 1 when a finding cites an id that does not resolve |
| `swarm-mcp add <path> [--adapter auto\|village\|git\|wiki\|mapped] [--name SLUG] [--agent none\|api\|claude-code] [--mapping M] [--dry-run] [--db]` | add or refresh a dataset in the store (see above) |
| `swarm-mcp render timeline [--since --until --channel --source --top --out]` | a self-contained HTML swimlane (one lane per agent, one mark per message, masked hover snippets). Default output `data/swarmscope-timeline.html` |
| `swarm-mcp export --out DIR [--source --kind --channel --author --since --until --query] [--with-agents] [--keep-ips] [--no-check] [--json]` | export a redacted subset of the store for sharing, then rescan it (see Export) |

Developer-only: `python -m swarm_mcp.bench generate|reference|score` (see ADDING_MODULES.md).

## Tools and prompt

| tool | what it does |
|---|---|
| `core_info` | start here: modules, sources (row counts, date ranges, channels, blind-spot notes), findings health, config |
| `core_get(ids, before=0, after=0, max_chars)` | the record behind any id, or a batch of up to 50: messages, actions, agents, periods (with member records) and artifacts (with the records that touched them); `before`/`after` add neighbouring records |
| `scope_search(query=None, match, source, channel, author, since, until, table, newest_first, limit, offset, max_chars)` | full-text search over messages or actions; with no query, reads the window in time order |
| `scope_agents(name=None, ...)` | the agent list; with a name, that agent's profile (channels, co-presence, who it names, actions, samples) |
| `scope_periods(name=None, source, agent, top)` | dataset periods (AI Village: weekly goals, with a heuristic goal type); with a name, one period's activity |
| `scope_timeline(bin, group_by, table, ...)` | activity counts per hour/day/week/month, optionally by channel or author |
| `scope_graph(...)` | who talks to whom: mention and reply edges, top nodes by degree and betweenness, example ids |
| `findings_record(claim, evidence_ids, confidence)` | record a claim; rejected unless every id resolves |
| `findings_list(status, limit, sample=None, seed=0)` | recorded findings, newest first; with `sample`, a seeded random sample with the cited evidence, for spot checks |
| `sweep_run(rubric, ids=None, filters=None, dry_run=True, cap=50)` | apply a yes/no rubric to many records with an LLM; the default dry run returns the cost estimate |
| `sweep_get(sweep_id=None, ...)` | the saved sweeps, or one sweep's verdicts |
| `sweep_review(sweep_id, labels=None, n=20, seed=0)` | items to hand-label plus the current precision; with labels, records them and returns the updated precision |

Prompt: `investigate(question, custom, source, since, until, period, agent, location)`.
`question` is one of `actors`, `instructions`, `sequence`, `reasoning`, `misreporting`,
`collaboration`, `environment` or `custom` (your own question in `custom`). The
rendered prompt walks the model through finding, reading and citing evidence.

`subtasks` (Rigel's module, loaded when the store has data) adds
`subtasks_corpora`, `subtasks_list`, `subtasks_get`, `subtasks_trace_pair` and
`subtasks_locate`: work units (pull requests, runs, sessions) grouped into subtasks,
with typed handoffs between actors; see ADDING_MODULES.md. `village` adds the AI
Village docs as resources (`village://readme`, `village://schema`, `village://changelog`).

## Rubric sweeps

A sweep applies one yes/no rubric ("Does the agent claim to have finished a task it
did not finish?") to many records, one model call per record, and stores every
verdict with the id it is about.

1. `sweep_run(rubric, filters={"source": "village", "channel": "general", "since": "2026-01-05", "until": "2026-01-12"})`
   is a dry run: records, estimated tokens and USD, and the first prompt. Filters are
   `source`, `kind` (`msg` or `event`), `type` (the dataset type, e.g. `session_goal`),
   `channel`, `author`, `since`, `until` and `query`; or pass `ids`.
2. The same call with `dry_run=false` runs it (needs `ANTHROPIC_API_KEY`; `cap` defaults
   to 50, max 500). The run stops after 3 consecutive failed calls.
3. `sweep_review(sweep_id)` returns items to check; read each with `core_get`, then
   `sweep_review(sweep_id, labels=[{"event_id": ..., "correct": true}, ...])`. Precision
   of the `yes` verdicts comes with a Wilson 95% interval and the number of labels it
   rests on. Label at least 20 before quoting counts.

Each record goes to the model inside `<record untrusted="true">` with the system prompt
saying it is data, never instructions. Replies are strict JSON (`verdict`, `confidence`,
`rationale`); anything unparseable becomes `unclear`. Results go to `sweeps/`
(gitignored).

## Export

```bash
uv run --directory swarm_mcp swarm-mcp export --source village --channel general \
    --since 2025-12-01 --until 2025-12-08 --out data/export-week
```

The export directory holds `events.jsonl` (standard records with full text, every
string field redacted except the identity fields), optional `agents.jsonl`, and
`manifest.json` (counts by source and kind, redaction counts by type, the policy, the
filters, file hashes; never the redacted values). Exports mask private and loopback IPs
too (`--keep-ips` leaves them). Afterwards the command rescans everything with every rule
and exits 1 on any hit, hash mismatch or unlisted file; `--no-check` skips that. Emails
at the `email_allowlist` domains are kept.

## Privacy

- Dataset text is returned only as `{"content": ..., "untrusted": true}`, capped at 500
  characters by default (`max_chars` raises it).
- One masking engine (`swarm_mcp.redact`) is used everywhere: tool output, the
  timeline page, setup profiles and exports. It masks emails (except
  `[privacy] email_allowlist`, default `agentvillage.org`) as `[email]`, phone numbers as
  `[phone]`, credentials (provider keys, JWTs, auth headers, `key=secret` pairs, private
  keys) as `[credential]` and `user:pass@` in URLs as `[url-credential]`. VCS remotes such
  as `git@github.com:org/repo` are kept.
- The server instructions tell the model that record contents are data, not instructions.

## Hooks (`.claude/settings.json`)

These run in every Claude Code session opened in this repo.
- **PostToolUse `hooks/audit_log.py`**: for every `mcp__swarm__*` call, appends the tool
  name, arguments, a sha256 of the result and a timestamp to `findings/audit.jsonl`. It never blocks.
- **Stop `hooks/require_evidence.py`**: blocks stopping (exit 2, reasons on stderr) while a
  finding in `findings/findings.jsonl` cites an id that does not resolve or a line is
  corrupt. It respects `stop_hook_active`, so it cannot loop, and allows the stop with a
  warning when the store is missing. `swarm-mcp info` shows the same check.

`findings/*.jsonl` is gitignored.

## Configuration

Everything has a default. Optional `swarm.toml` at the repo root (relative paths are
against the repo root):

```toml
[data]
dir = "data"                    # datasets; the store is <dir>/swarmscope.duckdb
# db = "data/swarmscope.duckdb"
# findings = "findings"         # findings.jsonl + audit.jsonl
# sweeps = "sweeps"

[llm]
model = "claude-sonnet-5-5"
effort = "low"                  # "none" for models without effort (e.g. Haiku 4.5)
fallbacks = true
# concurrency = 4
# prices = { "my-model" = [1.0, 5.0] }   # USD per 1M tokens, input and output

[privacy]
email_allowlist = ["agentvillage.org"]

[server]
# modules = ["core", "scope", "findings"]   # core is always loaded
# disable = ["wiki"]
max_text = 500
```

Environment variables: `SWARM_DATA_DIR` (overrides `[data] dir`), `ANTHROPIC_API_KEY`
(enables sweeps and `add --agent api`) and `SWARM_LLM_MODEL` (overrides `[llm] model`).
