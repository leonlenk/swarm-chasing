"""Load a dataset into the SwarmScope store via an adapter.

Idempotent per source: all rows of the adapter's source are replaced inside one
transaction; findings and other sources are untouched. Rows are validated
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
from swarm_mcp.scope.adapters import get_adapter

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


def ingest(
    adapter_name: str,
    path: Path,
    db_path: Path,
    *,
    include_events: bool = True,
    source: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Ingest ``path`` with adapter ``adapter_name`` into ``db_path``. Returns counts and timing."""
    say = progress or (lambda msg: log.info(msg))
    adapter = get_adapter(adapter_name, source)
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

        con = db.open_connection(db_path, read_only=False, timeout=30)
        try:
            con.execute("BEGIN TRANSACTION")
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
                        }
                    ),
                ],
            )
            con.execute("COMMIT")
            stored = {
                t: con.execute(f"SELECT count(*) FROM {t} WHERE source = ?", [adapter.source]).fetchone()[0]
                for t in schema.RECORD_MODELS
            }
        except Exception:
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
