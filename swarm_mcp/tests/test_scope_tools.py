"""The scope_* MCP tools over the synthetic store (see conftest.make_village)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
from conftest import A_GEM, A_GPT, A_OPUS, HUMAN_ID, call, call_error, config_for, run
from mcp import Client

from swarm_mcp import info
from swarm_mcp.scope import db
from swarm_mcp.scope.analysis import graph as graph_analysis
from swarm_mcp.scope.analysis import timeline as timeline_analysis
from swarm_mcp.server import build_server
from swarm_mcp.toolkit import HARD_MAX_CHARS, MIN_MAX_CHARS, ToolInputError

OPUS = f"village:agent:{A_OPUS}"
GPT = f"village:agent:{A_GPT}"
GEM = f"village:agent:{A_GEM}"
HUMAN = f"human:{HUMAN_ID}"
EMAIL = "bob.smith@gmail.com"
TEXT_KEYS = {"content", "snippet", "label", "text"}


def assert_wrapped(obj: Any, path: str = "$") -> int:
    """Every content/snippet/label/text field must be an untrusted wrapper. Returns how many were checked."""
    n = 0
    if isinstance(obj, dict):
        is_wrapper = obj.get("untrusted") is True
        for k, v in obj.items():
            if k in TEXT_KEYS and not (is_wrapper and k == "content"):
                assert isinstance(v, dict) and v.get("untrusted") is True, f"{path}.{k} is not wrapped: {v!r}"
                assert isinstance(v["content"], str)
                n += 1
            n += assert_wrapped(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            n += assert_wrapped(v, f"{path}[{i}]")
    return n


# ----------------------------------------------------------------------------- module loading


def test_module_skipped_without_store(raw_data_dir: Path):
    app = build_server(config_for(raw_data_dir))
    info = call(app, "core_info")
    skipped = {m["name"]: m for m in info["modules"]["skipped"]}
    assert "scope" in skipped
    assert "SwarmScope store not found" in skipped["scope"]["reasons"][0]
    assert "swarm-mcp add data/ai-village" in skipped["scope"]["reasons"][0]
    assert any("no SwarmScope store" in n for n in info["notes"])


# ----------------------------------------------------------------------------- core_info sources / agents


def test_core_info_sources(app):
    out = call(app, "core_info")
    srcs = {x["source"]: x for x in out["sources"]}
    src = srcs["village"]
    assert src["adapter"] == "ai_village" and src["path"].endswith("ai-village")
    assert src["row_counts"] == {
        "agents": 4,
        "messages": 260,
        "actions": 3,
        "periods": 3,
        "artifacts": 0,
        "touches": 0,
    }
    kinds = {k["kind"]: k for k in src["kinds"]}
    assert {k: v["records"] for k, v in kinds.items()} == {"msg": 260, "event": 3, "agent": 4, "goal": 3}
    assert kinds["msg"]["table"] == "messages" and kinds["msg"]["id_format"] == "village:msg:<id>"
    assert kinds["goal"]["table"] == "periods"
    assert src["ingest_counts"]["messages"] == 260
    assert src["ingest_meta"]["schema_version"] == 2 and isinstance(src["ingest_meta"]["notes"], list)
    assert src["channels"] == [{"channel": "general", "messages": 259}, {"channel": "rest", "messages": 1}]
    assert "channels_total" not in src
    assert src["messages_ts"] == {"min": "2026-01-05T13:00:00Z", "max": "2026-01-21T04:09:00Z"}
    assert src["actions_ts"]["min"] == "2026-01-05T13:30:00Z"
    assert src["action_kinds"] == {"session_goal": 2, "session_summary": 1}
    assert src["ingested_at"].endswith("Z")
    assert any("blind spots" in n for n in out["notes"]) and any("core_get" in n for n in out["notes"])
    assert out["findings"]["ok"] is True and out["findings"]["checked"] == 0
    assert {m["name"] for m in out["modules"]["loaded"]} >= {"core", "scope", "findings", "village"}
    assert out["config"]["db_exists"] is True and "api_key_set" in out["config"]["llm"]


def test_core_info_without_scope_module(data_dir: Path):
    """core_info reads the store itself, so sources show even with the scope module disabled."""
    app = build_server(config_for(data_dir, disable="scope"))
    out = call(app, "core_info")
    assert [s["source"] for s in out["sources"]] == ["village"]
    assert out["sources"][0]["row_counts"]["messages"] == 260
    assert "not loaded" in call_error(app, "core_get", ids="village:msg:m0001")


def test_core_info_blind_spot_notes_and_channel_cap(store_path: Path):
    con = duckdb.connect(str(store_path))
    try:
        con.execute(
            "UPDATE sources SET meta = ? WHERE source = 'village'",
            [json.dumps({"schema_version": 2, "notes": ["DMs are not in the export"]})],
        )
    finally:
        con.close()
    with db.connect(store_path) as s:
        src = info.store_sources(s, max_channels=1)[0]
    assert src["ingest_meta"]["notes"] == ["DMs are not in the export"]
    assert src["channels"] == [{"channel": "general", "messages": 259}] and src["channels_total"] == 2
    text = info.format_info(
        {
            "server": {"version": "x"},
            "config": {
                "config_file": None,
                "data_dir": "d",
                "db_path": "db",
                "db_exists": True,
                "findings_dir": "f",
                "sweeps_dir": "s",
                "llm": {"model": "m", "effort": "low", "api_key_set": False},
            },
            "modules": {"loaded": [], "skipped": []},
            "sources": [src],
            "findings": {"ok": True, "message": "ok"},
        }
    )
    assert "blind spot: DMs are not in the export" in text and "260 messages" in text


def test_agents(app):
    out = call(app, "scope_agents")
    assert out["total"] == 4 and out["returned"] == 4 and out["has_more"] is False
    assert [a["display_name"] for a in out["agents"]] == ["GPT-5.2", "Claude Opus 4.5", "Gemini 2.5 Pro", "o3"]
    opus = out["agents"][1]
    assert opus["agent_id"] == OPUS and "Opus 4.5" in opus["aliases"]
    assert opus["model_string"] == "claude-opus-4-5-20251101" and opus["lab"]
    assert opus["first_seen"] == "2026-01-05T14:00:00Z" and opus["last_seen"] == "2026-01-14T08:00:00Z"
    assert opus["message_count"] == 4
    o3 = out["agents"][3]
    assert o3["is_participating"] is False and o3["message_count"] == 0 and o3["first_seen"] is None
    assert out["human_authors"] == 1 and out["human_message_count"] == 1

    by_name = call(app, "scope_agents", sort_by="name", limit=2)
    assert [a["display_name"] for a in by_name["agents"]] == ["Claude Opus 4.5", "Gemini 2.5 Pro"]
    assert by_name["has_more"] is True
    joined = call(app, "scope_agents", sort_by="joined")
    assert joined["agents"][0]["display_name"] == "o3"
    assert "Unknown source" in call_error(app, "scope_agents", source="nope")


# ----------------------------------------------------------------------------- search


def test_search_phrase_and_author_alias(app):
    out = call(app, "scope_search", query="GENUINELY")
    assert out["total"] == 2 and out["returned"] == 2 and out["has_more"] is False
    assert [r["evidence_id"] for r in out["results"]] == ["village:msg:m0003", "village:msg:m0008"]
    r = out["results"][0]
    assert r["author"] == "GPT-5.2" and r["author_id"] == GPT and r["channel"] == "general"
    assert r["ts"] == "2026-01-05T15:00:00Z"
    assert assert_wrapped(out) == 2

    newest = call(app, "scope_search", query="genuinely", newest_first=True)
    assert newest["results"][0]["evidence_id"] == "village:msg:m0008"

    by_alias = call(app, "scope_search", query="genuinely", author="Opus 4.5")
    assert by_alias["total"] == 1 and by_alias["results"][0]["evidence_id"] == "village:msg:m0008"
    assert by_alias["filters"]["author"] == "Claude Opus 4.5"
    human = call(app, "scope_search", query="charity", author="human")
    assert [r["author_id"] for r in human["results"]] == [HUMAN]


def test_search_modes(app):
    terms = call(app, "scope_search", query="together charity", match="all_terms")
    assert [r["evidence_id"] for r in terms["results"]] == ["village:msg:m0002"]
    assert call(app, "scope_search", query="together charity")["total"] == 0  # phrase: no such substring

    rx = call(app, "scope_search", query=r"give\w+", match="regex")
    assert [r["evidence_id"] for r in rx["results"]] == ["village:msg:m0003"]

    # LIKE wildcards are literal in phrase mode
    assert call(app, "scope_search", query="%")["total"] == 0
    assert call(app, "scope_search", query="_")["total"] == 0
    assert call(app, "scope_search", query="1.234.5")["total"] == 1

    err = call_error(app, "scope_search", query="(unclosed", match="regex")
    assert "Invalid regex" in err and "Traceback" not in err
    assert call(app, "scope_search", query="   ")["mode"] == "read"  # blank query = read the window


def test_search_paging_and_filters(app):
    page = call(app, "scope_search", query="filler message", limit=20)
    assert page["total"] == 250 and page["returned"] == 20 and page["has_more"] is True
    assert page["next_offset"] == 20
    last = call(app, "scope_search", query="filler message", limit=20, offset=240)
    assert last["returned"] == 10 and last["has_more"] is False and "next_offset" not in last
    assert last["results"][-1]["evidence_id"] == "village:msg:m0349"

    rest = call(app, "scope_search", query="busy", channel="#REST")
    assert rest["total"] == 1 and rest["filters"]["channel"] == "rest"
    jan5 = call(app, "scope_search", query="charity", since="2026-01-05", until="2026-01-05")
    assert jan5["total"] == 2 and jan5["filters"]["until"] == "2026-01-06T00:00:00Z"
    assert call(app, "scope_search", query="charity", source="village")["total"] == 2


def test_search_errors(app):
    assert "Unknown agent" in call_error(app, "scope_search", query="x", author="nobody at all")
    err = call_error(app, "scope_search", query="x", channel="lobby")
    assert "Unknown channel" in err and "general" in err
    assert "Could not parse" in call_error(app, "scope_search", query="x", since="last tuesday")
    assert "must be before" in call_error(app, "scope_search", query="x", since="2026-01-10", until="2026-01-01")
    assert "channel only applies" in call_error(app, "scope_search", query="x", table="actions", channel="general")
    assert "max_chars" in call_error(app, "scope_search", query="x", max_chars=5)


def test_search_masks_email_and_centres_snippet(app):
    out = call(app, "scope_search", query="Contact")
    snip = out["results"][0]["text"]["content"]
    assert EMAIL not in snip and "[email]" in snip and "[phone]" in snip and "555" not in snip
    # allow-listed domain stays readable
    assert "help@agentvillage.org" in call(app, "scope_search", query="help@")["results"][0]["text"]["content"]

    long = call(app, "scope_search", query="THE END", max_chars=100)
    s = long["results"][0]["text"]
    assert s["truncated"] is True and s["total_chars"] > 1000
    assert "THE END" in s["content"] and s["content"].startswith("…") and len(s["content"]) <= 110


def test_search_actions(app):
    out = call(app, "scope_search", query="GiveDirectly", table="actions")
    assert out["total"] == 1
    r = out["results"][0]
    assert r["evidence_id"] == "village:event:e0003" and r["kind"] == "session_summary"
    assert r["agent"] == "Claude Opus 4.5" and r["agent_id"] == OPUS and "channel" not in r
    assert EMAIL not in r["text"]["content"] and "[email]" in r["text"]["content"]
    assert call(app, "scope_search", query="draft", table="actions", author="GPT-5.2")["total"] == 1


# ----------------------------------------------------------------------------- core_get on store records


def test_core_get_message_with_neighbors(app):
    out = call(app, "core_get", ids="village:msg:m0003", before=2, after=2)
    assert out["table"] == "messages" and out["evidence_id"] == "village:msg:m0003" and out["source"] == "village"
    assert out["author"] == "GPT-5.2" and out["author_id"] == GPT and out["channel"] == "general"
    assert out["ts"] == "2026-01-05T15:00:00Z" and out["ts_quality"] == "exact"
    assert out["meta"]["speaker_type"] == "agent" and out["reply_to"] is None
    assert {"agent_id": OPUS, "name": "Claude Opus 4.5"} in out["recipients"]
    assert "genuinely" in out["content"]["content"] and out["content"]["untrusted"] is True
    nb = out["neighbors"]
    assert [m["evidence_id"] for m in nb["before"]] == ["village:msg:m0001", "village:msg:m0002"]
    assert [m["evidence_id"] for m in nb["after"]] == ["village:msg:m0004", "village:msg:m0005"]
    assert nb["before"][0]["author"] == "human:u0000000"
    assert EMAIL not in str(out) and "[email]" in nb["after"][0]["snippet"]["content"]
    assert "recipients" not in nb["after"][0]  # detail only for the requested record
    assert "same channel" in out["context"]
    assert "artifacts" not in out  # the village touches no artifacts
    assert assert_wrapped(out) == 5

    # same-channel only: m0006 is the only message in #rest
    lone = call(app, "core_get", ids="village:msg:m0006", before=3, after=3)
    assert lone["neighbors"] == {"before": [], "after": []}
    plain = call(app, "core_get", ids="village:msg:m0006")
    assert "neighbors" not in plain and "context" not in plain


def test_core_get_masks_and_truncates(app):
    m4 = call(app, "core_get", ids="village:msg:m0004")
    assert EMAIL not in m4["content"]["content"] and "[email]" in m4["content"]["content"]

    full = call(app, "core_get", ids="village:msg:m0007")
    assert full["content"]["truncated"] is True and full["content"]["total_chars"] > 3000
    short = call(app, "core_get", ids="village:msg:m0007", max_chars=80)
    assert short["content"]["truncated"] is True and len(short["content"]["content"]) < 140
    big = call(app, "core_get", ids="village:msg:m0007", max_chars=20000)
    assert "truncated" not in big["content"] and big["content"]["content"].endswith("THE END")
    # neighbor snippets are capped at 200 chars even when max_chars is large
    m8 = call(app, "core_get", ids="village:msg:m0008", before=1, max_chars=20000)
    assert m8["neighbors"]["before"][0]["evidence_id"] == "village:msg:m0007"
    assert len(m8["neighbors"]["before"][0]["snippet"]["content"]) < 260
    assert "max_chars" in call_error(app, "core_get", ids="village:msg:m0007", max_chars=5)


def test_core_get_action_agent_period(app):
    act = call(app, "core_get", ids="village:event:e0003", before=2, after=2)
    assert act["table"] == "actions" and act["kind"] == "session_summary" and act["agent_id"] == OPUS
    assert act["agent"] == "Claude Opus 4.5" and act["ts"] == "2026-01-05T14:30:00Z"
    assert EMAIL not in act["content"]["content"] and "[email]" in act["content"]["content"]
    assert [a["evidence_id"] for a in act["neighbors"]["before"]] == ["village:event:e0001"]
    assert act["neighbors"]["before"][0]["kind"] == "session_goal"
    assert act["neighbors"]["after"] == [] and "same agent" in act["context"]
    assert_wrapped(act)

    agent = call(app, "core_get", ids=OPUS)
    assert agent["table"] == "agents" and agent["display_name"] == "Claude Opus 4.5" and agent["agent_id"] == OPUS
    assert "Opus 4.5" in agent["aliases"] and agent["first_seen"] == "2026-01-05T14:00:00Z"
    assert agent["message_count"] == 4 and agent["action_count"] == 2 and "neighbors" not in agent
    assert agent["meta"]["model_string"] == "claude-opus-4-5-20251101"

    goal = call(app, "core_get", ids="village:goal:g1")
    assert goal["table"] == "periods" and goal["kind"] == "village_goal" and goal["label"]["untrusted"] is True
    assert "charity" in goal["label"]["content"]
    assert goal["start"] == "2026-01-05T12:00:00Z" and goal["end"] == "2026-01-12T12:00:00Z"
    assert goal["messages_in_period"] == 6 and goal["ongoing"] is False
    assert "records" not in goal  # no action points at the goal via run_id
    assert call(app, "core_get", ids="village:goal:g3")["ongoing"] is True


def test_core_get_batch_and_bad_ids(app):
    assert "Malformed evidence id" in call_error(app, "core_get", ids="m0003")
    assert "Unknown evidence kind 'post'" in call_error(app, "core_get", ids="village:post:1")
    assert "Unknown evidence kind 'chat'" in call_error(app, "core_get", ids="village:chat:m0003")  # pre-v2 kind
    err = call_error(app, "core_get", ids="village:msg:nope")
    assert "does not resolve" in err and "Traceback" not in err
    out = call(app, "core_get", ids=["village:msg:m0001", OPUS, "village:msg:nope", "bad"])
    assert out["requested"] == 4 and out["returned"] == 2
    assert [r["evidence_id"] for r in out["results"]] == ["village:msg:m0001", OPUS]
    assert [r["table"] for r in out["results"]] == ["messages", "agents"]
    assert [e["id"] for e in out["errors"]] == ["village:msg:nope", "bad"]
    assert "does not resolve" in out["errors"][0]["error"] and "Malformed" in out["errors"][1]["error"]
    ctxd = call(app, "core_get", ids=["village:msg:m0003"], before=1)
    assert ctxd["results"][0]["neighbors"]["before"][0]["evidence_id"] == "village:msg:m0002"
    assert "At most 50" in call_error(app, "core_get", ids=[f"village:msg:m{i:04d}" for i in range(51)])
    assert "must not be empty" in call_error(app, "core_get", ids=[])


ART = "village:artifact:charity-notes.md"


@pytest.fixture
def art_app(data_dir: Path):
    """The synthetic village plus one artifact touched by two messages and an action, and two actions that
    belong to goal g1 through run_id (as commits belong to their pull request)."""
    con = duckdb.connect(str(data_dir / "swarmscope.duckdb"))
    try:
        con.execute(
            "INSERT INTO artifacts (artifact_id, source, kind, name, meta) VALUES (?, 'village', 'file', ?, ?)",
            [ART, "charity-notes.md", json.dumps({"role": "notes"})],
        )
        for rid, op, ts in [
            ("village:msg:m0002", "create", "2026-01-05 14:00:00"),
            ("village:event:e0003", "modify", "2026-01-05 14:30:00"),
            ("village:msg:m0003", "mention", "2026-01-05 15:00:00"),
        ]:
            con.execute(
                "INSERT INTO touches (touch_id, source, record_id, artifact_id, op, ts, meta) "
                "VALUES (?, 'village', ?, ?, ?, CAST(? AS TIMESTAMP), '{}')",
                [f"{rid}|{op}|{ART}", rid, ART, op, ts],
            )
        con.execute(
            "UPDATE actions SET run_id = 'village:goal:g1' "
            "WHERE evidence_id IN ('village:event:e0001', 'village:event:e0003')"
        )
    finally:
        con.close()
    return build_server(config_for(data_dir))


def test_core_get_artifacts_touches_and_period_members(art_app):
    art = call(art_app, "core_get", ids=ART)
    assert art["table"] == "artifacts" and art["evidence_id"] == ART and art["source"] == "village"
    assert art["kind"] == "file" and art["name"] == "charity-notes.md" and art["meta"] == {"role": "notes"}
    assert art["touches_by_op"] == {"create": 1, "modify": 1, "mention": 1}
    assert art["first_touches"] == [
        {"evidence_id": "village:msg:m0002", "op": "create", "ts": "2026-01-05T14:00:00Z"},
        {"evidence_id": "village:event:e0003", "op": "modify", "ts": "2026-01-05T14:30:00Z"},
        {"evidence_id": "village:msg:m0003", "op": "mention", "ts": "2026-01-05T15:00:00Z"},
    ]
    assert art["last_touches"] == []

    # messages and actions list the artifacts they touched
    assert call(art_app, "core_get", ids="village:msg:m0002")["artifacts"] == [{"artifact_id": ART, "op": "create"}]
    assert call(art_app, "core_get", ids="village:event:e0003")["artifacts"] == [{"artifact_id": ART, "op": "modify"}]
    assert "artifacts" not in call(art_app, "core_get", ids="village:msg:m0001")

    # a period lists its member records (actions whose run_id is the period)
    goal = call(art_app, "core_get", ids="village:goal:g1")
    assert goal["records"] == ["village:event:e0001", "village:event:e0003"]
    assert "records" not in call(art_app, "core_get", ids="village:goal:g2")

    # every id an artifact or period points at resolves
    batch = call(art_app, "core_get", ids=[t["evidence_id"] for t in art["first_touches"]] + goal["records"])
    assert batch["errors"] == [] and batch["returned"] == 5

    src = {s["source"]: s for s in call(art_app, "core_info")["sources"]}["village"]
    assert src["row_counts"]["artifacts"] == 1 and src["row_counts"]["touches"] == 3
    kinds = {k["kind"]: k for k in src["kinds"]}
    assert kinds["artifact"]["records"] == 1 and kinds["artifact"]["id_format"] == "village:artifact:<id>"


def test_core_get_many_touches_keeps_first_and_last(art_app, store_path: Path):
    con = duckdb.connect(str(store_path))
    try:
        for i in range(20):
            rid = f"village:msg:m{100 + i:04d}"
            con.execute(
                "INSERT INTO touches (touch_id, source, record_id, artifact_id, op, ts, meta) "
                "VALUES (?, 'village', ?, ?, 'read', CAST(? AS TIMESTAMP), '{}')",
                [f"{rid}|read|{ART}", rid, ART, f"2026-01-21 00:{i:02d}:00"],
            )
    finally:
        con.close()
    art = call(art_app, "core_get", ids=ART)
    assert art["touches_by_op"]["read"] == 20 and len(art["first_touches"]) == 10
    assert [t["evidence_id"] for t in art["last_touches"]] == [f"village:msg:m{115 + i:04d}" for i in range(5)]


# ----------------------------------------------------------------------------- scope_search without a query


def test_read_window(app):
    out = call(app, "scope_search", since="2026-01-05", until="2026-01-05")
    assert out["mode"] == "read" and out["total"] == 3 and out["has_more"] is False
    assert [m["evidence_id"] for m in out["results"]] == [f"village:msg:m000{i}" for i in (1, 2, 3)]
    assert out["results"][0]["author"] == "human:u0000000"
    assert assert_wrapped(out) == 3

    m4 = call(app, "scope_search", since="2026-01-06", until="2026-01-06")
    assert m4["results"][0]["evidence_id"] == "village:msg:m0004"
    assert EMAIL not in str(m4) and "[email]" in m4["results"][0]["text"]["content"]

    opus = call(app, "scope_search", since="2026-01-01", author="Opus 4.5")
    assert opus["total"] == 4 and opus["filters"]["author"] == "Claude Opus 4.5"
    assert call(app, "scope_search", channel="rest")["total"] == 1
    long = call(app, "scope_search", since="2026-01-13", until="2026-01-13", max_chars=40)
    assert long["results"][0]["text"]["truncated"] is True
    newest = call(app, "scope_search", newest_first=True, limit=1)
    assert newest["results"][0]["evidence_id"] == "village:msg:m0349" and newest["total"] == 260
    acts = call(app, "scope_search", table="actions")
    assert [r["evidence_id"] for r in acts["results"]] == [
        "village:event:e0001",
        "village:event:e0003",
        "village:event:e0004",
    ]


def test_read_paging(app):
    page = call(app, "scope_search", since="2026-01-21", limit=100)
    assert page["total"] == 250 and page["returned"] == 100 and page["has_more"] is True
    assert page["next_offset"] == 100
    nxt = call(app, "scope_search", since="2026-01-21", limit=200, offset=page["next_offset"])
    assert nxt["results"][0]["evidence_id"] == "village:msg:m0200"
    assert nxt["returned"] == 150 and nxt["has_more"] is False


def test_read_errors(app):
    assert "Could not parse" in call_error(app, "scope_search", since="soon")
    assert "must be before" in call_error(app, "scope_search", since="2026-01-10", until="2026-01-01")
    assert "Unknown channel" in call_error(app, "scope_search", channel="nope")
    assert "Unknown agent" in call_error(app, "scope_search", author="Claude Haiku 9")


# ----------------------------------------------------------------------------- scope_agents(name=...)


def test_agent_profile(app):
    out = call(app, "scope_agents", name="Opus 4.5")
    assert out["agent_id"] == OPUS and out["display_name"] == "Claude Opus 4.5"
    assert out["model_string"] == "claude-opus-4-5-20251101" and out["lab"]
    assert out["first_seen"] == "2026-01-05T14:00:00Z" and out["last_seen"] == "2026-01-14T08:00:00Z"
    assert out["message_count"] == 4
    assert out["channels"] == [{"channel": "general", "messages": 3}, {"channel": "rest", "messages": 1}]
    # only (general, 2026-01-05) is shared with GPT; Gemini posts on other days
    assert out["top_co_channel_agents"] == [{"agent_id": GPT, "name": "GPT-5.2", "shared_channel_days": 1}]
    assert out["active_channel_days"] == 4
    assert GPT in {r["agent_id"] for r in out["names_most"]}
    named_by = {r["author_id"]: r["messages"] for r in out["named_by_most"]}
    assert named_by[GPT] == 2 and named_by[GEM] == 1
    assert out["actions_by_kind"] == {"session_goal": 1, "session_summary": 1}
    assert out["busiest_day"] == {"day": "2026-01-05", "messages": 1, "village_day": 1, "tz": "America/Los_Angeles"}
    samples = out["samples"]
    assert samples["first"]["evidence_id"] == "village:msg:m0002"
    assert samples["last"]["evidence_id"] == "village:msg:m0008"
    assert len(samples["spread"]) == 4
    assert [s["ts"] for s in samples["spread"]] == sorted(s["ts"] for s in samples["spread"])
    assert assert_wrapped(out) == 6
    assert len(samples["first"]["snippet"]["content"]) <= 200 + 50

    again = call(app, "scope_agents", name="Opus 4.5", samples=2)
    assert [s["evidence_id"] for s in again["samples"]["spread"]] == [
        s["evidence_id"] for s in call(app, "scope_agents", name="Opus 4.5", samples=2)["samples"]["spread"]
    ]


def test_agent_profile_window_and_errors(app):
    out = call(app, "scope_agents", name=OPUS, since="2026-01-10")
    assert out["message_count"] == 2 and out["actions_by_kind"] == {}
    assert out["samples"]["first"]["evidence_id"] == "village:msg:m0007"
    assert out["filters"]["since"] == "2026-01-10T00:00:00Z"
    gpt = call(app, "scope_agents", name="gpt-5.2")
    # the 250 fillers (2026-01-21 00:00-04:09 UTC) fall on the Pacific evening of 2026-01-20: Village day 16
    assert gpt["busiest_day"] == {"day": "2026-01-20", "messages": 250, "village_day": 16, "tz": "America/Los_Angeles"}
    assert "Unknown agent" in call_error(app, "scope_agents", name="Nobody")


def test_ambiguous_agent_lists_source_and_id(data_dir: Path):
    """The same display name in two sources: the error names each candidate's source and id."""
    con = duckdb.connect(str(data_dir / "swarmscope.duckdb"))
    try:  # same name and same id tail as the village agent, in another source
        con.execute(
            "INSERT INTO agents (agent_id, source, display_name, aliases) VALUES (?, 'aaa', 'Claude Opus 4.5', [])",
            [f"aaa:agent:{A_OPUS}"],
        )
        con.execute("INSERT INTO sources (source) VALUES ('aaa')")
    finally:
        con.close()
    app = build_server(config_for(data_dir))
    for tool, args in [
        ("scope_search", {"author": "Claude Opus 4.5"}),
        ("scope_timeline", {"author": "Claude Opus 4.5"}),
        ("scope_periods", {"agent": "Claude Opus 4.5"}),
        ("scope_agents", {"name": "Claude Opus 4.5"}),
    ]:
        err = call_error(app, tool, **args)
        assert f"Claude Opus 4.5 (aaa, aaa:agent:{A_OPUS}); Claude Opus 4.5 (village, {OPUS})" in err, (tool, err)
        assert "source=" in err and "agent_id" in err
    # either way out works: a full agent_id (even with the same tail) or source=
    assert call(app, "scope_agents", name=OPUS)["agent_id"] == OPUS
    assert call(app, "scope_search", author="Claude Opus 4.5", source="village")["total"] == 4


