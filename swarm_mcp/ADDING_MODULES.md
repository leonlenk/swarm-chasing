# swarm-mcp for developers: modules, adapters, mappings, the bench

User-facing commands, tools and configuration are in [README.md](README.md). This
file covers extending the server. Each tool module is one file (or package) in
`src/swarm_mcp/modules/`: drop one in, restart the server, and it is discovered.

```bash
uv run --directory swarm_mcp swarm-mcp info     # what loaded or was skipped, and why
uv run --directory swarm_mcp pytest             # synthetic data only
uvx ruff check swarm_mcp
```

## A module in five lines

```python
# src/swarm_mcp/modules/hello.py
NAME, DESCRIPTION = "hello", "Greets people."
def register(mcp, ctx):
    @ctx.tool()
    def greet(name: str) -> dict[str, str]: return {"greeting": f"hi {name}"}   # -> tool "hello_greet"
```

For a fuller example, copy `src/swarm_mcp/modules/_template.py`. Files that start
with `_` are never loaded.

## The contract

| name | required | meaning |
|---|---|---|
| `NAME` | yes (defaults to the file name) | tool-name prefix: tools are named `<NAME>_<function name>`; a function named exactly `NAME` keeps that name (e.g. the `investigate` prompt) |
| `DESCRIPTION` | recommended | one line, shown in `core_info` and in the server instructions |
| `requires(ctx) -> list[str]` | optional | reasons the module can't load (e.g. a missing data path). A non-empty list means it is skipped. Keep it cheap: check paths, don't load data |
| `register(mcp, ctx)` | yes | registers tools, resources and prompts through `ctx` |

What the server does with each module, in order:
1. **Selection:** `[server] disable` and `[server] modules` in `swarm.toml` are checked *before* import.
2. **Import:** an import error skips the module.
3. **`requires()`:** reasons, or an exception, skip the module.
4. **`register()`:** an exception skips the module and **rolls back** anything it had
   already registered (tools, resources and prompts).

Every outcome is recorded with its reason, logged to stderr and shown by `core_info`
and `swarm-mcp info`. A module can never crash the server. Keep the tool count small:
prefer one tool with an optional argument (`scope_agents(name=None)`) over two tools.

## What `ctx` gives you

| | |
|---|---|
| `@ctx.tool()` | registers `<NAME>_<fn>`. The parameters' type hints and `Annotated[T, Field(description=...)]` become the JSON schema. The docstring becomes the description. Sync functions run in a worker thread |
| `@ctx.resource("path")` | a resource at `<NAME>://path` (`{param}` in the path makes it a template) |
| `@ctx.prompt()` | a prompt named `<NAME>_<fn>` |
| `ctx.lazy(key, loader)` | compute-once, thread-safe cache, namespaced by module. Load data here, not at import or in `register()` |
| `ctx.cache` | the shared `LazyCache` (`get`, `clear(prefix)`, `stats`) |
| `ctx.config`, `ctx.data_dir` | the `Config` (from `swarm.toml`) and the resolved data path |
| `ctx.setting(key)` | per-module setting `[modules.<NAME>] key = ...` in `swarm.toml` (`village` also accepts `SWARM_VILLAGE_DIR`) |
| `ctx.limit(limit, default=None)` | returns `(effective_limit, note)`. It clamps to [1, max] and says when it did |
| `ctx.untrusted(text, max_chars=None, focus=None)` | **use for every dataset string you return**: masks, caps (default 500) and wraps it as `{"content": ..., "untrusted": true}` (+ `truncated`, `total_chars` when cut; `focus` = regex to centre the snippet on) |
| `ctx.store(read_only=True)` | `with ctx.store() as s:` a short-lived `scope.db.Store` on the DuckDB store (`s.all/one/scalar`, `resolve_agent`, `author_filter`, `resolve_channel`). Open per call; never cache it, so the CLI and hooks can use the file too |
| `ctx.store_path` | the store's path (check it in `requires()`) |
| `ctx.scrub(text)` | the masking engine (`redact.mask_text`, default rules) with the configured email allowlist |
| `ctx.log` | a logger that writes to stderr |
| `ctx.registry` | records for all modules, the store resolver (`store_api`) and the sweep record providers |

Helpers in `swarm_mcp.toolkit` are `ToolInputError`, `untrusted`, `snippet`, `truncate`, `parse_time` and `iso`.
The SwarmScope core library (`swarm_mcp.scope`: `schema`, `db`, `evidence`, `adapters`,
`ingest`, `records`, `findings`, `analysis`, `viz`) is plain Python, not a module; tool
modules and the CLI call into it.

## Ids and retrieval

