"""Re-adding a source must not grow the store: DuckDB keeps deleted rows of indexed tables in the
file, so ``ingest`` compacts the store afterwards. Also: a busy store fails cleanly. Synthetic data only."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import duckdb
import pytest

from swarm_mcp.scope import db
from swarm_mcp.scope import ingest as ing
from swarm_mcp.toolkit import ToolInputError

N_MSGS = 3000


class _Synth:
    """A minimal adapter: N synthetic messages from three agents."""

    name = "synth"

    def __init__(self, source: str, n: int = N_MSGS):
        self.source, self.n = source, n

    def load(self, path: Path, include_events: bool = True) -> Iterator[tuple[str, dict[str, Any]]]:
        t0 = datetime(2031, 1, 1)
        for a in range(3):
            yield "agents", {"agent_id": f"{self.source}:agent:a{a}", "source": self.source, "display_name": f"A{a}"}
        for i in range(self.n):
            yield "messages", {
                "evidence_id": f"{self.source}:msg:{i}",
                "source": self.source,
                "author_id": f"{self.source}:agent:a{i % 3}",
                "ts": t0 + timedelta(minutes=i),
                "content": f"synthetic message {i} " + "lorem ipsum dolor " * 20,
            }


def _snapshot(store: Path) -> dict[str, Any]:
    """Per-table, per-source row count and order-independent content hash (ingested_at left out)."""
    con = duckdb.connect(str(store), read_only=True)
    try:
        out = {}
        for t in ("agents", "messages", "findings"):
            by = "'-'" if t == "findings" else "source"
            for src, n, h in con.execute(f"SELECT {by}, count(*), bit_xor(hash(t)) FROM {t} t GROUP BY 1").fetchall():
                out[f"{t}/{src}"] = (n, h)
        for row in con.execute("SELECT source, adapter, path, counts, meta FROM sources").fetchall():
            out[f"sources/{row[0]}"] = row[1:]
        out["dead"] = sum(
            size - con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name, size in con.execute("SELECT table_name, estimated_size FROM duckdb_tables()").fetchall()
        )
        return out
    finally:
        con.close()


def _hold(store: Path, read_only: bool) -> subprocess.Popen:
    """Another process holding the store open (like a long query) until killed."""
    code = (
        "import duckdb, sys, time\n"
        f"c = duckdb.connect({str(store)!r}, read_only={read_only})\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout is not None and p.stdout.readline().strip() == "held"
    return p


def test_readding_a_source_keeps_the_store_size_flat(tmp_path: Path):
    """Regression: DELETE + INSERT of a source left the old rows in the file (DuckDB does not vacuum
    deletes from tables with a primary key, and CHECKPOINT/VACUUM don't either), so every re-add grew
    the store by a full copy of the source. Other sources and findings must survive the compaction."""
    store = tmp_path / "s.duckdb"
    ing.ingest(_Synth("other", 200), tmp_path, store)
    with db.connect(store, read_only=False) as s:
        s.con.execute(
            "INSERT INTO findings VALUES ('f1', TIMESTAMP '2031-01-02', 'a synthetic claim', ['other:msg:1'], "
            "'high', 'tester', 'open')"
        )
    first = ing.ingest(_Synth("synth"), tmp_path, store)
    assert first["replaced"] == "new"
    size, before = store.stat().st_size, _snapshot(store)
    sizes = []
    for _ in range(4):
        res = ing.ingest(_Synth("synth"), tmp_path, store)
        assert res["replaced"] == "same"
        sizes.append(store.stat().st_size)
    # the old code: each re-add kept the previous copy (here 3.9 -> 5.3 MB over 4 re-adds; 1.5 GB for AI Village)
    assert sizes[-1] <= sizes[0] and sizes[-1] <= size, (size, sizes)
    after = _snapshot(store)
    assert after == before and after["dead"] == 0
    assert after["messages/other"][0] == 200 and after["findings/-"][0] == 1
    assert res["compaction"]["compacted"] is True
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.duckdb"]  # no temp files or folders left


def test_a_small_readd_into_a_big_store_does_not_compact(tmp_path: Path):
    """Compaction waits until deleted rows are a sizable share of the store (it rewrites the whole file)."""
    store = tmp_path / "s.duckdb"
    ing.ingest(_Synth("big"), tmp_path, store)
    ing.ingest(_Synth("tiny", 20), tmp_path, store)
    res = ing.ingest(_Synth("tiny", 20), tmp_path, store)
    assert res["compaction"]["compacted"] is False and "skipped" not in res["compaction"]
    assert 0 < res["compaction"]["dead_rows"] < 100


def test_add_while_another_process_holds_the_store_fails_cleanly(tmp_path: Path, monkeypatch):
    store = tmp_path / "s.duckdb"
    ing.ingest(_Synth("synth", 300), tmp_path, store)
    snap = _snapshot(store)
    real = db.open_connection
    monkeypatch.setattr(db, "open_connection", lambda p, read_only=True, timeout=10: real(p, read_only=read_only, timeout=0.3))
    holder = _hold(store, read_only=True)
    try:
        with pytest.raises(ToolInputError, match=r"in use by another process \(PID \d+\), so nothing was ingested"):
            ing.ingest(_Synth("synth", 300), tmp_path, store)
    finally:
        holder.kill()
        holder.wait()
    assert _snapshot(store) == snap


def test_compaction_skips_a_store_held_by_a_writer(tmp_path: Path, monkeypatch):
    """No swap while another process has the store open read-write: its writes would go to the old file."""
    store = tmp_path / "s.duckdb"
    compact = ing.compact
    monkeypatch.setattr(ing, "compact", lambda *a, **k: {})
    for _ in range(2):  # a re-add without compaction: the old rows stay in the file
        ing.ingest(_Synth("synth", 300), tmp_path, store)
    holder = _hold(store, read_only=False)
    try:
        res = compact(store, timeout=0.3)
    finally:
        holder.kill()
        holder.wait()
    assert res["compacted"] is False and "in use by another process" in res["skipped"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.duckdb"]
    assert compact(store)["compacted"] is True  # free again: compacted
    assert _snapshot(store)["dead"] == 0