# ----------------------------------------------------------------------------- timeline


def test_timeline_series(app):
    out = call(app, "scope_timeline")  # one AI Village source: buckets are Village days (Pacific)
    assert out["total"] == 260 and out["bins_returned"] == 8
    assert out["peak"] == {"bucket": "2026-01-20T08:00:00Z", "count": 251, "village_day": 16}
    assert out["series"][0] == {"bucket": "2026-01-05T08:00:00Z", "count": 3, "village_day": 1}
    assert out["village_days"] == {"tz": "America/Los_Angeles", "day_one": "2026-01-05"}
    assert sum(b["count"] for b in out["series"]) == 260
    assert any("omitted" in n for n in out["notes"])

    week = call(app, "scope_timeline", bin="week", author="Opus 4.5")
    assert week["total"] == 4 and week["filters"]["author"] == "Claude Opus 4.5"
    assert week["series"][0] == {"bucket": "2026-01-05T08:00:00Z", "count": 2}  # Monday, Pacific midnight
    hour = call(app, "scope_timeline", bin="hour", since="2026-01-21", channel="general")
    assert hour["total"] == 250 and hour["bins_returned"] == 5
    assert hour["series"][0] == {"bucket": "2026-01-21T00:00:00Z", "count": 60, "village_day": 16}


