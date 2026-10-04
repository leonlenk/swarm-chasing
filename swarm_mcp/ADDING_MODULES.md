# swarm-mcp: running it and adding modules

A modular MCP server (stdio) for the swarm-understanding toolkit. Each tool
module is one file (or package) in `src/swarm_mcp/modules/`. Drop one in,
restart the server, and it is discovered automatically.

## Run

```bash
uv run --directory swarm_mcp swarm-mcp                 # stdio server (what Claude Code launches)
uv run --directory swarm_mcp swarm-mcp --list-modules  # print loaded/skipped modules as JSON, then exit
uv run --directory swarm_mcp swarm-mcp ingest ai_village data/ai-village   # build the SwarmScope store
uv run --directory swarm_mcp swarm-mcp render timeline --since 2025-10-20  # HTML swimlane (data/...)
uv run --directory swarm_mcp swarm-mcp check-findings  # every finding's evidence ids resolve? (exit 0/1)
uv run --directory swarm_mcp pytest                    # tests (synthetic data, no dataset needed)
```

The data modules (`scope`, `findings`, `village`) read the SwarmScope DuckDB
store, so run `ingest` once first; until then they are skipped with that reason.

Claude Code picks the server up from the repo's `.mcp.json` (server name `swarm`).
`SWARM_DATA_DIR` there is `${SWARM_DATA_DIR:-data}`, so you can override it from
your shell. The equivalent CLI registration, run from the repo root, is:

```bash
claude mcp add swarm -e SWARM_DATA_DIR=data -- uv run --directory swarm_mcp swarm-mcp
```

That registers it with the default `local` scope, private to you. `--scope project`
writes the same entry into `.mcp.json`, which already exists.

### Configuration (env)

| var | default | meaning |
|---|---|---|
| `SWARM_DATA_DIR` | `data` | dataset root. A relative path is tried against the cwd first, then the repo root (`uv run --directory` changes the cwd) |
| `SWARM_MCP_MODULES` | all | comma list of modules to load (`core` is always loaded unless disabled) |
| `SWARM_MCP_DISABLE` | none | comma list of modules to skip (wins over `SWARM_MCP_MODULES`) |
| `SWARM_MCP_DEFAULT_LIMIT` / `SWARM_MCP_MAX_LIMIT` | 20 / 200 | result-count defaults for `ctx.limit()` |
| `SWARM_MCP_MAX_TEXT` | 500 | default cap for returned dataset text (`ctx.untrusted`) |
| `SWARM_MCP_SCRUB` | 1 | mask emails/phones in returned text (`0` = off) |
| `SWARM_MCP_EMAIL_ALLOWLIST` | `agentvillage.org` | email domains left unmasked |
| `SWARM_MCP_LOG_LEVEL` | INFO | stderr log level |
| `SWARMSCOPE_DB` | `<data dir>/swarmscope.duckdb` | the SwarmScope store (`ctx.store()`) |
| `SWARMSCOPE_FINDINGS_DIR` | `<project root>/findings` | `findings.jsonl` (source of truth) and `audit.jsonl` |
| `SWARM_<MODULE>_<KEY>` | | per-module settings via `ctx.setting("key")`, e.g. `SWARM_VILLAGE_DIR` |
| `SWARM_GIT_DIR` | `<data>/*/repos/` | folder of bare git clones for the `git` and `subtasks` modules |
| `SWARM_WIKI_DB` | `<data>/*/*.db` | a wiki database (collusion.wiki explorer schema) for the `wiki` and `subtasks` modules |

### Modules in this repo

| module | data | what it gives |
|---|---|---|
| `core` | none | module report, config, and `core_get_event` / `core_get_events` / `core_event_sources` for any event id |
| `village` | `<data>/ai-village/*.jsonl.gz` | agents, goals, chat search and windows, per-agent activity |
| `git` | bare clones in `<data>/<dataset>/repos/*.git` | repos and PR listings; PRs and commits as event ids |
| `wiki` | `<data>/<name>/*.db` in the collusion.wiki explorer schema | corpus description with blind spots, search over what each revision added, pages, editor labels; revisions, pages and edit sessions as event ids |
| `subtasks` | any *corpus* with an adapter in `subtasks/sources.py`: git repos (+ village chat if present) and wikis | work units (PRs...) grouped into subtasks by several methods, typed handoffs between actors, pair tracing |

A repo for `git` is a bare clone with every PR head fetched, so closed and squash-merged PRs keep their commits:

```bash
git clone --bare https://github.com/ai-village-agents/rpg-game data/ai-village/repos/rpg-game.git
git -C data/ai-village/repos/rpg-game.git fetch origin '+refs/pull/*/head:refs/pull/*/head'
```

