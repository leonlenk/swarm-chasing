"""Library wiring between the new features and the SwarmScope store: mapped ingest, the
store record provider (sweeps, exports) and the shared masking engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import A_OPUS, call, call_error, config_for
from setup_datasets import AGENTS_A, make_nested_jsonl, make_sqlite_board

from swarm_mcp import export as exp
from swarm_mcp import llm
from swarm_mcp.llm import FakeClient
from swarm_mcp.scope import db, evidence
from swarm_mcp.scope.adapters.mapped import MappedStoreAdapter
from swarm_mcp.scope.ingest import ingest_mapped
from swarm_mcp.scope.records import StoreRecordProvider, export_store, store_records
from swarm_mcp.server import build_server
from swarm_mcp.setup.agent import draft_mapping
from swarm_mcp.setup.masking import mask, show
from swarm_mcp.setup.profile import profile_path
from swarm_mcp.toolkit import Scrubber

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


def test_store_records_filters(store_path: Path):
    allrec = list(store_records(store_path))
    assert allrec and [r["time"] for r in allrec] == sorted(r["time"] for r in allrec)
    assert {r["kind"] for r in allrec} == {"chat", "event"}
    gen = list(store_records(store_path, {"source": "village", "channel": "general", "until": "2026-01-06"}))
    assert gen and all(r["kind"] == "chat" and r["location"] == "general" for r in gen)
    assert all(r["time"] < "2026-01-07" for r in gen)
    opus = list(store_records(store_path, {"author": "Opus 4.5", "kind": "event"}))
    assert opus and all(r["actor_id"].endswith(A_OPUS) and r["kind"] == "event" for r in opus)
    humans = list(store_records(store_path, {"author": "human"}))
    assert humans and all(r["actor_type"] == "human" for r in humans)
    q = list(store_records(store_path, {"query": "givedirectly"}))
    assert q and all("givedirectly" in r["text"].lower() for r in q)
    assert len(list(store_records(store_path, {}, limit=3))) == 3
    with pytest.raises(ValueError, match="Unknown filter"):
        list(store_records(store_path, {"actor": "x"}))
    with pytest.raises(ValueError, match="Unknown source"):
        list(store_records(store_path, {"source": "nope"}))
    # the provider caps and masks text
    p = StoreRecordProvider(store_path, max_chars=60, mask=Scrubber(True, ["agentvillage.org"]))
    recs = p.iter_records({"query": "bob.smith"}, limit=10)
    assert recs and all("bob.smith" not in r["text"] and "[email]" in r["text"] for r in recs)


def test_sweep_filters_use_the_store_provider(data_dir: Path, tmp_path: Path, monkeypatch):
    app = build_server(config_for(data_dir, sweeps=tmp_path / "sweeps"))
    flt = {"source": "village", "channel": "general", "since": "2026-01-05", "until": "2026-01-07"}
    est = call(app, "sweep_run", rubric="Does the agent agree?", filters=flt, cap=50)
    est = est["estimate"]
    assert est["records"] == 5 and est["est_input_tokens"] > 0  # m1-m5; a bare until includes that day
    dry = call(app, "sweep_run", rubric="q", filters=flt)
    assert "records from provider 'store'" in dry["notes"]
    monkeypatch.setattr(llm, "get_client", lambda config=None: FakeClient(lambda s, p: '{"verdict": "no"}'))
    out = call(app, "sweep_run", rubric="q", filters=flt, dry_run=False)
    assert out["sent"] == 5 and all(v["event_id"].startswith("village:chat:") for v in out["verdicts"])
    assert "Unknown filter" in call_error(app, "sweep_run", rubric="q", filters={"actor": "x"})


def test_export_store_redacts_and_checks(store_path: Path, tmp_path: Path):
    out = tmp_path / "export"
    res = export_store(store_path, out, {"source": "village"}, allow_email_domains=["agentvillage.org"])
    assert res["ok"] is True and res["check"]["findings"] == []
    assert res["records"]["by_source_kind"]["village"]["chat"] > 0
    assert res["redaction"]["counts"]["email"] >= 2 and res["redaction"]["counts"]["phone"] >= 2
    text = (out / "events.jsonl").read_text()
    assert "bob.smith@gmail.com" not in text and "help@agentvillage.org" in text
    assert "lorem ipsum" in text and "THE END" in text  # full text, not the 500-char tool cap
    assert exp.check(out).ok
    res2 = export_store(store_path, out, {"author": "human"}, with_agents=True, check=False)
    assert "check" not in res2 and res2["records"]["agents"] == 4
    assert json.loads((out / "manifest.json").read_text())["filters"] == {"from": "swarmscope store", "author": "human"}


def test_masking_uses_the_redact_engine():
    s = Scrubber(True, ["agentvillage.org"])
    assert s("key AKIAIOSFODNN7EXAMPLE and Bearer abcdefghijklmnopqrstuvwxyz012345") == (
        "key [credential] and Bearer [credential]"
    )
    # behaviour change from the regex-only scrubber: VCS remotes are no longer masked as emails
    assert s("clone git@github.com:org/repo.git") == "clone git@github.com:org/repo.git"
    assert s("x@y.com +1 415 555 0134") == "[email] [phone]"
    assert Scrubber(False)("x@y.com") == "x@y.com"
    assert mask("mail help@agentvillage.org") == "mail [email]"  # setup masking: no allowlist
    assert show({"token": "ghp_" + "a1B2" * 9}) == '{"token": "[credential]"}'


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