def test_village_days_only_for_one_village_source(data_dir: Path):
    """busiest_day and timeline buckets use Village days for village data only; other sources stay UTC."""
    con = duckdb.connect(str(data_dir / "swarmscope.duckdb"))
    try:  # a non-village source with one agent posting late on 2026-01-05 UTC (= 2026-01-05 PST)
        con.execute("INSERT INTO sources (source) VALUES ('aaa')")
        con.execute(
            "INSERT INTO agents (agent_id, source, display_name, aliases) VALUES ('aaa:agent:x', 'aaa', 'X', [])"
        )
        con.execute(
            "INSERT INTO periods VALUES ('aaa:period:p1', 'aaa', 'pull_request', 'PR', TIMESTAMP '2026-01-05', "
            "TIMESTAMP '2026-01-07', '{}')"
        )
        for i, ts in enumerate(["2026-01-05 23:00:00", "2026-01-06 01:00:00", "2026-01-06 02:00:00"]):
            con.execute(
                "INSERT INTO messages (evidence_id, source, channel, author_id, recipient_ids, ts, content) "
                "VALUES (?, 'aaa', 'c', 'aaa:agent:x', [], CAST(? AS TIMESTAMP), 'synthetic')",
                [f"aaa:msg:{i}", ts],
            )
    finally:
        con.close()
    app = build_server(config_for(data_dir))
    assert call(app, "scope_agents", name="X")["busiest_day"] == {"day": "2026-01-06", "messages": 2}
    pr = call(app, "scope_periods", name="aaa:period:p1")
    assert pr["busiest_day"] == {"day": "2026-01-06", "messages": 2} and "UTC date" in pr["notes"][-1]
    goal = call(app, "scope_periods", name="charity")
    assert goal["busiest_day"]["village_day"] == 1 and "Village day" in goal["notes"][-1]

    aaa = call(app, "scope_timeline", source="aaa")
    assert aaa["series"] == [
        {"bucket": "2026-01-05T00:00:00Z", "count": 1},
        {"bucket": "2026-01-06T00:00:00Z", "count": 2},
    ]
    mixed = call(app, "scope_timeline")
    assert "village_days" not in mixed and all("village_day" not in b for b in mixed["series"])
    assert any("pass source=" in n for n in mixed["notes"])
    village = call(app, "scope_timeline", source="village")
    assert village["series"][0] == {"bucket": "2026-01-05T08:00:00Z", "count": 3, "village_day": 1}
    assert call(app, "scope_timeline", author="Opus 4.5")["series"][0]["village_day"] == 1  # one source by author


