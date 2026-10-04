"""Mapped datasets in the SwarmScope store: ingest_mapped, the converter and id resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import call, config_for
from setup_datasets import AGENTS_A, make_nested_jsonl, make_sqlite_board

from swarm_mcp.scope import db, evidence
from swarm_mcp.scope.adapters.mapped import MappedStoreAdapter
from swarm_mcp.scope.ingest import ingest_mapped
from swarm_mcp.server import build_server
from swarm_mcp.setup.agent import draft_mapping
from swarm_mcp.setup.profile import profile_path

BOARD_SPEC = {
    "source": "board",
    "agents": {"from": "board.sqlite#members", "id": "member_key", "display_name": "screen_name"},
    "records": [
        {
            "from": "board.sqlite#posts",
            "kind": "post",
            "local_id": "post_key",
            "time": {"field": "posted_unix", "format": "epoch_s"},
            "actor": {"field": "author_ref", "match": "id", "unmatched_prefix": "human:"},
            "text": "body_md",
            "reply_to": {"field": "reply_to_post"},
            "meta": {"thread": "thread_ref"},
        },
        {
            "from": "board.sqlite#threads",
            "kind": "open",
            "category": "action",
            "local_id": "thread_key",
            "time": {"field": "opened_ts", "format": "epoch_s"},
            "actor": {"field": "opened_by", "match": "id"},
            "text": "title",
        },
    ],
    "periods": [
        {
            "from": "board.sqlite#threads",
            "kind": "thread",
            "local_id": "thread_key",
            "label": "title",
            "start": {"field": "opened_ts", "format": "epoch_s"},
        }
    ],
}


def mapped_store(tmp_path: Path, data_dir: Path):
    """The synthetic village store plus a mapped sqlite board ingested into the same file."""
    root = make_sqlite_board(tmp_path / "board")
    mapping = tmp_path / "board.json"
    mapping.write_text(json.dumps(BOARD_SPEC))
    store = data_dir / "swarmscope.duckdb"
    res = ingest_mapped(mapping, root, store)
    return {"data_dir": data_dir, "store": store, "result": res, "mapping": mapping, "root": root}


def test_ingest_mapped_converts_records_agents_periods(mapped_store):
    res, store = mapped_store["result"], mapped_store["store"]
    assert res["source"] == "board" and res["adapter"] == "mapped"
    assert res["counts"]["messages"] == 240  # posts -> messages
    assert res["counts"]["actions"] == 12  # thread openings (category action) -> actions, kind kept
    assert res["counts"]["periods"] == 12 and res["counts"]["agents"] >= 3
    with db.connect(store) as s:
        assert s.scalar("SELECT count(*) FROM messages WHERE source = 'village'") > 0  # other sources untouched
        assert {r["kind"] for r in s.all("SELECT DISTINCT kind FROM actions WHERE source = 'board'")} == {"open"}
        post = s.one("SELECT * FROM messages WHERE source = 'board' AND reply_to IS NOT NULL LIMIT 1")
        assert post["evidence_id"].startswith("board:post:") and post["reply_to"].startswith("board:post:")
        assert post["ts"] is not None and post["channel"] is None
        assert json.loads(post["meta"])["kind"] == "post"
        for eid in (post["evidence_id"], "board:open:1", "board:thread:1"):
            assert evidence.resolve(s, eid)["evidence_id"] == eid
        with pytest.raises(evidence.EvidenceError, match="Unknown evidence kind"):
            evidence.resolve(s, "board:tweet:1")
        with pytest.raises(evidence.EvidenceError, match="does not resolve"):
            evidence.resolve(s, "board:post:nope")
    # idempotent: a second ingest replaces the source, counts unchanged
    again = ingest_mapped(mapped_store["mapping"], mapped_store["root"], store)
    assert again["counts"] == res["counts"]


def test_mapped_ids_resolve_through_the_store_event_source(mapped_store):
    app = build_server(config_for(mapped_store["data_dir"]))
    hits = call(app, "scope_search", query="the", source="board", limit=3)
    assert hits["total"] > 0
    eid = hits["results"][0]["evidence_id"]
    got = call(app, "core_get", ids=eid, after=1)
    assert got["event"]["event_id"] == eid and got["event"]["kind"] == "post"
    thread = call(app, "core_get", ids="board:thread:1")["event"]
    assert thread["event_id"] == "board:thread:1" and thread["period_kind"] == "thread"
    assert call(app, "core_get", ids="board:open:1")["event"]["action_kind"] == "open"
    periods = call(app, "scope_periods", source="board")
    assert periods["count"] == 12 and periods["periods"][0]["kind"] == "thread"
    kinds = {k["kind"] for src in call(app, "core_info")["sources"] if src["source"] == "board"
             for k in src["kinds"]}  # fmt: skip
    assert {"post", "open", "thread", "agent"} <= kinds


def test_ingest_mapped_from_a_heuristic_draft(tmp_path: Path):
    root = make_nested_jsonl(tmp_path / "crew")
    spec = draft_mapping(profile_path(root), "crew")
    mapping = tmp_path / "crew.json"
    mapping.write_text(json.dumps(spec))
    res = ingest_mapped(mapping, root, tmp_path / "s.duckdb")
    assert res["counts"]["messages"] == 300 and res["counts"]["agents"] == len(AGENTS_A)
    with db.connect(tmp_path / "s.duckdb") as s:
        nova = s.one("SELECT * FROM agents WHERE agent_id = 'crew:agent:p-nova'")
        assert nova["display_name"] == "Nova" and nova["first_seen"] is not None


def test_ingest_refuses_a_store_with_another_schema(tmp_path: Path):
    import duckdb

    from swarm_mcp.scope.ingest import ingest

    store = tmp_path / "other.duckdb"
    con = duckdb.connect(str(store))
    con.execute("CREATE TABLE events (evidence_id TEXT, source TEXT)")
    con.execute("CREATE VIEW messages AS SELECT * FROM events")
    con.close()
    root = make_nested_jsonl(tmp_path / "crew")
    with pytest.raises(ValueError, match="different schema"):
        ingest(MappedStoreAdapter.from_file(_crew_mapping(tmp_path, root), root), root, store)


def _crew_mapping(tmp_path: Path, root: Path) -> Path:
    mapping = tmp_path / "crew.json"
    mapping.write_text(json.dumps(draft_mapping(profile_path(root), "crew")))
    return mapping
