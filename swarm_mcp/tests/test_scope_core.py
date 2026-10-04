"""SwarmScope core: adapter mapping, ingest idempotency, evidence ids, the untrusted wrapper."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import A_GPT, A_OPUS, HUMAN_ID, build_store

from swarm_mcp.scope import db, evidence
from swarm_mcp.scope.adapters import get_adapter
from swarm_mcp.toolkit import Scrubber, ToolInputError, untrusted


def test_adapter_inspect_lists_fields(raw_data_dir: Path):
    info = get_adapter("ai_village").inspect(raw_data_dir / "ai-village")
    assert info["missing_required"] == []
    assert "content" in info["fields"]["chat_messages.jsonl.gz"]


def test_adapter_mapping(store_path: Path):
    with db.connect(store_path) as s:
        counts = {t: s.scalar(f"SELECT count(*) FROM {t}") for t in ("agents", "messages", "actions", "periods")}
        assert counts == {"agents": 4, "messages": 260, "actions": 3, "periods": 3}

        opus = s.one("SELECT * FROM agents WHERE display_name = 'Claude Opus 4.5'")
        assert opus["agent_id"] == f"village:agent:{A_OPUS}"
        assert "Opus 4.5" in opus["aliases"]
        assert '"claude-opus-4-5-20251101"' in opus["meta"]
        assert str(opus["first_seen"]).startswith("2026-01-05 14:00")
        assert str(opus["last_seen"]).startswith("2026-01-14 08:00")

        m2 = s.one("SELECT * FROM messages WHERE evidence_id = 'village:msg:m0002'")
        assert m2["channel"] == "general" and m2["author_id"] == f"village:agent:{A_OPUS}"
        assert m2["ts_quality"] == "exact" and m2["msg_type"] is None
        # "GPT-5.2" and "Gemini 2.5" are named; recipients are agent ids, deduplicated
        assert set(m2["recipient_ids"]) >= {f"village:agent:{A_GPT}"}
        assert len(m2["recipient_ids"]) == len(set(m2["recipient_ids"]))

        m5 = s.one("SELECT * FROM messages WHERE evidence_id = 'village:msg:m0005'")
        # GPT names "Opus 4.5" and "Claude Opus 4.5" (same agent) and "o3": self excluded, deduplicated
        assert m5["recipient_ids"].count(f"village:agent:{A_OPUS}") == 1
        assert f"village:agent:{A_GPT}" not in m5["recipient_ids"]

        human = s.one("SELECT * FROM messages WHERE evidence_id = 'village:msg:m0001'")
        assert human["author_id"] == f"human:{HUMAN_ID}"

        rest = s.one("SELECT channel FROM messages WHERE evidence_id = 'village:msg:m0006'")
        assert rest["channel"] == "rest"

        kinds = {r["kind"]: r["n"] for r in s.all("SELECT kind, count(*) n FROM actions GROUP BY 1")}
        assert kinds == {"session_goal": 2, "session_summary": 1}
        goal = s.one("SELECT * FROM periods ORDER BY start_ts LIMIT 1")
        assert goal["evidence_id"] == "village:goal:g1" and goal["kind"] == "village_goal"


def test_ingest_is_idempotent(data_dir: Path, store_path: Path):
    build_store(data_dir)  # second run replaces the source's rows
    with db.connect(store_path) as s:
        assert s.scalar("SELECT count(*) FROM messages") == 260
        assert s.scalar("SELECT count(*) FROM sources") == 1


def test_ingest_without_events(raw_data_dir: Path, tmp_path: Path):
    from swarm_mcp.scope.ingest import ingest

    out = ingest("ai_village", raw_data_dir / "ai-village", tmp_path / "x.duckdb", include_events=False)
    assert out["counts"]["actions"] == 0 and out["counts"]["messages"] == 260


def test_evidence_parse_and_resolve(store_path: Path):
    ref = evidence.parse("village:msg:m0003")
    assert (ref.source, ref.kind, ref.native_id, ref.table) == ("village", "msg", "m0003", "messages")
    assert evidence.make("village", "agent", A_OPUS) == f"village:agent:{A_OPUS}"
    with db.connect(store_path) as s:
        rec = evidence.resolve(s, "village:msg:m0003")
        assert rec["table"] == "messages" and "genuinely" in rec["record"]["content"]
        assert evidence.resolve(s, f"village:agent:{A_GPT}")["record"]["display_name"] == "GPT-5.2"
        assert evidence.resolve(s, "village:event:e0001")["record"]["kind"] == "session_goal"
        assert evidence.resolve(s, "village:goal:g2")["table"] == "periods"

        with pytest.raises(evidence.EvidenceError, match="does not resolve"):
            evidence.resolve(s, "village:msg:not-a-real-id")
        with pytest.raises(evidence.EvidenceError, match="Malformed"):
            evidence.resolve(s, "m0003")
        with pytest.raises(evidence.EvidenceError, match="Unknown evidence kind"):
            evidence.resolve(s, "village:tweet:1")
        ok, bad = evidence.check(s, ["village:msg:m0001", "village:msg:nope"])
        assert ok == ["village:msg:m0001"] and list(bad) == ["village:msg:nope"]


def test_resolve_agent_by_name_alias_and_id(store_path: Path):
    with db.connect(store_path) as s:
        assert s.resolve_agent("Claude Opus 4.5")["agent_id"].endswith(A_OPUS)
        assert s.resolve_agent("opus 4.5")["agent_id"].endswith(A_OPUS)
        assert s.resolve_agent("GPT 5.2")["agent_id"].endswith(A_GPT)
        assert s.resolve_agent(f"village:agent:{A_GPT}")["display_name"] == "GPT-5.2"
        with pytest.raises(ToolInputError, match="Unknown agent"):
            s.resolve_agent("Nobody 9")
        assert s.author_filter("human")[2] == "human"


def test_missing_store_is_a_clear_error(tmp_path: Path):
    with pytest.raises(db.StoreMissing, match="swarm-mcp ingest"):
        with db.connect(tmp_path / "nope.duckdb"):
            pass


def test_untrusted_wrapper_masks_caps_and_delimits():
    scrub = Scrubber(True, ["agentvillage.org"])
    out = untrusted("mail bob@gmail.com or help@agentvillage.org, call +1 415 555 0134", scrub)
    assert out == {
        "content": "mail [email] or help@agentvillage.org, call [phone]",
        "untrusted": True,
    }
    long = "x" * 2000
    cut = untrusted(long, scrub)
    assert cut["truncated"] is True and cut["total_chars"] == 2000 and len(cut["content"]) < 560
    focused = untrusted(("a" * 1000) + " NEEDLE " + ("b" * 1000), scrub, 100, re.compile("needle", re.I))
    assert "NEEDLE" in focused["content"] and focused["content"].startswith("…")
    assert evidence.snippet("hello", 3)["content"].startswith("hel")