def test_timeline_grouping(app):
    out = call(app, "scope_timeline", group_by="author", top_groups=2)
    assert [g["group"] for g in out["groups"]] == ["GPT-5.2", "Claude Opus 4.5"]
    assert out["groups"][0]["group_id"] == GPT and out["groups"][0]["total"] == 253
    assert sum(b["count"] for b in out["groups"][1]["series"]) == 4
    assert out["other"] == {"groups": 2, "total": 3}

    ch = call(app, "scope_timeline", group_by="channel", bin="month")
    assert {g["group"]: g["total"] for g in ch["groups"]} == {"general": 259, "rest": 1}
    assert ch["other"]["total"] == 0

    acts = call(app, "scope_timeline", table="actions", group_by="author")
    assert {g["group"]: g["total"] for g in acts["groups"]} == {"Claude Opus 4.5": 2, "GPT-5.2": 1}
    assert "only applies" in call_error(app, "scope_timeline", table="actions", group_by="channel")
    assert "Unknown channel" in call_error(app, "scope_timeline", channel="nowhere")


def test_timeline_bucket_cap(store_path: Path):
    with db.connect(store_path) as s:
        with pytest.raises(ToolInputError, match="coarser|bin='day'"):
            timeline_analysis.timeline(s, bin="hour", max_buckets=5)
        assert timeline_analysis.timeline(s, bin="month")["bins_returned"] == 1


