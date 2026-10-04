"""Load a dataset into the SwarmScope store via an adapter.

Idempotent per source: all rows of the adapter's source are replaced inside one
transaction; findings and other sources are untouched. A source that was loaded by a
different adapter, or by a different mapping, is only replaced with ``replace=True``
(``SourceConflict`` otherwise), so a mapping named ``village`` can't wipe AI Village. Rows are validated
against the pydantic models, spooled to temporary NDJSON files next to the
store (deleted afterwards) and bulk-loaded with DuckDB's ``read_json``, which
is far faster than row-by-row inserts. DuckDB keeps the deleted rows of a replaced source
in the file, so after an ingest ``compact`` rewrites the store once they fill much of it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import duckdb

from swarm_mcp.scope import db, schema
from swarm_mcp.scope.adapters import Adapter, get_adapter
from swarm_mcp.toolkit import ToolInputError

log = logging.getLogger("swarm_mcp.scope.ingest")

_ORDER = {
    "messages": "ts, evidence_id",
    "actions": "ts, evidence_id",
    "periods": "start_ts",
    "agents": "agent_id",
    "artifacts": "artifact_id",
    "touches": "ts, touch_id",
}


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _columns_struct(table: str) -> str:
    cols = schema.COLUMNS[table]
    return "{" + ", ".join(f"{_sql_str(c)}: {_sql_str(t.replace('TEXT', 'VARCHAR'))}" for c, t in cols.items()) + "}"


Owner = tuple[str, str, "str | None"]  # (adapter name, resolved dataset path, resolved mapping path or None)


def _describe(o: Owner) -> str:
    return f"{o[0]} data from {o[1]}" + (f" (mapping {o[2]})" if o[2] else "")


class SourceConflict(ToolInputError):
    """The source already holds a different dataset: another adapter or path, or another mapping file."""

    def __init__(self, source: str, existing: Owner, new: Owner):
        self.source, self.existing, self.new = source, existing, new
        super().__init__(
            f"source {source!r} already holds {_describe(existing)}; this is {_describe(new)}. Nothing was "
            "ingested: ingesting would delete all of the source's rows. Pick another source name, or pass "
            "replace=True to replace it."
        )

    @property
    def mapping_only(self) -> bool:
        """Same adapter and dataset, only the mapping file differs (a re-add with a changed mapping)."""
        return self.existing[:2] == self.new[:2]


def _resolved(p: Any) -> str:
    return str(Path(str(p)).expanduser().resolve())


def _owner(adapter: Adapter, path: Path) -> Owner:
    mapping = (getattr(adapter, "source_meta", None) or {}).get("mapping")
    return adapter.name, _resolved(path), _resolved(mapping) if mapping else None


def _owner_of(row: Any) -> Owner | None:
    """The owner recorded in a ``sources`` row (adapter, path, meta), or None."""
    if row is None:
        return None
    try:
        meta = json.loads(row[2]) if isinstance(row[2], str) else (row[2] or {})
    except ValueError:
        meta = {}
    mapping = meta.get("mapping") if isinstance(meta, dict) else None
    return str(row[0]), _resolved(row[1]), _resolved(mapping) if mapping else None


def _in_use(e: Exception) -> str:
    """'in use by another process (PID n)' for a DuckDB lock error (its own text suggests read-only mode)."""
    m = re.search(r"\(PID (\d+)\)", str(e))
    return "in use by another process" + (f" (PID {m.group(1)})" if m else "")


def _read_spool(table: str, spool: Path) -> str:
    file = _sql_str(str(spool / f"{table}.ndjson"))
    return (
        f"read_json({file}, format='newline_delimited', columns={_columns_struct(table)}, "
        "maximum_object_size=67108864)"
    )


def _duplicate_error(
    con: duckdb.DuckDBPyConnection, table: str, spool: Path, adapter: Adapter, err: Exception
) -> ToolInputError:
    """A readable error for a primary-key collision at INSERT (e.g. two mapped records with one local_id).

    Called after ROLLBACK; names up to five duplicate ids from the spooled rows and how many there are
    (or, when the clash is with another source's rows, the key DuckDB reported)."""
    pk = schema.PRIMARY_KEYS[table]
    dupes: list[tuple[Any, int, int]] = []
    try:
        dupes = con.execute(
            f"SELECT {pk}, n, count(*) OVER () FROM (SELECT {pk}, count(*) AS n FROM {_read_spool(table, spool)} "
            "GROUP BY 1 HAVING count(*) > 1) ORDER BY n DESC, 1 LIMIT 5"
        ).fetchall()
    except duckdb.Error:
        pass
    if dupes:
        ids = ", ".join(f"{k!r} ({n}x)" for k, n, _ in dupes)
        more = dupes[0][2] - len(dupes)
        ids += f" and {more:,} more" if more else ""
    else:
        m = re.search(r'duplicate key "([^"]*)"', str(err))
        ids = repr(m.group(1)) if m else "(unknown)"
    return ToolInputError(
        f"source {adapter.source!r}: duplicate {pk} in {table}: {ids}. Nothing was ingested (the store is "
        "unchanged). Each record's id must be unique; for a mapping, make each entry's local_id unique "
        "within its kind (e.g. a primary key, not a foreign key). The mapping check (--dry-run) reads only "
        "a sample, so it can miss duplicates further into the data."
    )


def ingest(
    adapter_name: str | Adapter,
    path: Path,
    db_path: Path,
    *,
    include_events: bool = True,
    source: str | None = None,
    progress: Callable[[str], None] | None = None,
    replace: bool = False,
    allow_mapping_change: bool = False,
) -> dict[str, Any]:
    """Ingest ``path`` with adapter ``adapter_name`` (a name or an adapter instance) into ``db_path``.

    Returns counts, timing and ``replaced``: "new" (the source was not in the store), "same" (the same
    adapter, dataset path and mapping again), "mapping" (the same dataset with another mapping file,
    allowed by ``allow_mapping_change``) or "replaced" (``replace=True`` over a different dataset,
    described in ``previous``). Otherwise a source that holds a different dataset (another adapter,
    dataset path or mapping) raises ``SourceConflict``; the check reads the ``sources`` row inside the
    write transaction, before any DELETE, so nothing is changed."""
    say = progress or (lambda msg: log.info(msg))
    adapter = get_adapter(adapter_name, source) if isinstance(adapter_name, str) else adapter_name
    path = Path(path)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    spool = Path(tempfile.mkdtemp(prefix=".swarmscope-ingest-", dir=db_path.parent))
    try:
        handles = {t: open(spool / f"{t}.ndjson", "w", encoding="utf-8") for t in schema.RECORD_MODELS}
        counts = dict.fromkeys(schema.RECORD_MODELS, 0)
        try:
            for table, row in adapter.load(path, include_events=include_events):
                model = schema.RECORD_MODELS[table]
                handles[table].write(model.model_validate(row).model_dump_json() + "\n")
                counts[table] += 1
                if table in ("messages", "touches") and counts[table] % 50000 == 0:
                    say(f"  read {counts[table]:,} messages ...")
        finally:
            for h in handles.values():
                h.close()
        t_read = time.perf_counter()
        say(f"read {', '.join(f'{v:,} {k}' for k, v in counts.items())} in {t_read - t0:.1f}s; loading into {db_path}")

        try:
            con = db.open_connection(db_path, read_only=False, timeout=30)
        except duckdb.Error as e:
            if not db._is_lock_error(e):
                raise
            raise ToolInputError(
                f"the store {db_path} is {_in_use(e)}, so nothing was ingested. DuckDB allows one writer or "
                "several readers at a time: close the other program (the MCP server holds the store only "
                "during a tool call) and retry."
            ) from None
        in_tx = False
        try:
            not_tables = [
                t
                for (t, kind) in con.execute(
                    "SELECT table_name, table_type FROM information_schema.tables WHERE table_name IN "
                    f"({', '.join('?' * len(schema.RECORD_MODELS))})",
                    list(schema.RECORD_MODELS),
                ).fetchall()
                if kind != "BASE TABLE"
            ]
            if not_tables:
                what = "is not a table" if len(not_tables) == 1 else "are not tables"
                raise ValueError(
                    f"The store at {db_path} uses a different schema ({', '.join(sorted(not_tables))} {what}), "
                    "so nothing was ingested. Use a separate store (--db) or move that file away."
                )
            con.execute("BEGIN TRANSACTION")
            in_tx = True
            # adapter.source is final now (git/wiki derive it from the path while loading)
            row = con.execute("SELECT adapter, path, meta FROM sources WHERE source = ?", [adapter.source]).fetchone()
            previous, new = _owner_of(row), _owner(adapter, path)
            if previous is None:
                replaced = "new"
            elif previous == new:
                replaced = "same"
            elif replace:
                replaced = "replaced"
            elif allow_mapping_change and previous[:2] == new[:2]:
                replaced = "mapping"
            else:
                raise SourceConflict(adapter.source, previous, new)
            for table in schema.RECORD_MODELS:
                con.execute(f"DELETE FROM {table} WHERE source = ?", [adapter.source])
            for table in schema.RECORD_MODELS:
                if not counts[table]:
                    continue
                cols = ", ".join(schema.COLUMNS[table])
                try:
                    con.execute(
                        f"INSERT INTO {table} SELECT {cols} FROM {_read_spool(table, spool)} ORDER BY {_ORDER[table]}"
                    )
                except duckdb.ConstraintException as e:
                    con.execute("ROLLBACK")
                    in_tx = False
                    raise _duplicate_error(con, table, spool, adapter, e) from None
            con.execute("DELETE FROM sources WHERE source = ?", [adapter.source])
            con.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?)",
                [
                    adapter.source,
                    adapter.name,
                    _resolved(path),
                    datetime.now(timezone.utc).replace(tzinfo=None),
                    json.dumps(counts),
                    json.dumps(
                        {
                            "include_events": include_events,
                            "schema_version": schema.SCHEMA_VERSION,
                            "notes": list(getattr(adapter, "notes", []) or []),
                            **(getattr(adapter, "source_meta", None) or {}),
                        }
                    ),
                ],
            )
            con.execute("COMMIT")
            in_tx = False
            stored = {
                t: con.execute(f"SELECT count(*) FROM {t} WHERE source = ?", [adapter.source]).fetchone()[0]
                for t in schema.RECORD_MODELS
            }
        except Exception:
            if in_tx:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    finally:
        shutil.rmtree(spool, ignore_errors=True)

    compaction = compact(db_path, progress=say)
    return {
        "adapter": adapter.name,
        "source": adapter.source,
        "path": str(path),
        "db": str(db_path),
        "counts": stored,
        "seconds": round(time.perf_counter() - t0, 1),
        "replaced": replaced,
        **({"previous": _describe(previous)} if replaced in ("replaced", "mapping") and previous else {}),
        "compaction": compaction,
    }


