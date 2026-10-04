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
adapters (`--adapter village|git` forces one; `--adapter git` also takes the top folder of a
working tree, but never a folder inside a repository; `--name` sets a git source's name).
Wiki databases are only ingested on request: `swarm-mcp add data/collusion-wiki --adapter wiki`
(a `.db` file, or a folder that directly holds one).
For any other dataset it profiles the files, drafts a mapping to `mappings/<source>.json`
(`--agent none` heuristics, `api` an LLM, or `claude-code` a task for the
`/swarm-setup` command), checks it, and stops with the report if the check
fails. When the check passes, it ingests. If `mappings/<source>.json` already
exists, `add` uses it instead of drafting, so your edits survive a re-run
(delete the file to redraft); `--mapping M` uses a mapping from elsewhere.
`--dry-run` ingests nothing: it writes the draft mapping (when there is none
yet) and stops after the check.
Re-running `add` on the same dataset replaces that source and keeps other sources
and findings; so does re-adding a mapped dataset with a changed mapping. If the
source name already holds a different dataset (another adapter or path), `add`
refuses and changes nothing: pick another `--name`, or pass `--replace`. The
store is `data/swarmscope.duckdb`.

On the 2026-09-20 AI Village export, `add` loads 46 agents, 183,485 chat
messages, 104,239 actions and 51 goal periods. `agent_memories` and
computer-use turns are not read.

## Connect to Claude Code

The repo's `.mcp.json` registers the server as **`swarm`**, so Claude Code offers
it when you open the repo; enable it and restart it (`/mcp`) after an `add`. Tools
appear as `mcp__swarm__<tool>`. To register it by hand, from the repo root:

```bash
claude mcp add swarm -- uv run --directory swarm_mcp swarm-mcp
```

Start a session with `core_info`, or with the `investigate` prompt.

## Commands

| command | what it does |
|---|---|
| `swarm-mcp` | run the MCP server on stdio (what Claude Code launches) |
| `swarm-mcp info [--json] [--db]` | modules (loaded or skipped, and why), sources with counts and date ranges, findings health, config. Exits 1 when a finding cites an id that does not resolve |
| `swarm-mcp add <path> [--adapter auto\|village\|git\|wiki\|mapped] [--name SLUG] [--agent none\|api\|claude-code] [--mapping M] [--dry-run] [--replace] [--db]` | add or refresh a dataset in the store (see above) |
| `swarm-mcp render timeline [--since --until --channel --source --top --snippet-chars --out --db] [--no-explore]` | a self-contained HTML explorer in a paper figure style: each agent's activity over time (counts per bin, single messages when zoomed in; Village days when the store has village goals), who names whom in the window on screen, a thread reader (click a message: masked conversation around it, copyable evidence ids), and linked panels for goal recaps, notable moments, one agent over time and metrics over time. Every figure exports the current view as SVG or PNG at 5.5 in. `--no-explore` skips the linked panels. Default output `data/swarmscope-timeline.html` |
| `swarm-mcp render subtasks [--corpus --out --title-chars --db] [--llm-names --llm-cap --llm-min-size]` | a self-contained HTML subtask map from the same inference as the `subtasks_*` tools: one row per inferred subtask on a time axis (switch method and granularity), who did what, typed handoffs with evidence ids, why each unit was grouped, a two-actor pair lens and method agreement. `--corpus` (or `--source`) is any source whose records touch artifacts (a git repo, a wiki); it may be left out when the store has only one. Subtasks are inferred over the whole corpus, so there is no `--since`/`--until`. `--llm-names` names the subtasks with the configured model (needs `ANTHROPIC_API_KEY`; at most `--llm-cap` calls, subtasks of at least `--llm-min-size` units; names are cached next to the store). Default output `data/swarmscope-subtasks-<corpus>.html`, or `data/swarmscope-subtasks.html` when the corpus is left out |
| `swarm-mcp export --out DIR [--source --kind --type --channel --author --since --until --query] [--with-agents] [--keep-ips] [--no-check] [--json] [--db]` | export a redacted subset of the store for sharing, then rescan it (see Export) |

Developer-only: `python -m swarm_mcp.bench generate|reference|score` (see ADDING_MODULES.md).

## Tools and prompt

| tool | what it does |
|---|---|
| `core_info` | start here: modules, sources (row counts, date ranges, channels, blind-spot notes), findings health, config |
| `core_get(ids, before=0, after=0, max_chars)` | the record behind any id, or a batch of up to 50: messages, actions, agents, periods (with member records) and artifacts (with the records that touched them); `before`/`after` add neighbouring records (at most 10 each per id in a batch) |
| `scope_search(query=None, match, source, channel, author, since, until, table, newest_first, limit, offset, max_chars)` | full-text search over messages or actions; with no query, reads the window in time order |
| `scope_agents(name=None, ...)` | the agent list; with a name, that agent's profile (channels, co-presence, who it names, actions, samples) |
| `scope_periods(name=None, source, agent, kind, top, limit, offset)` | dataset periods, paged (AI Village: weekly goals, with a heuristic goal type); with a name, one period's activity |
| `scope_timeline(bin, group_by, table, ...)` | activity counts per hour/day/week/month, optionally by channel or author |
| `scope_graph(...)` | who talks to whom: mention and reply edges, top nodes by degree and betweenness, example ids |
| `scope_recap(period=None, since, until, source, channel, top, max_chars)` | what happened during a period or window: active agents (messages, actions; humans and external actors counted apart), top who-names-whom pairs, terms that rose against the previous window of equal length (log-odds candidates) and the busiest threads with ids and snippets; Village days when the source has village goals |
| `scope_moments(since, until, source, kinds, limit, offset, max_chars)` | where to look: ranked bursts, silences, partner shifts and first uses of spreading terms, each with its numbers (z-score, baseline, counts), Village day and evidence ids; paged |
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
`subtasks_corpora`, `subtasks_list`, `subtasks_get`, `subtasks_trace_pair`,
`subtasks_locate`, `subtasks_graph` and `subtasks_name`: work units (pull requests, runs,
sessions) grouped into subtasks, with typed handoffs between actors, which subtasks built
on which (`subtasks_graph`), and names you or a model give them (`subtasks_name`); see
ADDING_MODULES.md. `village` adds the AI
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
filters, file hashes; never the redacted values). `--kind` is `msg` or `event`; `--type` is the
dataset's own type (e.g. `commit`, `revision`, `session_goal`). `--out` must be a new or empty
folder or a previous export (which is replaced); anything else is refused before writing.
Exports mask private and loopback IPs too (`--keep-ips` leaves them). Afterwards the command rescans everything with every rule
and exits 1 on any hit, hash mismatch or unlisted file; `--no-check` skips that. Emails
at the `email_allowlist` domains are kept.