# ----------------------------------------------------------------------------- graph


def test_comm_graph_mentions(app):
    out = call(app, "scope_graph")
    edges = {(e["source"], e["target"], e["type"]): e for e in out["edges"]}
    gpt_opus = edges[(GPT, OPUS, "mention")]
    assert gpt_opus["weight"] == 2 and gpt_opus["evidence_id"] == "village:msg:m0003"  # m0003 and m0005
    assert gpt_opus["source_name"] == "GPT-5.2" and gpt_opus["target_name"] == "Claude Opus 4.5"
    assert (OPUS, GPT, "mention") in edges
    assert not [e for e in out["edges"] if e["type"] == "reply"]  # synthetic messages are hours apart
    assert all(not e["source"].startswith("human:") for e in out["edges"])
    assert out["totals"]["messages_considered"] == 260
    names = {n["name"] for n in out["nodes"]}
    assert {"Claude Opus 4.5", "GPT-5.2", "Gemini 2.5 Pro"} <= names
    assert out["top_betweenness"] and all("betweenness" in n for n in out["nodes"])
    assert any("reply edge" in n for n in out["notes"])

    # the example evidence id resolves to a message that really names the target
    rec = call(app, "core_get", ids=gpt_opus["evidence_id"])
    assert rec["author_id"] == GPT and OPUS in {r["agent_id"] for r in rec["recipients"]}