COMPACT_MIN_DEAD = 0.25  # compact once deleted rows still in the file reach this share of the live rows


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def compact(
    db_path: Path,
    *,
    min_dead: float = COMPACT_MIN_DEAD,
    timeout: float = 10.0,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Rewrite the store into a fresh file when deleted rows fill much of it. Never raises.

    DuckDB never drops the deleted rows of a table that has an index (every store table has a
    primary key), and neither CHECKPOINT nor VACUUM reclaims them, so each re-ingest of a source
    would leave its old copy in the file for good. Once the deleted rows reach ``min_dead`` of the
    live ones, the store is copied (``COPY FROM DATABASE``: every table, findings and sources
    included) into a temporary file next to it, the row counts are compared, and the copy is
    synced and moved over the store with ``os.replace``. The store stays attached read-only
    throughout: its shared lock lets readers in (they keep reading the old, identical file) and
    keeps writers out until the new file is in place. A busy store (a writer holds it for longer
    than ``timeout`` seconds), a leftover write-ahead log or any error leaves the store as it was
    and is reported in ``skipped``; a crash leaves at most a ``.swarmscope-compact-*`` folder."""
    say = progress or (lambda msg: log.info(msg))
    store = Path(db_path).resolve()  # a symlinked store: replace its target, not the link
    out: dict[str, Any] = {"compacted": False}
    tmpdir: Path | None = None
    con = duckdb.connect(":memory:")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                con.execute(f"ATTACH {_sql_str(str(store))} AS store (READ_ONLY)")
                break
            except duckdb.Error as e:
                if not db._is_lock_error(e) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        if store.with_name(store.name + ".wal").exists():  # under our lock: a crashed writer's log
            raise RuntimeError("the store has a write-ahead log that its next writer will apply first")
        tables = con.execute(
            "SELECT schema_name, table_name, estimated_size FROM duckdb_tables() WHERE database_name = 'store'"
        ).fetchall()
        live: dict[str, int] = {}
        dead = 0
        for sch, name, size in tables:
            ref = f'"{sch}"."{name}"'
            live[ref] = con.execute(f"SELECT count(*) FROM store.{ref}").fetchone()[0]
            dead += max(0, (size or 0) - live[ref])
        total = sum(live.values())
        out.update(live_rows=total, dead_rows=dead)
        if not dead or dead < min_dead * total:
            return out
        tmpdir = Path(tempfile.mkdtemp(prefix=".swarmscope-compact-", dir=store.parent))
        tmp = tmpdir / store.name
        con.execute(f"ATTACH {_sql_str(str(tmp))} AS compact")
        con.execute("COPY FROM DATABASE store TO compact")
        con.execute("DETACH compact")
        con.execute(f"ATTACH {_sql_str(str(tmp))} AS compact (READ_ONLY)")
        for ref, n in live.items():
            got = con.execute(f"SELECT count(*) FROM compact.{ref}").fetchone()[0]
            if got != n:
                raise RuntimeError(f"the copy of {ref} has {got} rows, not {n}")
        con.execute("DETACH compact")
        if any(p.name != tmp.name for p in tmpdir.iterdir()):  # e.g. a WAL the DETACH did not fold in
            raise RuntimeError("the copy left extra files")
        os.chmod(tmp, stat.S_IMODE(store.stat().st_mode))
        _fsync(tmp)
        before = store.stat().st_size
        os.replace(tmp, store)  # still holding the old file's lock: no writer can open it in between
        try:
            _fsync(store.parent)
        except OSError:
            pass
        out.update(compacted=True, before_mb=round(before / 1e6, 1), after_mb=round(store.stat().st_size / 1e6, 1))
        say(f"compacted the store: {out['before_mb']:,} MB -> {out['after_mb']:,} MB ({dead:,} deleted rows dropped)")
    except Exception as e:  # noqa: BLE001 - compaction only saves disk: never fail the ingest over it
        busy = isinstance(e, duckdb.Error) and db._is_lock_error(e)
        out["skipped"] = f"the store is {_in_use(e)}" if busy else f"{type(e).__name__}: {e}".splitlines()[0]
        say(f"store not compacted ({out['skipped']}); the next add tries again")
    finally:
        con.close()
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return out


def ingest_mapped(
    mapping: str | Path,
    path: str | Path | None,
    db_path: Path,
    *,
    progress: Callable[[str], None] | None = None,
    replace: bool = False,
    allow_mapping_change: bool = False,
) -> dict[str, Any]:
    """Ingest the dataset at ``path`` (default: the mapping's ``root``) through the declarative
    ``mapping`` JSON into ``db_path``. Idempotent: the mapping's source is replaced as a whole.
    A source already loaded by another adapter or mapping needs ``replace=True``."""
    from swarm_mcp.scope.adapters.mapped import MappedStoreAdapter

    adapter = MappedStoreAdapter.from_file(mapping, path)
    result = ingest(
        adapter, adapter.mapped.root, db_path, progress=progress, replace=replace,
        allow_mapping_change=allow_mapping_change,
    )  # fmt: skip
    result["mapping"] = str(mapping)
    stats = {k: v for k, v in adapter.mapped.stats.items() if v}
    if stats:
        result["mapping_stats"] = stats
    return result
