"""Load a dataset into the SwarmScope store via an adapter.

Idempotent per source: all rows of the adapter's source are replaced inside one
transaction; findings and other sources are untouched. A source that was loaded by a
different adapter, or by a different mapping, is only replaced with ``replace=True``
(``SourceConflict`` otherwise), so a mapping named ``village`` can't wipe AI Village. Rows are validated
against the pydantic models, spooled to temporary NDJSON files next to the
store (deleted afterwards) and bulk-loaded with DuckDB's ``read_json``, which
is far faster than row-by-row inserts.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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


class SourceConflict(ToolInputError):
    """The source already holds data from another adapter, or from another mapping file."""

    def __init__(self, source: str, db_path: Path, existing: tuple[str, str | None], new: tuple[str, str | None]):
        self.source, self.existing, self.new = source, existing, new

        def owner(o: tuple[str, str | None]) -> str:
            return f"adapter {o[0]!r}" + (f" (mapping {o[1]})" if o[1] else "")

        super().__init__(
            f"Source {source!r} in {db_path} was loaded by {owner(existing)}; this ingest uses {owner(new)}. "
            "Ingesting would delete all of its rows, so nothing was ingested. Pass replace=True to replace it."
        )

    @property
    def same_adapter(self) -> bool:
        """Only the mapping file differs (re-adding a mapped source with a changed mapping)."""
        return self.existing[0] == self.new[0]


def _owner(adapter: Adapter) -> tuple[str, str | None]:
    """(adapter name, mapping path or None): what a source row's data came from."""
    mapping = (getattr(adapter, "source_meta", None) or {}).get("mapping")
    return adapter.name, str(Path(mapping).resolve()) if mapping else None


def check_replace(adapter: Adapter, db_path: Path) -> None:
    """Raise ``SourceConflict`` if ``adapter.source`` is in the store from another adapter or mapping."""
    if not Path(db_path).exists():
        return
    with db.connect(db_path, read_only=True) as store:
        if not store.has_table("sources"):  # a new or foreign store: ingest() reports what is wrong with it
            return
        row = store.con.execute("SELECT adapter, meta FROM sources WHERE source = ?", [adapter.source]).fetchone()
    if row is None:
        return
    try:
        meta = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
    except ValueError:
        meta = {}
    mapping = meta.get("mapping") if isinstance(meta, dict) else None
    existing = (str(row[0]), str(Path(mapping).resolve()) if mapping else None)
    if existing != _owner(adapter):
        raise SourceConflict(adapter.source, db_path, existing, _owner(adapter))


def ingest(
    adapter_name: str | Adapter,
    path: Path,
    db_path: Path,
    *,
    include_events: bool = True,
    source: str | None = None,
    progress: Callable[[str], None] | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Ingest ``path`` with adapter ``adapter_name`` (a name or an adapter instance) into ``db_path``.
    Returns counts and timing. Refuses (``SourceConflict``) to replace a source loaded by another
    adapter or mapping unless ``replace``."""
    say = progress or (lambda msg: log.info(msg))
    adapter = get_adapter(adapter_name, source) if isinstance(adapter_name, str) else adapter_name
    path = Path(path)
    db_path = Path(db_path)
    if not replace:
        check_replace(adapter, db_path)
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

        con = db.open_connection(db_path, read_only=False, timeout=30)
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
            for table in schema.RECORD_MODELS:
                con.execute(f"DELETE FROM {table} WHERE source = ?", [adapter.source])
            for table in schema.RECORD_MODELS:
                if not counts[table]:
                    continue
                cols = ", ".join(schema.COLUMNS[table])
                file = _sql_str(str(spool / f"{table}.ndjson"))
                con.execute(
                    f"INSERT INTO {table} SELECT {cols} FROM read_json({file}, format='newline_delimited', "
                    f"columns={_columns_struct(table)}, maximum_object_size=67108864) ORDER BY {_ORDER[table]}"
                )
            con.execute("DELETE FROM sources WHERE source = ?", [adapter.source])
            con.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?)",
                [
                    adapter.source,
                    adapter.name,
                    str(path),
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

    return {
        "adapter": adapter.name,
        "source": adapter.source,
        "path": str(path),
        "db": str(db_path),
        "counts": stored,
        "seconds": round(time.perf_counter() - t0, 1),
    }


def ingest_mapped(
    mapping: str | Path,
    path: str | Path | None,
    db_path: Path,
    *,
    progress: Callable[[str], None] | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Ingest the dataset at ``path`` (default: the mapping's ``root``) through the declarative
    ``mapping`` JSON into ``db_path``. Idempotent: the mapping's source is replaced as a whole.
    A source already loaded by another adapter or mapping needs ``replace=True``."""
    from swarm_mcp.scope.adapters.mapped import MappedStoreAdapter

    adapter = MappedStoreAdapter.from_file(mapping, path)
    result = ingest(adapter, adapter.mapped.root, db_path, progress=progress, replace=replace)
    result["mapping"] = str(mapping)
    stats = {k: v for k, v in adapter.mapped.stats.items() if v}
    if stats:
        result["mapping_stats"] = stats
    return result
