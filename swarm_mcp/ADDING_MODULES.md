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

### Modules in this repo

| module | needs | what it gives |
|---|---|---|
| `core` | nothing | module report and config |
| `scope` | the store | sources (with each one's blind spots), agents, search, `scope_get_record` for any evidence id, message windows, profiles, timelines, communication graphs |
| `findings` | the store | a claim ledger whose evidence ids are checked (also by the Stop hook) |
| `village` | `<data>/ai-village/` | AI Village goal periods and dataset docs |
| `subtasks` | the store, with a source whose records touch artifacts | work units (pull requests, runs, or per-actor sessions) grouped into subtasks by several methods, typed handoffs between actors, pair tracing |

### Adding a dataset = writing an adapter

Datasets enter through adapters (`scope/adapters/`), never through tool modules, so every tool works on
every dataset. An adapter maps its data onto the generic tables (`scope/schema.py`):

| table | holds | examples |
|---|---|---|
| `agents` | actors, with aliases | village agents, git authors, wiki editor labels |
| `messages` | things said | chat lines, wiki revisions (`msg_type`) |
| `actions` | things done | session goals, commits, page deletions (`kind`) |
| `periods` | spans, incl. episodes of work that records point at via `run_id` or `meta.members` | weekly goals, pull requests |
| `artifacts` | things made and changed (`meta.hub`, `meta.role = 'test'`, `meta.category` refine analyses) | files, wiki pages |
| `touches` | which record did what to which artifact: `create` / `modify` / `delete` / `read` / `mention` | a commit creating `src/talents.js`, a revision linking a page |

Evidence ids are `<source>:<kind>:<id>` with **schema** kinds (`msg`, `event`, `agent`, `period`, `artifact`;
AI Village goals keep `goal`), so they resolve the same way for every dataset. The dataset's own type
(commit, revision, pull request) goes in the record's `kind` / `msg_type`, not the id. List the dataset's
blind spots in the adapter's `notes`; `scope_list_sources` shows them. Adapters so far:

```bash
uv run --directory swarm_mcp swarm-mcp ingest ai_village data/ai-village
uv run --directory swarm_mcp swarm-mcp ingest git data/ai-village/repos/rpg-game.git     # source: rpg-game
uv run --directory swarm_mcp swarm-mcp ingest wiki data/collusion-wiki                    # source: collusion-wiki
```

The git adapter expects a bare clone with every PR head fetched, so closed and squash-merged PRs keep their
commits (`git clone --bare <url> data/ai-village/repos/rpg-game.git`, then
`git -C <that> fetch origin '+refs/pull/*/head:refs/pull/*/head'`). For collusion.wiki, save Simon
Willison's SQLite build of the published export as `data/collusion-wiki/collusion-wiki.db`
(https://static.simonwillison.net/static/cors-allow/2026/collusion-wiki.db).

`examples/subtasks_demo.py` and `examples/wiki_demo.py` run the tools end to end over stdio;
`examples/wiki_eval.py` scores inferred subtasks against the wiki publishers' page_family labels.

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
| `ctx.log` | a logger that writes to stderr |
| `ctx.registry` | records for all modules (used by `core`) |

Helpers in `swarm_mcp.toolkit` are `ToolInputError`, `untrusted`, `snippet`, `truncate`, `parse_time` and `iso`.
The SwarmScope core library (`swarm_mcp.scope`: `schema`, `db`, `evidence`,
`adapters`, `ingest`, `findings`, `analysis`, `viz`) is plain Python, not a
module; tool modules call into it.

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
