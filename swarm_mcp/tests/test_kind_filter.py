"""The "source:kind" record filter keeps its source everywhere: store records, exports and sweeps.

Regression: "rpg:event" was cut to "event", so an export or sweep scoped to one source also took every
other source's events (gated AI Village records leaked into an export scoped to another source).
All data is synthetic (conftest village store plus a mapped sqlite board).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import call, call_error, config_for
from test_mapped_ingest import mapped_store  # noqa: F401 - pytest fixture

from swarm_mcp.export import select
from swarm_mcp.scope.records import export_store, store_records
from swarm_mcp.server import build_server
from swarm_mcp.toolkit import ToolInputError


def _sources(records) -> set[tuple[str, str]]:
    return {tuple(r["event_id"].split(":")[:2]) for r in records}


def test_store_records_qualified_kind(mapped_store):  # noqa: F811
    store = mapped_store["store"]
    assert _sources(store_records(store, {"kind": "board:event"})) == {("board", "event")}
    assert _sources(store_records(store, {"kind": "event"})) == {("board", "event"), ("village", "event")}
    mixed = store_records(store, {"kind": ["board:event", "village:msg"]})
    assert _sources(mixed) == {("board", "event"), ("village", "msg")}
    both = store_records(store, {"kind": ["board:event", "event"]})  # a bare kind widens it again
    assert _sources(both) == {("board", "event"), ("village", "event")}
    # an explicit source filter intersects with the kind's source
    assert list(store_records(store, {"kind": "board:event", "source": "village"})) == []
    with pytest.raises(ToolInputError, match="Unknown source"):
        list(store_records(store, {"kind": "nope:event"}))


def test_export_scoped_by_qualified_kind(mapped_store, tmp_path: Path):  # noqa: F811
    res = export_store(mapped_store["store"], tmp_path / "x", {"kind": "board:event"}, check=False)
    by = res["records"]["by_source_kind"]
    assert set(by) == {"board"} and set(by["board"]) == {"event"}
    lines = (tmp_path / "x" / "events.jsonl").read_text().splitlines()
    assert lines and all(json.loads(ln)["event_id"].startswith("board:event:") for ln in lines)


def test_export_select_agrees_with_store_records(mapped_store):  # noqa: F811
    recs = list(store_records(mapped_store["store"]))
    for kinds in (["board:event"], ["event"], ["board:event", "village:msg"], ["msg"]):
        want = [r["event_id"] for r in store_records(mapped_store["store"], {"kind": kinds})]
        assert sorted(r["event_id"] for r in select(recs, kinds=kinds)) == sorted(want), kinds


def test_sweep_filters_scoped_by_qualified_kind(mapped_store, tmp_path: Path):  # noqa: F811
    app = build_server(config_for(mapped_store["data_dir"], sweeps=tmp_path / "sweeps"))
    dry = call(app, "sweep_run", rubric="q", filters={"kind": "board:event"}, cap=500)
    assert dry["estimate"]["records"] == 12  # the board's thread openings only, not village events
    assert all(e.startswith("board:event:") for e in dry["event_ids"])
    assert "Unknown source" in call_error(app, "sweep_run", rubric="q", filters={"kind": "nope:event"})
