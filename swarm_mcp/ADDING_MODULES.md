# swarm-mcp: running it and adding modules

A modular MCP server (stdio) for the swarm-understanding toolkit. Each tool
module is one file (or package) in `src/swarm_mcp/modules/`. Drop one in,
restart the server, and it is discovered automatically.

## Run

```bash
uv run --directory swarm_mcp swarm-mcp                 # stdio server (what Claude Code launches)
uv run --directory swarm_mcp swarm-mcp --list-modules  # print loaded/skipped modules as JSON, then exit
uv run --directory swarm_mcp pytest                    # tests (synthetic data, no dataset needed)
```

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
| `SWARM_MCP_MAX_TEXT` | 1000 | default per-field text truncation |
| `SWARM_MCP_SCRUB` | 1 | mask emails/phones in returned text (`0` = off) |
| `SWARM_MCP_EMAIL_ALLOWLIST` | `agentvillage.org` | email domains left unmasked |
| `SWARM_MCP_LOG_LEVEL` | INFO | stderr log level |
| `SWARM_<MODULE>_<KEY>` | | per-module settings via `ctx.setting("key")`, e.g. `SWARM_VILLAGE_DIR` |
| `SWARM_GIT_DIR` | `<data>/*/repos/` | folder of bare git clones for the `git` and `subtasks` modules |

### Modules in this repo

| module | data | what it gives |
|---|---|---|
| `core` | none | module report, config, and `core_get_event` / `core_get_events` / `core_event_sources` for any event id |
| `village` | `<data>/ai-village/*.jsonl.gz` | agents, goals, chat search and windows, per-agent activity |
| `git` | bare clones in `<data>/<dataset>/repos/*.git` | repos and PR listings; PRs and commits as event ids |
| `subtasks` | `git` repos (+ village chat if present) | PRs grouped into subtasks by several methods, typed handoffs between agents, pair tracing |

A repo for `git` is a bare clone with every PR head fetched, so closed and squash-merged PRs keep their commits:

```bash
git clone --bare https://github.com/ai-village-agents/rpg-game data/ai-village/repos/rpg-game.git
git -C data/ai-village/repos/rpg-game.git fetch origin '+refs/pull/*/head:refs/pull/*/head'
```

The first `git`/`subtasks` call on a repo loads it (about 15 s for the RPG week's 458 PRs); later calls are instant.

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
| `ctx.scrub(text)` | masks emails as `[email]` (except allow-listed domains) and phone-like strings as `[phone]` |
| `ctx.event_id(kind, local_id)` | builds an event id owned by this module (see below) |
| `@ctx.event_source(kinds={...})` | registers the resolver that makes this module's event ids retrievable through `core_get_event` |
| `ctx.log` | a logger that writes to stderr |
| `ctx.registry` | records for all modules (used by `core`) |

Helpers in `swarm_mcp.toolkit` are `ToolInputError`, `truncate`, `parse_time` and `iso`.

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
- **Long text.** Use `truncate(text, n)`, which adds an explicit
  `…[truncated, N more chars]` marker. Also set a `truncated: true` flag and
  say how to get more.
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