def test_comm_graph_replies_humans_and_filters(app):
    out = call(app, "scope_graph", edge_types="replies", reply_window_minutes=90)
    assert [(e["source"], e["target"], e["evidence_id"]) for e in out["edges"]] == [(GPT, OPUS, "village:msg:m0003")]
    assert out["totals"]["edges_by_type"] == {"reply": 1}

    with_h = call(app, "scope_graph", edge_types="replies", reply_window_minutes=90, include_humans=True)
    pairs = {(e["source"], e["target"]): e["evidence_id"] for e in with_h["edges"]}
    assert pairs[(OPUS, HUMAN)] == "village:msg:m0002"
    assert any(n["agent_id"] == HUMAN and n["name"] == "human:u0000000" for n in with_h["nodes"])

    heavy = call(app, "scope_graph", min_weight=2)
    assert heavy["edges"] and all(e["weight"] >= 2 for e in heavy["edges"])
    assert heavy["totals"]["edges_below_min_weight"] > 0

    rest = call(app, "scope_graph", channel="rest")
    assert rest["totals"]["messages_considered"] == 1
    assert all(e["evidence_id"] == "village:msg:m0006" for e in rest["edges"])
    capped = call(app, "scope_graph", max_edges=1, top_nodes=1)
    assert len(capped["edges"]) == 1 and len(capped["nodes"]) == 1 and capped["totals"]["edges"] > 1
    assert "Unknown channel" in call_error(app, "scope_graph", channel="nope")