Every record in the store has an id `<source>:<kind>:<native_id>` (`scope/evidence.py`).
`source` is the ingested dataset; `kind` is a schema kind, the same for every dataset:
`msg` (messages), `event` (actions), `agent`, `period` (time periods and episodes of
work such as pull requests) and `artifact` (files, pages); AI Village goals keep `goal`.
The dataset's own type lives in the record (`messages.msg_type`, `actions.kind`,
`periods.kind`). `native_id` may contain `:`. Callers treat ids as opaque and pass them
back: `core_get` resolves one id or a batch through the scope module's store resolver
(`ctx.registry.store_api["get_record"]`), with the artifacts a record touched, a period's
member records and optional neighbours. Derived results (search hits, subtasks, handoffs,
claims) cite ids rather than copy text, so every finding can be expanded and checked.

Sweeps and exports work on one flat record shape, `scope.records.event_record`:
`event_id`, `source`, `kind`, `time` (ISO UTC with `Z`), `actor`, `actor_type`, `location`
and `text` (masked; `truncated: true` when cut), plus extra keys.

Register everything through `ctx.*`. The raw `mcp` argument is passed only to satisfy the
contract, and its API depends on the SDK version. `swarm_mcp/sdk.py` is the only file
that touches the MCP SDK.

## Conventions for LLM-friendly tools

- **Structured results.** Return `dict[str, Any]`. Include counts such as `total`,
  `returned` and `has_more` (with `next_offset` for paging), plus `notes: list[str]` for
  anything the caller should know (clamped limits, truncation, heuristics).
- **Limits.** A default of about 20 and a maximum of 200 through `ctx.limit()`. Clamp
  rather than reject.
- **Dataset text is untrusted.** Return agent/human-authored strings only via
  `ctx.untrusted(...)`, and give tools that return text a `max_chars` parameter.
- **Ids.** Return the id with every record so the caller can cite it.
- **Errors.** `raise ToolInputError("clear, actionable message")`. The caller sees exactly
  that text, with `is_error=true`. Unexpected exceptions become a one-line message; their
  tracebacks go only to stderr.
- **Never write to stdout** in stdio mode. Use `ctx.log`.
- **Times.** Accept ISO dates or datetimes in UTC through `parse_time` (a bare-date upper
  bound includes that whole day), and return ISO strings with `Z`.
- **Read-only by default.** `ctx.tool()` marks tools `readOnlyHint=true`. Pass
  `read_only=False` for tools that change anything.

## The store, adapters and record providers

`swarm-mcp add` fills the store through an adapter (`scope/adapters/`). An adapter has a
`name`, a `source` (the id prefix), `inspect(path)` and `load(path)`, which yields
`(table, row)` pairs for the `agents`, `messages`, `actions`, `periods`, `artifacts` and
`touches` tables (`scope/schema.py`, schema v2), and optional `notes` (its blind spots,
shown by `core_info`). `scope.ingest.ingest(adapter, path, db)` validates the rows, bulk-loads
them and replaces that source's rows in one transaction; an adapter's optional
`source_meta` dict lands in `sources.meta`.

- `ai_village` (`scope/adapters/ai_village.py`): the hand-written AI Village adapter.
  Agents (aliases such as "Opus 4.5"; model, lab and join date in `meta`), chat messages
  (`channel` = room name; `author_id` = agent id or `human:<user id>`; `recipient_ids` =
  agents named in the text), actions from `events.jsonl.gz` (`session_goal`,
  `session_summary`) and the weekly goals as periods.
