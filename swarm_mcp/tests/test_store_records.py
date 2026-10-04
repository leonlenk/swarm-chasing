"""The store record provider (sweeps, exports) and the shared masking engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import A_OPUS, call, call_error, config_for

from swarm_mcp import export as exp
from swarm_mcp import llm
from swarm_mcp.llm import FakeClient
from swarm_mcp.scope.records import StoreRecordProvider, export_store, from_store_record, store_records
from swarm_mcp.server import build_server
from swarm_mcp.setup.masking import mask, show
from swarm_mcp.toolkit import Scrubber


def test_store_records_filters(store_path: Path):
    allrec = list(store_records(store_path))
    assert allrec and [r["time"] for r in allrec] == sorted(r["time"] for r in allrec)
    assert {r["kind"] for r in allrec} == {"msg", "event"}
    gen = list(store_records(store_path, {"source": "village", "channel": "general", "until": "2026-01-06"}))
    assert gen and all(r["kind"] == "msg" and r["location"] == "general" for r in gen)
    assert all(r["time"] < "2026-01-07" for r in gen)
    opus = list(store_records(store_path, {"author": "Opus 4.5", "kind": "event"}))
    assert opus and all(r["actor_id"].endswith(A_OPUS) and r["kind"] == "event" for r in opus)
    humans = list(store_records(store_path, {"author": "human"}))
    assert humans and all(r["actor_type"] == "human" for r in humans)
    q = list(store_records(store_path, {"query": "givedirectly"}))
    assert q and all("givedirectly" in r["text"].lower() for r in q)
    assert len(list(store_records(store_path, {}, limit=3))) == 3
    # kind is the schema kind in the id ("source:kind" too); type is the dataset's msg_type / action kind
    msgs = list(store_records(store_path, {"kind": "village:msg"}))
    assert msgs and all(r["kind"] == "msg" for r in msgs) and len(msgs) + len(opus) <= len(allrec)
    summaries = list(store_records(store_path, {"type": "session_summary"}))
    assert summaries and all(r["kind"] == "event" and r["action_kind"] == "session_summary" for r in summaries)
    assert list(store_records(store_path, {"kind": "event", "channel": "general"})) == []
    with pytest.raises(ValueError, match="Unknown record kind.*'type' filter"):
        list(store_records(store_path, {"kind": "chat"}))
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
    assert out["sent"] == 5 and all(v["event_id"].startswith("village:msg:") for v in out["verdicts"])
    assert "Unknown filter" in call_error(app, "sweep_run", rubric="q", filters={"actor": "x"})


def test_from_store_record_matches_store_records(data_dir: Path):
    app = build_server(config_for(data_dir))
    get_record = app.swarm_registry.store_api["get_record"]
    by_id = {r["event_id"]: r for r in store_records(data_dir / "swarmscope.duckdb")}
    msg = next(r for r in by_id.values() if r["kind"] == "msg" and len(r["text"]) > 20 and "@" not in r["text"])
    act = next(r for r in by_id.values() if r["kind"] == "event" and "@" not in r["text"])
    for want in (msg, act):
        got = from_store_record(get_record(want["event_id"], max_chars=4000))
        assert got == want  # same shape and values, whichever path produced it
    cut = from_store_record(get_record(msg["event_id"], max_chars=10))
    assert cut["truncated"] is True and len(cut["text"]) < len(msg["text"])
    secret = next(r for r in by_id.values() if r["kind"] == "msg" and "bob.smith" in r["text"])
    assert "bob.smith" not in from_store_record(get_record(secret["event_id"], max_chars=4000))["text"]  # masked
    agent_id = f"village:agent:{A_OPUS}"
    with pytest.raises(ValueError, match="no text to evaluate"):
        from_store_record(get_record(agent_id))


def test_export_store_redacts_and_checks(store_path: Path, tmp_path: Path):
    out = tmp_path / "export"
    res = export_store(store_path, out, {"source": "village"}, allow_email_domains=["agentvillage.org"])
    assert res["ok"] is True and res["check"]["findings"] == []
    assert res["records"]["by_source_kind"]["village"]["msg"] > 0
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
    aws_example = "AKIA" + "IOSFODNN7EXAMPLE"  # split so no source line looks like a key to scanners
    assert s(f"key {aws_example} and Bearer abcdefghijklmnopqrstuvwxyz012345") == (
        "key [credential] and Bearer [credential]"
    )
    # behaviour change from the regex-only scrubber: VCS remotes are no longer masked as emails
    assert s("clone git@github.com:org/repo.git") == "clone git@github.com:org/repo.git"
    assert s("x@y.com +1 415 555 0134") == "[email] [phone]"
    assert Scrubber(False)("x@y.com") == "x@y.com"
    assert mask("mail help@agentvillage.org") == "mail [email]"  # setup masking: no allowlist
    assert show({"token": "ghp_" + "a1B2" * 9}) == '{"token": "[credential]"}'