def test_comm_graph_analysis_direct(store_path: Path):
    with db.connect(store_path) as s:
        out = graph_analysis.comm_graph(s, edge_types="mentions", since="2026-01-07 00:00:00.000000")
        assert {(e["source"], e["target"]) for e in out["edges"]} >= {(GPT, OPUS)}
        assert out["totals"]["messages_considered"] == 256


# ----------------------------------------------------------------------------- protocol


def test_mcp_client_roundtrip(data_dir: Path):
    app = build_server(config_for(data_dir))
    expected = {
        "scope_agents",
        "scope_search",
        "scope_periods",
        "scope_timeline",
        "scope_graph",
        "scope_recap",
        "scope_moments",
    }

    async def go():
        async with Client(app) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert {t for t in tools if t.startswith("scope_")} == expected
            for name in expected:
                assert tools[name].annotations.read_only_hint is True
                assert tools[name].description
            schema = tools["scope_search"].input_schema
            assert not schema.get("required")  # no query = read the window
            assert "description" in schema["properties"]["max_chars"]
            assert tools["core_get"].input_schema["required"] == ["ids"]

            res = await client.call_tool("scope_search", {"query": "Contact", "limit": 1})
            assert res.is_error is False
            hit = res.structured_content["results"][0]
            assert hit["evidence_id"] == "village:msg:m0004" and hit["text"]["untrusted"] is True
            assert EMAIL not in str(res.structured_content)

            bad = await client.call_tool("core_get", {"ids": "village:msg:missing"})
            assert bad.is_error is True and "does not resolve" in bad.content[0].text

            graph = await client.call_tool("scope_graph", {"edge_types": "mentions"})
            assert graph.is_error is False and graph.structured_content["totals"]["edges"] > 0

    run(go())


# ----------------------------------------------------------------------------- scope_periods paging

N_SPRINTS = 5000


@pytest.fixture
def many_periods_app(data_dir: Path):
    """The synthetic village plus 5,000 hourly 'sprint' periods (after the 3 goals in list order)."""
    con = duckdb.connect(str(data_dir / "swarmscope.duckdb"))
    try:
        con.execute(
            "INSERT INTO periods SELECT 'village:sprint:s' || lpad(CAST(i AS TEXT), 5, '0'), 'village', 'sprint', "
            "'Sprint ' || i || ' ' || repeat('x', 200), TIMESTAMP '2026-01-05' + i * INTERVAL 1 HOUR, "
            "TIMESTAMP '2026-01-05' + (i + 1) * INTERVAL 1 HOUR, '{}' FROM range(?) t(i)",
            [N_SPRINTS],
        )
    finally:
        con.close()
    return build_server(config_for(data_dir))


def test_periods_list_is_paged(many_periods_app):
    out = call(many_periods_app, "scope_periods")
    assert out["total"] == out["count"] == N_SPRINTS + 3
    assert out["returned"] == len(out["periods"]) == 50 and out["has_more"] is True and out["next_offset"] == 50
    assert [p["index"] for p in out["periods"]] == list(range(1, 51))
    assert len(json.dumps(out)) < 40_000  # was ~2.4 MB for the whole list

    nxt = call(many_periods_app, "scope_periods", limit=100, offset=out["next_offset"])
    assert nxt["returned"] == 100 and nxt["offset"] == 50 and nxt["next_offset"] == 150
    assert [p["index"] for p in nxt["periods"]] == list(range(51, 151))
    assert "capped at the maximum of 200" in str(call(many_periods_app, "scope_periods", limit=1000)["notes"])

    last = call(many_periods_app, "scope_periods", limit=10, offset=N_SPRINTS)
    assert last["returned"] == 3 and last["has_more"] is False and "next_offset" not in last
    past = call(many_periods_app, "scope_periods", offset=N_SPRINTS + 10)
    assert past["returned"] == 0 and "past the last period" in str(past["notes"])
    assert "offset" in call_error(many_periods_app, "scope_periods", offset=-1)


def test_periods_kind_filter_keeps_global_index(many_periods_app):
    goals = call(many_periods_app, "scope_periods", kind="village_goal")
    assert goals["total"] == 3 and goals["filters"] == {"kind": "village_goal"} and goals["has_more"] is False
    assert {p["kind"] for p in goals["periods"]} == {"village_goal"}

    sprints = call(many_periods_app, "scope_periods", kind="sprint", source="village", limit=5, offset=100)
    assert sprints["total"] == N_SPRINTS and sprints["filters"] == {"source": "village", "kind": "sprint"}
    assert {p["kind"] for p in sprints["periods"]} == {"sprint"}
    # indexes are those of the unfiltered list, so name=<index> still finds the same period
    for p in sprints["periods"]:
        assert call(many_periods_app, "scope_periods", name=str(p["index"]))["evidence_id"] == p["evidence_id"]
    assert call(many_periods_app, "scope_periods", kind="nope")["total"] == 0