## Privacy

- Dataset text is returned only as `{"content": ..., "untrusted": true}`, capped at 500
  characters by default (`max_chars`, 20 to 20000, changes it). Each tool response is
  also capped at about 80,000 characters: a list stops early with a "narrow your request"
  note, and paged tools return `next_offset`.
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
  name, arguments, a sha256 of the result and a timestamp to `findings/audit.jsonl` (plus the
  recorded finding ids for `findings_record`). It never blocks, even if the script is missing.
- **Stop `hooks/require_evidence.py`**: blocks stopping (a JSON `{"decision": "block"}` with
  the reasons) while a current finding in `findings/findings.jsonl` (the last line per id, not
  rejected or retracted) cites an id that does not resolve, or a line is corrupt. A finding the
  audit log ties to another session never blocks this one. It respects `stop_hook_active`, so
  it cannot loop, and allows the stop with a warning when the store is missing. It runs with
  plain `python3` and never exits 2: if the check can't run (uv missing, a broken
  `pyproject.toml`, an import error) it allows the stop with a note on stderr.
  `swarm-mcp info` shows the same check.

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
# disable = ["subtasks"]
max_text = 500
```

Environment variables: `SWARM_DATA_DIR` (overrides `[data] dir`), `ANTHROPIC_API_KEY`
(enables sweeps and `add --agent api`) and `SWARM_LLM_MODEL` (overrides `[llm] model`).
