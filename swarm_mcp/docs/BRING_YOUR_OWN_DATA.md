# Bring your own dataset

`swarm_mcp.setup` turns a new multi-agent dataset (chat logs, forums, agent traces)
into the standard event records every swarm-mcp tool understands. You point it at
a file or folder, and it:

1. **profiles** the data: tables, fields, and guesses at what each field means;
2. **drafts a mapping**: a declarative JSON file saying which field is the id, time,
   actor, text and so on. The draft comes from the heuristics, an LLM, or a Claude Code agent;
3. **checks** the mapping by running it and validating the records it produces;
4. hands you the **ingest** command.

The mapping is pure data. Nothing in it is ever evaluated, imported or executed.

## Commands

All commands run through the standalone entry point. CLI wiring into `swarm-mcp` comes later.

```bash
uv run --directory swarm_mcp python -m swarm_mcp.setup inspect <path> [--rows N] [--out FILE] [--json]
uv run --directory swarm_mcp python -m swarm_mcp.setup setup <source> <path> [--agent none|api|claude-code] [--mappings-dir DIR] [--rounds 3]
uv run --directory swarm_mcp python -m swarm_mcp.setup check <mapping.json> [<path>] [--full] [--rows N] [--json]
uv run --directory swarm_mcp python -m swarm_mcp.setup schema
```

`uv run --directory swarm_mcp` runs from `swarm_mcp/`. The CLI compensates: inputs (the
dataset, the mapping file) resolve like `SWARM_DATA_DIR` (cwd first, then the project root),
and outputs (`--out`, `--mappings-dir`, default `mappings/`) go under the project root. So
`... setup forum data/forum` from the repo root writes `<repo>/mappings/forum.json`.
Absolute paths always work.

