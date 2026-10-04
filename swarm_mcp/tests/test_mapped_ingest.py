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


@pytest.fixture
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
        assert {r["kind"] for r in s.all("SELECT DISTINCT kind FROM periods WHERE source = 'board'")} == {"thread"}
        post = s.one("SELECT * FROM messages WHERE source = 'board' AND reply_to IS NOT NULL LIMIT 1")
        # schema kinds in the id, the dataset kind prefixes the local id and lands in msg_type
        assert post["evidence_id"].startswith("board:msg:post/") and post["reply_to"].startswith("board:msg:post/")
        assert post["msg_type"] == "post" and post["ts"] is not None and post["channel"] is None
        meta = json.loads(post["meta"])
        assert post["evidence_id"] == f"board:msg:post/{meta['native_id']}" and meta["thread"] >= 1
        for eid in (post["evidence_id"], "board:event:open/1", "board:period:thread/1", "board:agent:m-ada"):
            assert evidence.resolve(s, eid)["evidence_id"] == eid
        with pytest.raises(evidence.EvidenceError, match="Unknown evidence kind"):
            evidence.resolve(s, "board:post:1001")  # dataset kinds are not id kinds
        with pytest.raises(evidence.EvidenceError, match="does not resolve"):
            evidence.resolve(s, "board:msg:post/nope")
        src_meta = json.loads(s.one("SELECT meta FROM sources WHERE source = 'board'")["meta"])
        assert src_meta["categories"] == {"post": "message", "open": "action", "thread": "period"}
        assert src_meta["mapping"] == str(mapped_store["mapping"]) and "kinds" not in src_meta
    # idempotent: a second ingest replaces the source, counts unchanged
    again = ingest_mapped(mapped_store["mapping"], mapped_store["root"], store)
    assert again["counts"] == res["counts"]


def test_mapped_ids_resolve_through_the_store_event_source(mapped_store):
    app = build_server(config_for(mapped_store["data_dir"]))
    hits = call(app, "scope_search", query="the", source="board", limit=3)
    assert hits["total"] > 0
    eid = hits["results"][0]["evidence_id"]
    assert eid.startswith("board:msg:post/")
    got = call(app, "core_get", ids=eid, after=1)
    assert got["evidence_id"] == eid and got["table"] == "messages" and got["source"] == "board"
    assert got["msg_type"] == "post" and got["author_id"].startswith(("board:agent:", "human:"))
    assert got["content"]["untrusted"] is True and got["content"]["content"]
    assert set(got["neighbors"]) >= {"before", "after"}
    thread = call(app, "core_get", ids="board:period:thread/1")
    assert thread["table"] == "periods" and thread["kind"] == "thread" and thread["start"]
    opened = call(app, "core_get", ids="board:event:open/1")
    assert opened["table"] == "actions" and opened["kind"] == "open" and opened["agent_id"].startswith("board:agent:")
    batch = call(app, "core_get", ids=[eid, "board:event:open/1", "board:msg:post/nope"])
    assert (batch["requested"], batch["returned"]) == (3, 2) and batch["errors"][0]["id"] == "board:msg:post/nope"
    periods = call(app, "scope_periods", source="board")
    assert periods["count"] == 12 and periods["periods"][0]["kind"] == "thread"
    assert periods["periods"][0]["evidence_id"].startswith("board:period:thread/")
    board = next(src for src in call(app, "core_info")["sources"] if src["source"] == "board")
    assert board["adapter"] == "mapped" and board["row_counts"]["messages"] == 240
    assert board["ingest_meta"]["categories"]["open"] == "action"


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