def test_period_index_is_store_wide_across_sources(data_dir: Path):
    """An index listed under scope_periods(source=...) must name the same period everywhere."""
    con = duckdb.connect(str(data_dir / "swarmscope.duckdb"))
    try:  # a second source that sorts before 'village', with two periods
        con.execute(
            "INSERT INTO periods VALUES ('aaa:period:p1', 'aaa', 'pull_request', 'PR one', "
            "TIMESTAMP '2026-01-05', TIMESTAMP '2026-01-06', '{}'), ('aaa:period:p2', 'aaa', 'pull_request', "
            "'PR two', TIMESTAMP '2026-01-06', TIMESTAMP '2026-01-07', '{}')"
        )
    finally:
        con.close()
    app = build_server(config_for(data_dir))
    listed = call(app, "scope_periods", source="village")["periods"]
    assert [p["index"] for p in listed] == [3, 4, 5]
    for p in listed:
        i = str(p["index"])
        assert call(app, "scope_periods", name=i)["evidence_id"] == p["evidence_id"]
        assert call(app, "scope_periods", name=i, source="village")["evidence_id"] == p["evidence_id"]
        assert call(app, "scope_recap", period=i)["period"]["evidence_id"] == p["evidence_id"]
        assert call(app, "scope_recap", period=i, source="village")["period"]["index"] == p["index"]
    assert [p["index"] for p in call(app, "scope_periods")["periods"]] == [1, 2, 3, 4, 5]
    assert "belongs to source 'aaa'" in call_error(app, "scope_recap", period="1", source="village")
    assert "out of range 1..5" in call_error(app, "scope_periods", name="9")
    assert call(app, "scope_periods", name="charity", source="village")["index"] == 3


def test_periods_counts_match_and_use_few_queries(many_periods_app, store_path: Path, monkeypatch):
    calls = []
    for meth in ("all", "scalar"):
        orig = getattr(db.Store, meth)

        def counted(self, sql, params=None, _orig=orig):
            calls.append(sql)
            return _orig(self, sql, params)

        monkeypatch.setattr(db.Store, meth, counted)
    out = call(many_periods_app, "scope_periods", agent="Opus 4.5", kind="sprint", limit=100)
    assert out["returned"] == 100 and out["agent"] == "Claude Opus 4.5"
    assert len(calls) < 15  # one GROUP BY per page, not one query per period
    monkeypatch.undo()

    con = duckdb.connect(str(store_path), read_only=True)
    try:
        for p in out["periods"]:
            start, end = con.execute(
                "SELECT start_ts, end_ts FROM periods WHERE evidence_id = ?", [p["evidence_id"]]
            ).fetchone()
            win = "ts >= ? AND ts < ?"
            want_agent = con.execute(
                f"SELECT count(*) FROM messages WHERE author_id = ? AND {win}", [OPUS, start, end]
            ).fetchone()[0]
            want_msgs, want_active = con.execute(
                f"SELECT count(*), count(DISTINCT author_id) FILTER (WHERE author_id NOT LIKE 'human:%') "
                f"FROM messages WHERE source = 'village' AND {win}",
                [start, end],
            ).fetchone()
            assert (p["agent_messages"], p["messages"], p["active_agents"]) == (want_agent, want_msgs, want_active)
    finally:
        con.close()
    assert sum(p["agent_messages"] for p in out["periods"]) > 0 and sum(p["messages"] for p in out["periods"]) > 0


# ----------------------------------------------------------------------------- one max_chars range for every tool

MAX_CHARS_TOOLS = {
    "core_get": {"ids": "village:msg:m0007"},
    "scope_search": {"query": "THE END"},
    "scope_agents": {"name": "Opus 4.5"},
    "findings_list": {"sample": 1},
}


def test_max_chars_range_is_shared(app):
    call(app, "findings_record", claim="m0007 is long", evidence_ids=["village:msg:m0007"])
    for tool, args in MAX_CHARS_TOOLS.items():
        for bad in (-5, 0, MIN_MAX_CHARS - 1, HARD_MAX_CHARS + 1, 50000):
            assert "max_chars" in call_error(app, tool, **args, max_chars=bad), (tool, bad)
        for ok in (MIN_MAX_CHARS, 80, HARD_MAX_CHARS):
            call(app, tool, **args, max_chars=ok)

    async def schemas():
        async with Client(app) as client:
            return {t.name: t.input_schema for t in (await client.list_tools()).tools}

    tools = run(schemas())
    for tool in MAX_CHARS_TOOLS:
        prop = tools[tool]["properties"]["max_chars"]
        bounds = [b for b in prop.get("anyOf", [prop]) if b.get("type") == "integer"][0]
        assert (bounds["minimum"], bounds["maximum"]) == (MIN_MAX_CHARS, HARD_MAX_CHARS), tool
        assert f"{MIN_MAX_CHARS}..{HARD_MAX_CHARS}" in prop["description"], tool


def test_findings_sample_max_chars_raises_and_lowers_the_snippet(app):
    call(app, "findings_record", claim="m0007 is long", evidence_ids=["village:msg:m0007"])

    def snippet(**kw):
        return call(app, "findings_list", sample=1, **kw)["items"][0]["evidence"][0]["content"]

    assert len(snippet()["content"]) < 260 and snippet()["truncated"] is True  # default 200
    wide = snippet(max_chars=2000)
    assert 2000 <= len(wide["content"]) < 2100 and wide["truncated"] is True
    assert len(snippet(max_chars=MIN_MAX_CHARS)["content"]) < 80
    assert snippet(max_chars=HARD_MAX_CHARS)["content"].endswith("THE END")