| command | what it does | exit code |
|---|---|---|
| `inspect` | profiles every table and writes `<path>/.swarmscope/profile.json` (or `--out`). Prints a summary, or the full JSON with `--json`. `--rows` sets the rows sampled per table (default 1000) | 0 |
| `setup` | profiles the data, drafts `mappings/<source>.json`, checks the draft, and writes the outputs (see [Outputs](#outputs)) | 0 if the check passes (always 0 for `claude-code`), 1 if it fails, 2 on a setup error |
| `check` | runs a mapping on a sample (2000 rows per table, `--rows`), or on everything with `--full`, and prints a report (`--json` for JSON). Without `<path>`, the dataset path comes from the mapping's `root` | 0 pass, 1 fail, 2 bad mapping or path |
| `schema` | prints the mapping JSON Schema (draft 2020-12) | 0 |

## The profile

`inspect` walks a file or folder. Hidden folders, caches and `.swarmscope/` are skipped.
It recognises these tables:

| format | files | table key |
|---|---|---|
| jsonl | `.jsonl` / `.ndjson`, optionally `.gz` | relative path, e.g. `logs/chat.jsonl.gz` |
| json | `.json` (.gz) holding an array of objects | relative path |
| json | `.json` object holding arrays (up to 64 MB) | `file.json#key` per array (`#outer.inner` one level down) |
| csv / tsv | `.csv` / `.tsv` (.gz), delimiter sniffed | relative path |
| parquet | `.parquet` (needs `duckdb` or `pyarrow`) | relative path |
| sqlite | `.db` / `.sqlite` / `.sqlite3`, opened read-only | `file.db#table` per table |

Docs (`.md`, `.txt`, `.rst`) are listed with their first heading. Other files are
listed as skipped.

**Sampling.** For each table, `inspect` reads only the first `--rows` rows and stops after
8 MB of decompressed data. A 2.4 GB gzipped JSONL file costs about as much as a small one.
Row counts are exact when the file was read to the end or the format stores them
(sqlite, parquet). Otherwise they are estimated from the bytes consumed.

**Per table:**

- the row count, the format, and a `looks_like` label: `agents`, `records`, `periods`, `lookup` or `other`;
- every field as a dotted path, with its types, `null_rate`, `empty_rate`, `distinct_in_sample`,
  `unique_ratio`, average length, the time format and parse rate, and **3 example values**.
  Examples are cut to 80 characters, with emails shown as `[email]` and phones as `[phone]`;
- **role guesses** with scores in [0, 1] and the reasons for each, for `id`, `time` (with
  its format: `iso`, `epoch_s`, `epoch_ms`, `epoch_us`, `rfc2822`), `actor`, `actor_type`,
  `location`, `text`, `reply_to` and `recipients`.

**Across tables:**

- `foreign_keys`: links found by value overlap in the samples, e.g.
  `chat.jsonl.gz::agent_speaker_id -> agents.jsonl.gz::id`. Sqlite tables also list their declared FKs.
- `agents_table`: the most likely agents table, with its id, display name and alias fields.

The scores are heuristics: name tokens plus value evidence. Check them against the dataset's docs.

## Mapping spec reference

The JSON Schema is printed by `python -m swarm_mcp.setup schema`, and lives in
`src/swarm_mcp/setup/spec_schema.py`.

**Paths.** A path is a dotted field path into a row, e.g. `speaker.id`. A `[]` marks an
array of objects: `mentions[].id` collects `id` from every element. Arrays met along
the way are mapped over and flattened. A key that literally contains dots, such as a CSV
header `user.name`, is matched first.

**Field-spec shorthand.** Every role that takes an object also accepts a plain path
string: `"time": "ts"` means `{"field": "ts", "format": "auto"}`.

### Top level

| key | required | meaning |
|---|---|---|
| `mapping_version` | no | `1` |
| `source` | yes | the event-id source slug (`^[a-z][a-z0-9_-]*$`), e.g. `forum` |
| `description` | no | one line about the dataset |
| `root` | no | default dataset path, used by `check` when no path is given |
| `email_allowlist` | no | email domains left unmasked in this source's text |
| `notes` | no | free-form strings, e.g. the draft's `TODO`s. These are ignored |
| `agents` | no | where agents come from (below) |
| `lookups` | no | named key → value tables used for joins |
| `records` | yes | one entry per record kind |
| `periods` | no | dataset periods, e.g. weekly goals |

### `agents`

| key | meaning |
|---|---|
| `from` | the table key or glob, e.g. `agents.jsonl.gz` or `forum.db#users`. If you omit it, every distinct actor value becomes an agent |
| `id` | path to the agent id (required when `from` is set) |
| `display_name` | path to the display name (defaults to the id) |
| `aliases` | paths to alternative names, as strings or lists |
| `meta` | fields to keep, as a list of paths or `{out_name: path}` |
| `where` | row filters (see `where` below) |
| `derive_from_actors` | when `true`, every distinct actor value becomes an agent, even if `from` is set |

### `lookups`

`{"room_names": {"from": "rooms.csv", "key": "id", "value": "name"}}`. A field spec
with `"lookup": "room_names"` translates its raw value through this table. The lookup
table is read whole, up to 1M rows.

### `records[]`

| key | meaning |
|---|---|
| `from` | the table key or glob (`logs/*.jsonl`, `forum.db#posts`, `dump.json#messages`). It is required |
| `kind` | the kind slug, unique across records and periods. `agent` is reserved. It is required |
| `category` | `message` (communication, so text must be non-empty, the default), `action` or `other` |
| `description` | one line about this kind |
| `where` | `[{"field", "op", "value"}]`. The ops are `==` (the default), `!=`, `in`, `not_in`, `exists`, `missing` and `contains`. A row is kept only if every condition holds, and values are compared as-is or as strings |
| `local_id` | a path, a list of paths (joined with `:`), or `"@row"` for `<table>:<row number>`, which is not stable across exports. It is required |
| `time` | a path or `{field, format, pattern}`. The formats are `auto`, `iso`, `epoch_s`, `epoch_ms`, `epoch_us`, `rfc2822` and `strptime` (which needs `pattern`, e.g. `%d/%m/%Y %H:%M`). Naive times are taken as UTC |
| `ts_quality` | `exact`, `approx`, `derived` or `missing`. Defaults to `exact` when there is a time, else `missing` |
| `actor` | a path or `{field, match, fallback_field, fallback_prefix, unmatched_prefix}` (below) |
| `actor_type` | a value field (below). When it is omitted, the type is `agent` for resolved agents and `human` for fallback actors |
| `location` | a value field: a room, channel, thread or repo |
| `type` | a value field: an optional subtype |
| `text` | a path or `{field}` / `{fields: [...], sep: "\n\n"}`, where the fields are concatenated |
| `reply_to` | a path or `{field, kind}`. `kind` is the kind of the target record (the default is this one) |
| `recipients` | a path or `{field, match, split}`. `split` separates a single string, e.g. `","` |
| `text_mentions` | `"at"` adds agents mentioned as `@handle`, and `"names"` adds any agent name or alias in the text (whole words, 3+ characters) as recipients |
| `meta` | fields to keep, as a list of paths or `{out_name: path}` |

**Actor resolution.** `match` sets how the value is matched: `id` matches agent ids,
`name` matches display names and aliases (case-insensitive), and `any` (the default)
tries both. A resolved actor becomes `<source>:agent:<id>`. If `field` is empty and
`fallback_field` has a value, the actor is `<fallback_prefix><value>` (default
`human:`), which is useful when human speakers sit in a separate column. A value that
resolves to no agent becomes `<unmatched_prefix><value>` (default `external:`) and is
counted by the check.

**Value fields** (`actor_type`, `location`, `type`) take a path, or an object with these keys:

| key | meaning |
|---|---|
| `field` | the path to read |
| `value` | a constant, used when there is no `field` |
| `lookup` | the name of a `lookups` entry to translate through |
| `values` | a raw → output map, e.g. `{"user": "human", "bot": "agent"}`. Unmapped values pass through |
| `default` | used when the result is empty |

### `periods[]`

| key | meaning |
|---|---|
| `from`, `kind`, `local_id` | as in records (all three are required) |
| `description`, `where`, `meta` | as in records |
| `label` | path to the period label |
| `start`, `end` | time specs, as in records |

### Worked example: a sqlite forum

`forum.sqlite` has the tables `ppl(uid, handle, nick_list)`, `thr(thrId, subj)` and
`msgs(mid, thr_ref, writer, epochMillis, txt, re_mid, cc)`. Here `writer` holds a
`ppl.uid`, `re_mid` is the message being replied to, and `cc` is a comma list of handles.

```json
{
  "mapping_version": 1,
  "source": "forum",
  "agents": {"from": "forum.sqlite#ppl", "id": "uid", "display_name": "handle", "aliases": ["nick_list"]},
  "lookups": {"threads": {"from": "forum.sqlite#thr", "key": "thrId", "value": "subj"}},
  "records": [
    {
      "from": "forum.sqlite#msgs",
      "kind": "post",
      "category": "message",
      "local_id": "mid",
      "time": {"field": "epochMillis", "format": "epoch_ms"},
      "actor": {"field": "writer", "match": "id"},
      "location": {"field": "thr_ref", "lookup": "threads"},
      "text": "txt",
      "reply_to": {"field": "re_mid", "kind": "post"},
      "recipients": {"field": "cc", "match": "name", "split": ","},
      "text_mentions": "at"
    }
  ]
}
```

This mapping gives records `forum:post:<mid>`, each with actor `forum:agent:<uid>`, the
thread subject as its location, and the replied-to message as `reply_to` (`forum:post:<re_mid>`).

## The check

`check` runs the mapping, by default on a sample, and validates what it yields. Every
problem is reported with counts and up to 5 masked examples.

| code                 | severity        | what                                                        |
|----------------------|-----------------|-------------------------------------------------------------|
| spec_invalid         | error           | JSON Schema / semantic errors in the mapping                |
| table_missing        | error           | a `from` matches no file or table                           |
| field_missing        | error           | a mapped field path never occurs in the sampled rows        |
| no_records           | error           | a records entry yields nothing                              |
| id_missing           | error >5% / warn| rows dropped because local_id is empty                      |
| id_duplicate         | error           | two records with the same event id                          |
| id_unparseable       | error           | an id fails `parse_event_id` or does not parse to itself    |
| record_invalid       | error           | `events.event_record` rejects a record                      |
| time_unparseable     | error >1% / warn| time present but not parseable with the given format        |
| time_out_of_range    | error >1% / warn| parsed time outside 1990–2100 (wrong epoch unit?)           |
| time_missing         | warn >5%        | no time value                                               |
| actor_unmatched      | error >20% / warn| actor value resolves to no agent                           |
| actor_missing        | warn >5%        | no actor value (and no fallback)                            |
| text_empty           | error >20% / warn| message-category record with empty text                    |
| recipient_unmatched  | warn            | recipient values that resolve to no agent                   |
| reply_dangling       | warn >10% (full), >50% (sample) | reply_to targets not among the mapped ids |
| no_agents            | error           | records have actors but no agents were loaded               |

The status is `pass` when there are no errors; warnings are allowed. In sample mode,
reply targets outside the sample count as dangling, so `reply_dangling` only matters
with `--full`. A `field_missing` error suggests close field names that do exist.

## Agent modes (`setup --agent`)

| mode | what happens |
|---|---|
| `none` (the default) | Builds a heuristic draft from the profile's top role guesses. Low-confidence choices get `TODO` entries in the mapping's `notes`. The draft is checked once. No model and no network |
| `api` | An LLM, via `swarm_mcp.llm.get_client()`, gets the profile, the mapping JSON Schema and the heuristic draft, and returns `{"mapping": …, "rationale": …}`. The mapping is validated and checked, and failures are fed back, for at most `--rounds` (default 3) model calls. This needs `ANTHROPIC_API_KEY`. Without it, setup stops with an error suggesting `--agent none` or `--agent claude-code`. The model and effort come from `SWARM_MCP_LLM_MODEL` / `SWARM_MCP_LLM_EFFORT` |
| `claude-code` | Writes the heuristic draft plus `mappings/<source>.task.md`, which holds a profile summary (field names and guesses only), the schema and check commands, and the done criteria. Then run `/swarm-setup <source> <path>` in Claude Code (`.claude/commands/swarm-setup.md`) |

In `api` mode, everything derived from the dataset (the profile, the heuristic draft, the
previous mapping, check reports and parse errors) is wrapped in
`<data-ID untrusted="true">…</data-ID>`, with a random token ID per block. The system prompt
says that content is data and never instructions, and that a block ends only at its own
token. Any tag-like text inside it is escaped, so the data cannot close the block early. In
`claude-code` mode, field and table names in the task file are escaped onto one line inside
a fence longer than any backtick run, so a name can't add a heading or close the fence.

## Outputs

`setup` writes these files to `--mappings-dir` (default `mappings/`):

| file | contents |
|---|---|
| `<source>.json` | the mapping |
| `<source>.setup.json` | the agent mode, the rounds with the check status and problem codes for each, the rationale, token usage, and the final check counts and rates |
| `<source>.task.md` | `claude-code` only: the task for the Claude Code agent |

Once the mapping passes, `setup` prints the ingest command:

```bash
uv run --directory swarm_mcp swarm-mcp ingest mapped --mapping mappings/<source>.json <path>
```

This command works only once the `mapped` adapter is registered in the ingest CLI (see
[Integration](#integration)). After ingest, restart the MCP server (`.mcp.json`, server
`swarm`; data and store paths go in its `env` block) and smoke-test it:
`core_event_sources` should list the source, `scope_search` should find a known phrase,
and `core_get_event` should resolve one id of each kind.

## Security

- **Mappings are pure data.** Paths are looked up in rows, filters use a fixed set of
  operators, and time patterns go to `strptime`. No spec value is ever evaluated,
  imported or used as a regex.
- **Dataset contents are untrusted.** Field names, values, docs and check output can
  contain text that looks like instructions. The LLM prompt and the `/swarm-setup`
  command both say to treat it as data.
- **Example values are masked and truncated** in profiles and check reports.
  Datasets themselves are never committed.

## Integration

- `src/swarm_mcp/setup/protocol_bridge.py` is the only glue to the record format. It
  defines a minimal `Adapter` protocol (`source`, `description`, `kinds`, `agents()`,
  `records()` and `periods()`) built on `swarm_mcp.events` (`make_event_id`,
  `parse_event_id`, `event_record`), and the `AgentRecord`, `StandardRecord` and
  `PeriodRecord` types. When the unified data format lands, adopt or replace it there:
  convert these records into its rows, and register `MappedAdapter` as `mapped` in its
  adapter registry. Wiring `inspect`, `setup` and `check` into the `swarm-mcp` CLI
  is a thin wrapper around `setup/cli.py`.
- `src/swarm_mcp/setup/masking.py` is a stand-in for the redact engine. Once that engine
  lands, `mask()` should delegate to it.