- `git` (`scope/adapters/git_repo.py`, Rigel's): a bare clone with PR heads fetched.
  Commits become actions, pull requests periods with member records, files artifacts and
  file changes touches. `swarm-mcp add <repo.git>` detects it; `--name` sets the source.
- `wiki` (`scope/adapters/wiki_db.py`, Rigel's): the collusion.wiki explorer schema.
  Revisions become messages (with `reply_to`), deletions and reverts actions, pages
  artifacts and links mention touches. Only via `swarm-mcp add <dir> --adapter wiki`.
- `claude_code` (`scope/adapters/claude_code.py`, Anand's): Claude Code sessions recorded by
  the swarm-live hooks (`swarm_mcp/live/`). Main sessions and subagents become agents and
  periods (`<session>:main`, `<session>:<agent id>`), tool calls actions (`kind` = tool name),
  user prompts, Agent/Task delegations (parent → subagent) and agent text messages (a
  subagent's final answer goes to its parent with `reply_to` = its delegation), and files
  named by Read/Write/Edit tools artifacts and touches. Source `claude-code`;
  `swarm-mcp add <recordings .db>` detects it, or the `claude_code_sync` tool.
- `mapped` (`scope/adapters/mapped.py`): runs a declarative mapping (below) and converts
  its records: category `message` → `messages` (`<source>:msg:<kind>/<id>`, the dataset
  kind in `msg_type`), `action` and `other` → `actions` (`<source>:event:<kind>/<id>`, the
  dataset kind in `kind`), periods → `<source>:period:<kind>/<id>`, then agents
  (`<source>:agent:<id>`). The `<kind>/` prefix keeps ids from different record entries
  apart. `scope.ingest.ingest_mapped(mapping, path, db)` is the library entry point.

`scope.records.store_records(db, filters)` streams standard records out of the store with
the filters `source`, `kind` (schema kind: `msg` or `event`), `type` (the dataset type:
`msg_type` or `actions.kind`), `channel`, `author`, `since`, `until` and `query`. The scope
module registers `StoreRecordProvider` as the `store` record provider for
`sweep_run(filters=...)`, and `scope.records.export_store` feeds the same records to
`export.export` with a `redact.Redactor`. Any module can register another provider with
`swarm_mcp.sweep.register_provider(ctx.registry, name, provider)` (an object with
`iter_records(filters, limit)`).

## Mapping a new dataset

`swarm-mcp add <path>` profiles any dataset that is not AI Village, drafts a mapping
(`<repo>/mappings/<source>.json`), checks it and ingests it. The code is in
`swarm_mcp.setup`: `readers` (formats), `profile`, `mapping` (`MappedAdapter`), `check`,
`agent` (drafting) and `protocol_bridge` (the `AgentRecord`/`StandardRecord`/`PeriodRecord`
types). A mapping is pure data: nothing in it is evaluated, imported or used as a regex.

**Formats.** jsonl/ndjson (optionally `.gz`; table key = relative path), json arrays, json
objects holding arrays (`file.json#key`), csv/tsv (delimiter sniffed), parquet, and sqlite
(`file.db#table`). The profiler reads only the first 1000 rows (and at most 8 MB) per
table, and guesses roles (`id`, `time` with its format, `actor`, `actor_type`, `location`,
`text`, `reply_to`, `recipients`), foreign keys by value overlap, and the agents table.
Example values in profiles and check reports are masked and cut to 80 characters.

**Drafting** (`--agent`): `none` builds a heuristic draft with `TODO` notes for
low-confidence choices; `api` gives an LLM the profile, the schema and the draft, and
feeds check failures back for up to 3 rounds (dataset content is wrapped in
`<data untrusted="true">`); `claude-code` writes `mappings/<source>.task.md` for the
`/swarm-setup` slash command, which edits the mapping and runs
`swarm-mcp add <path> --mapping M --dry-run` until it passes.

**Spec.** The JSON Schema is `MAPPING_SCHEMA` in `src/swarm_mcp/setup/spec_schema.py`.
Paths are dotted field paths (`speaker.id`; `mentions[].id` collects from every array
element). Every role that takes an object also accepts a plain path string.

| key | meaning |
|---|---|
| `source` (required) | the id source slug (`^[a-z][a-z0-9_-]*$`) |
| `description`, `root`, `email_allowlist`, `notes` | one line; default dataset path; domains left unmasked; free-form notes (the draft's `TODO`s) |
| `agents` | `{from, id, display_name, aliases, meta, where, derive_from_actors}`. Without `from`, every distinct actor value becomes an agent |
| `lookups` | `{name: {from, key, value}}` tables for joins (a field spec with `"lookup": name` translates through it) |
| `records[]` (required) | one entry per kind: `from`, `kind`, `category` (`message` default, `action`, `other`), `local_id` (a path, a list joined with `:`, or `@row`), `time` (`{field, format: auto\|iso\|epoch_s\|epoch_ms\|epoch_us\|rfc2822\|strptime, pattern}`), `ts_quality`, `actor` (`{field, match: id\|name\|any, fallback_field, fallback_prefix, unmatched_prefix}`), `actor_type`, `location`, `type`, `text` (`{field}` or `{fields, sep}`), `reply_to` (`{field, kind}`), `recipients` (`{field, match, split}`), `text_mentions` (`at` or `names`), `meta`, `where` |
| `periods[]` | `from`, `kind`, `local_id`, `label`, `start`, `end`, `meta`, `where` |

`where` is a list of `{field, op, value}` with ops `==` (default), `!=`, `in`, `not_in`,
`exists`, `missing` and `contains`. Value fields (`actor_type`, `location`, `type`) take a
path or `{field, value, lookup, values, default}`. A resolved actor becomes
`<source>:agent:<id>`; an empty actor with a `fallback_field` value becomes
`<fallback_prefix><value>` (default `human:`); an unresolved one becomes
`<unmatched_prefix><value>` (default `external:`).

Example, a sqlite forum with `ppl(uid, handle, nick_list)`, `thr(thrId, subj)` and
`msgs(mid, thr_ref, writer, epochMillis, txt, re_mid, cc)`:

```json
{
  "source": "forum",
  "agents": {"from": "forum.sqlite#ppl", "id": "uid", "display_name": "handle", "aliases": ["nick_list"]},
  "lookups": {"threads": {"from": "forum.sqlite#thr", "key": "thrId", "value": "subj"}},
  "records": [{
    "from": "forum.sqlite#msgs", "kind": "post", "local_id": "mid",
    "time": {"field": "epochMillis", "format": "epoch_ms"},
    "actor": {"field": "writer", "match": "id"},
    "location": {"field": "thr_ref", "lookup": "threads"},
    "text": "txt",
    "reply_to": {"field": "re_mid", "kind": "post"},
    "recipients": {"field": "cc", "match": "name", "split": ","},
    "text_mentions": "at"
  }]
}
```

**The check** runs the mapping on a sample (2000 rows per table) and fails on errors:
`spec_invalid`, `table_missing`, `field_missing` (with close field names), `no_records`,
`id_duplicate`, `id_unparseable`, `record_invalid`, `no_agents`, and above a threshold
`id_missing` (>5%), `time_unparseable` / `time_out_of_range` (>1%), `actor_unmatched` and
`text_empty` (>20%). `time_missing`, `actor_missing`, `recipient_unmatched` and
`reply_dangling` are warnings. Each problem is reported with counts and up to 5 masked
examples.

## LLM calls

`swarm_mcp/llm.py` is the only model seam: the `LLMClient` protocol
(`complete(system, prompt, max_tokens) -> LLMResult`), `AnthropicClient`, `FakeClient` for
tests, and `get_client(config)`, which refuses to build a client without
`ANTHROPIC_API_KEY`. `swarm_mcp/sweep.py` is the store-agnostic sweep engine (`estimate`,
`run`, `parse_verdict`, `sample_for_labeling`, `pending_labels`, `label`, `precision`,
`wilson_interval`); it has no MCP dependency.

## Other modules in this repo

`subtasks` (Rigel's) reads only the store: units are periods that records point at (pull
requests, runs), else per-actor sessions; handoffs come from touches. Its tools are
`subtasks_corpora`, `subtasks_list`, `subtasks_get`, `subtasks_trace_pair` and
`subtasks_locate`. For git data, a repo is a bare clone with every PR head fetched:

```bash
git clone --bare https://github.com/ai-village-agents/rpg-game data/ai-village/repos/rpg-game.git
git -C data/ai-village/repos/rpg-game.git fetch origin '+refs/pull/*/head:refs/pull/*/head'
uv run --directory swarm_mcp swarm-mcp add data/ai-village/repos/rpg-game.git   # source rpg-game
```

`claude_code` (Anand's) has one tool, `claude_code_sync`, which re-ingests the swarm-live
recordings (`[modules.claude_code] db`, else `SWARM_LIVE_DB`, `$CLAUDE_PLUGIN_DATA/swarm-live.db`
or `~/.swarm-live/swarm-live.db`) as source `claude-code`. Inside the plugin it also syncs at
startup; it sorts before `findings` and `scope`, so it can create the store they check for.

## Testing a module

Tests live in `swarm_mcp/tests/`. Generate fixtures in `tmp_path` and never commit data.
`tests/conftest.py` shows the patterns:
- `config_for(data_dir, db=..., modules=..., disable=..., llm=..., settings=...)` builds a
  `Config` as if from a `swarm.toml`, with no real environment leaking in.
- `build_server(config, package=...)` builds an app from a throwaway module package.
- `call(app, "tool", **args)` calls a tool in-process and returns its structured result;
  `call_error(...)` returns the error text from a failing call.
- `mcp.Client(app)` gives a full protocol round trip in-process.

## Synthetic benchmark (`swarm_mcp.bench`, developer-only)

`swarm_mcp.bench` generates fake swarms in the AI Village file layout with planted ground
truth, then scores investigation tools against it with precision, recall and F1. The
plants are a copied term, a parallel invention, a coordinator, a hidden activity gap, a
homoglyph name pair and attribution gaps. Write the output somewhere gitignored.

```bash
uv run --directory swarm_mcp python -m swarm_mcp.bench generate --out ../data/bench/s1 --seed 1 [--size small|medium]
uv run --directory swarm_mcp swarm-mcp add ../data/bench/s1 --db ../data/bench/s1/swarmscope.duckdb
uv run --directory swarm_mcp python -m swarm_mcp.bench reference --data ../data/bench/s1 --out ../data/bench/s1/outputs.json
uv run --directory swarm_mcp python -m swarm_mcp.bench score --truth ../data/bench/s1/truth.json --outputs ../data/bench/s1/outputs.json
```

`generate` writes `<out>/ai-village/` and `<out>/truth.json`; `add` detects the layout.
Truth ids are store ids (`village:msg:<uuid>`...), so they resolve with `core_get`. The output shapes the
investigation tools must return, and the scoring rules, are in the docstring of
`src/swarm_mcp/bench/score.py`.