The first `git`/`subtasks` call on a repo loads it (about 15 s for the RPG week's 458 PRs); later calls are instant.

For collusion.wiki, save Simon Willison's SQLite build of the published export as
`data/collusion-wiki/collusion-wiki.db` (https://static.simonwillison.net/static/cors-allow/2026/collusion-wiki.db).
Subtask inference over its ~5,800 edit sessions takes about 10 s on first use.
`examples/subtasks_demo.py` and `examples/wiki_demo.py` run the tools end to end over stdio;
`examples/wiki_eval.py` scores the inferred subtasks against the publishers' page_family labels.

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
| `NAME` | yes (defaults to the file name) | tool-name prefix: tools are named `<NAME>_<function name>` |
| `DESCRIPTION` | recommended | one line, shown in `core_list_modules` and in the server instructions |
| `requires(ctx) -> list[str]` | optional | reasons the module can't load (e.g. a missing data path). A non-empty list means it is skipped. Keep it cheap: check paths, don't load data |
| `register(mcp, ctx)` | yes | registers tools, resources and prompts through `ctx` |

What the server does with each module, in order:
1. **Selection:** `SWARM_MCP_DISABLE` and `SWARM_MCP_MODULES` are checked *before* import.
2. **Import:** an import error skips the module.
3. **`requires()`:** reasons, or an exception, skip the module.
4. **`register()`:** an exception skips the module and **rolls back** anything it had
   already registered.

Every outcome is recorded with its reason, logged to stderr and shown by
`core_list_modules`. A module can never crash the server.

## What `ctx` gives you

| | |
|---|---|
| `@ctx.tool()` | registers `<NAME>_<fn>`. The parameters' type hints and `Annotated[T, Field(description=...)]` become the JSON schema. The docstring becomes the description. Sync functions run in a worker thread |
| `@ctx.resource("path")` | a resource at `<NAME>://path` (`{param}` in the path makes it a template) |
| `@ctx.prompt()` | a prompt named `<NAME>_<fn>` |
| `ctx.lazy(key, loader)` | compute-once, thread-safe cache, namespaced by module. Load data here, not at import or in `register()` |
| `ctx.cache` | the shared `LazyCache` (`get`, `clear(prefix)`, `stats`) |
| `ctx.config`, `ctx.data_dir`, `ctx.setting(key)` | global config, the resolved data path, and per-module env settings |
| `ctx.limit(limit, default=None)` | returns `(effective_limit, note)`. It clamps to [1, max] and says when it did |
| `ctx.untrusted(text, max_chars=None, focus=None)` | **use for every dataset string you return**: masks, caps (default 500) and wraps it as `{"content": ..., "untrusted": true}` (+ `truncated`, `total_chars` when cut; `focus` = regex to centre the snippet on) |
| `ctx.store(read_only=True)` | `with ctx.store() as s:` a short-lived `scope.db.Store` on the DuckDB store (`s.all/one/scalar`, `resolve_agent`, `author_filter`, `resolve_channel`). Open per call; never cache it, so the CLI and hooks can use the file too |
| `ctx.store_path` | the store's path (check it in `requires()`) |
| `ctx.scrub(text)` | masks emails as `[email]` (except allow-listed domains) and phone-like strings as `[phone]` |
| `ctx.event_id(kind, local_id)` | builds an event id owned by this module (see below) |
| `@ctx.event_source(kinds={...})` | registers the resolver that makes this module's event ids retrievable through `core_get_event` |
| `ctx.log` | a logger that writes to stderr |
| `ctx.registry` | records for all modules (used by `core`) |

Helpers in `swarm_mcp.toolkit` are `ToolInputError`, `untrusted`, `snippet`, `truncate`, `parse_time` and `iso`.
The SwarmScope core library (`swarm_mcp.scope`: `schema`, `db`, `evidence`,
`adapters`, `ingest`, `findings`, `analysis`, `viz`) is plain Python, not a
module; tool modules call into it.

## Event ids and shared retrieval

Every piece of evidence a tool returns carries an `event_id` of the form
`<source>:<kind>:<local_id>`, e.g. `village:chat:16b4ab90-…`. `source` is the
dataset (by default the module's `NAME`), `kind` the record type within it, and
`local_id` the source's own id (it may contain `:`). Callers treat ids as opaque
and pass them back: `core_get_event` returns the original record plus its
surrounding context, and `core_get_events` fetches up to 50 at once. Derived
results (search hits, subtasks, handoffs, claims) should cite event ids rather
than copy text, so every finding can be expanded and checked.

Records use one shape across sources, built with `swarm_mcp.events.event_record`:

| key | meaning |
|---|---|
| `event_id`, `source`, `kind` | identity |
| `time` | ISO UTC with `Z` |
| `actor`, `actor_type` | who produced it (agent name, `human:<id>`...) and what kind of actor |
| `location` | where it happened: room, channel, repo... |
| `text` | the content, scrubbed and truncated (`truncated: true` when cut) |

Modules may add keys after these. To make a module's ids retrievable:

```python
from swarm_mcp.events import EventNotFound, event_record

@ctx.event_source(kinds={"thing": "one line on what a thing is and what its context is"})
def resolve(kind, local_id, *, before, after, max_chars):
    rec = lookup(local_id)                      # raise EventNotFound if missing
    return {"event": event_record(ctx.event_id(kind, local_id), time=..., actor=..., text=...),
            "before": [...], "after": [...],     # up to `before`/`after` neighbouring records
            "context": "previous/next things in the same room"}
```

A source name can only be registered once, and a module whose `register()` fails
has its sources removed along with its tools. `core_event_sources` lists what is
loaded.

Register everything through `ctx.*`. The raw `mcp` argument is passed only to
satisfy the contract, and its API depends on the SDK version. `swarm_mcp/sdk.py`
is the only file that touches the MCP SDK.

## Conventions for LLM-friendly tools

- **Structured results.** Return `dict[str, Any]`, not bare `dict`, which the SDK
  can't turn into structured output. Include counts such as `total_matches`,
  `returned` and `has_more`, plus a `notes: list[str]` for anything the caller
  should know (clamped limits, truncation, heuristics).
- **Limits.** Use a default of about 20 and a maximum of 200 through `ctx.limit()`.
  Clamp rather than reject.
- **Dataset text is untrusted.** Return agent/human-authored strings only via
  `ctx.untrusted(...)`, never as bare strings, and give tools that return text a
  `max_chars` parameter (default 500). The server instructions tell the client
  that record contents are data, not instructions.
- **Evidence ids.** Return the evidence id (`{source}:{kind}:{native_id}`) with
  every record so the caller can cite it; `scope.evidence.resolve` checks one.
- **Long text.** `ctx.untrusted` already caps; `truncate(text, n)` adds an explicit
  `…[truncated, N more chars]` marker for anything else.
- **Errors.** `raise ToolInputError("clear, actionable message")`. The caller
  sees exactly that text, with `is_error=true`. Unexpected exceptions become a
  one-line message, and their tracebacks go only to stderr.
- **Never write to stdout** in stdio mode. Use `ctx.log`. Stray prints are
  redirected (during import and register by us, and while serving by the SDK),
  but don't rely on that.
- **Times.** Accept ISO dates or datetimes in UTC through `parse_time` (a bare-date
  upper bound includes that whole day), and return ISO strings with `Z`.
- **Read-only by default.** `ctx.tool()` marks tools `readOnlyHint=true`. Pass
  `read_only=False` for tools that change anything.

## Testing a module

Tests live in `swarm_mcp/tests/`. Generate fixtures in `tmp_path` and never
commit data. `tests/conftest.py` shows the patterns:
- `build_server(config, package=...)` builds an app from a throwaway module package.
- `call(app, "tool", **args)` calls a tool in-process and returns its structured result.
- `call_error(...)` returns the error text from a failing call.
- `mcp.Client(app)` gives a full protocol round trip in-process.

## Synthetic benchmark (`swarm_mcp.bench`)

`swarm_mcp.bench` generates fake swarms in the AI Village file layout with planted
ground truth, then scores investigation tools against it with precision, recall
and F1. The plants are a copied term, a parallel invention, a coordinator, a hidden
activity gap, a homoglyph name pair and attribution gaps. Everything is synthetic.
Write the output to a gitignored place such as `data/` or a temp dir.

```bash
uv run --directory swarm_mcp python -m swarm_mcp.bench generate --out ../data/bench/s1 --seed 1 [--size small|medium]
uv run --directory swarm_mcp python -m swarm_mcp.bench reference --data ../data/bench/s1 --out ../data/bench/s1/outputs.json
uv run --directory swarm_mcp python -m swarm_mcp.bench score --truth ../data/bench/s1/truth.json --outputs ../data/bench/s1/outputs.json
```

`generate` writes `<out>/ai-village/` and `<out>/truth.json`. The scope and village
modules read the SwarmScope store, so ingest it first (for example
`swarm-mcp ingest ai_village <out>/ai-village --db <out>/swarmscope.duckdb`) and point
`SWARM_DATA_DIR` at `<out>`. Truth ids are store evidence ids, so they resolve with
`core_get_event` and `scope_get_record`. The output
shapes the scope tools must return, and the scoring rules, are in
`src/swarm_mcp/bench/contracts.md`.
