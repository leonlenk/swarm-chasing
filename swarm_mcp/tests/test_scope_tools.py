"""The scope_* MCP tools over the synthetic store (see conftest.make_village)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import A_GEM, A_GPT, A_OPUS, HUMAN_ID, call, call_error, config_for, run
from mcp import Client

from swarm_mcp.scope import db
from swarm_mcp.scope.analysis import graph as graph_analysis
from swarm_mcp.scope.analysis import timeline as timeline_analysis
from swarm_mcp.server import build_server
from swarm_mcp.toolkit import ToolInputError

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


# ----------------------------------------------------------------------------- list_sources / agents


def test_core_info_sources(app):
    out = call(app, "core_info")
    srcs = {x["source"]: x for x in out["sources"]}
    src = srcs["village"]
    assert {k["kind"] for k in src["kinds"]} == {"chat", "event", "agent", "goal"}
    st = src["store"]
    assert st["adapter"] == "ai_village"
    assert st["row_counts"] == {"agents": 4, "messages": 260, "actions": 3, "periods": 3}
    assert st["channels"] == [{"channel": "general", "messages": 259}, {"channel": "rest", "messages": 1}]
    assert st["messages_ts"] == {"min": "2026-01-05T13:00:00Z", "max": "2026-01-21T04:09:00Z"}
    assert st["actions_ts"]["min"] == "2026-01-05T13:30:00Z"
    assert st["action_kinds"] == {"session_goal": 2, "session_summary": 1}
    assert st["ingested_at"].endswith("Z")
    assert out["findings"]["ok"] is True and out["findings"]["checked"] == 0
    assert {m["name"] for m in out["modules"]["loaded"]} >= {"core", "scope", "findings", "village"}
    assert out["config"]["db_exists"] is True and "api_key_set" in out["config"]["llm"]


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
    assert [r["evidence_id"] for r in out["results"]] == ["village:chat:m0003", "village:chat:m0008"]
    r = out["results"][0]
    assert r["author"] == "GPT-5.2" and r["author_id"] == GPT and r["channel"] == "general"
    assert r["ts"] == "2026-01-05T15:00:00Z"
    assert assert_wrapped(out) == 2

    newest = call(app, "scope_search", query="genuinely", newest_first=True)
    assert newest["results"][0]["evidence_id"] == "village:chat:m0008"

    by_alias = call(app, "scope_search", query="genuinely", author="Opus 4.5")
    assert by_alias["total"] == 1 and by_alias["results"][0]["evidence_id"] == "village:chat:m0008"
    assert by_alias["filters"]["author"] == "Claude Opus 4.5"
    human = call(app, "scope_search", query="charity", author="human")
    assert [r["author_id"] for r in human["results"]] == [HUMAN]


def test_search_modes(app):
    terms = call(app, "scope_search", query="together charity", match="all_terms")
    assert [r["evidence_id"] for r in terms["results"]] == ["village:chat:m0002"]
    assert call(app, "scope_search", query="together charity")["total"] == 0  # phrase: no such substring

    rx = call(app, "scope_search", query=r"give\w+", match="regex")
    assert [r["evidence_id"] for r in rx["results"]] == ["village:chat:m0003"]

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
    assert last["results"][-1]["evidence_id"] == "village:chat:m0349"

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
    out = call(app, "core_get", ids="village:chat:m0003", before=2, after=2)
    ev = out["event"]
    assert ev["event_id"] == "village:chat:m0003" and ev["kind"] == "chat"
    assert ev["actor"] == "GPT-5.2" and ev["actor_id"] == GPT and ev["location"] == "general"
    assert ev["time"] == "2026-01-05T15:00:00Z" and ev["meta"]["speaker_type"] == "agent"
    assert {"agent_id": OPUS, "name": "Claude Opus 4.5"} in ev["recipients"]
    assert "genuinely" in ev["text"]
    assert [m["event_id"] for m in out["before"]] == ["village:chat:m0001", "village:chat:m0002"]
    assert [m["event_id"] for m in out["after"]] == ["village:chat:m0004", "village:chat:m0005"]
    assert out["before"][0]["actor"] == "human:u0000000"
    assert EMAIL not in str(out) and "[email]" in out["after"][0]["text"]
    assert "recipients" not in out["after"][0]  # detail only for the requested record

    # same-channel only: m0006 is the only message in #rest
    lone = call(app, "core_get", ids="village:chat:m0006", before=3, after=3)
    assert lone["before"] == [] and lone["after"] == []
    assert call(app, "core_get", ids="village:chat:m0006")["before"] == []


def test_core_get_masks_and_truncates(app):
    m4 = call(app, "core_get", ids="village:chat:m0004")["event"]
    assert EMAIL not in m4["text"] and "[email]" in m4["text"]
    full = call(app, "core_get", ids="village:chat:m0007")
    assert full["event"]["truncated"] is True and any("truncated" in n for n in full["notes"])
    short = call(app, "core_get", ids="village:chat:m0007", max_chars=80)["event"]
    assert short["truncated"] is True and len(short["text"]) < 140
    big = call(app, "core_get", ids="village:chat:m0007", max_chars=20000)["event"]
    assert "truncated" not in big and big["text"].endswith("THE END")


def test_core_get_action_agent_period(app):
    act = call(app, "core_get", ids="village:event:e0003", before=2, after=2)
    assert act["event"]["action_kind"] == "session_summary" and act["event"]["actor_id"] == OPUS
    assert EMAIL not in act["event"]["text"]
    assert [a["event_id"] for a in act["before"]] == ["village:event:e0001"]
    assert act["after"] == [] and "same agent" in act["context"]

    agent = call(app, "core_get", ids=OPUS)
    assert agent["event"]["text"] == "Claude Opus 4.5" and agent["before"] == []
    assert agent["event"]["message_count"] == 4 and agent["event"]["action_count"] == 2
    assert agent["event"]["meta"]["model_string"] == "claude-opus-4-5-20251101"

    goal = call(app, "core_get", ids="village:goal:g1")["event"]
    assert "charity" in goal["text"] and goal["period_kind"] == "village_goal"
    assert goal["start"] == "2026-01-05T12:00:00Z" and goal["end"] == "2026-01-12T12:00:00Z"
    assert goal["messages_in_period"] == 6
    assert call(app, "core_get", ids="village:goal:g3")["event"]["ongoing"] is True


def test_core_get_batch_and_bad_ids(app):
    assert "Malformed event_id" in call_error(app, "core_get", ids="m0003")
    assert "has no kind 'post'" in call_error(app, "core_get", ids="village:post:1")
    err = call_error(app, "core_get", ids="village:chat:nope")
    assert "No 'chat' record" in err and "Traceback" not in err
    out = call(app, "core_get", ids=["village:chat:m0001", OPUS, "village:chat:nope", "bad"])
    assert out["requested"] == 4 and out["returned"] == 2
    assert [r["event"]["event_id"] for r in out["results"]] == ["village:chat:m0001", OPUS]
    assert [e["id"] for e in out["errors"]] == ["village:chat:nope", "bad"]
    ctxd = call(app, "core_get", ids=["village:chat:m0003"], before=1)
    assert ctxd["results"][0]["before"][0]["event_id"] == "village:chat:m0002"
    assert "At most 50" in call_error(app, "core_get", ids=[f"village:chat:m{i:04d}" for i in range(51)])
    assert "must not be empty" in call_error(app, "core_get", ids=[])


# ----------------------------------------------------------------------------- scope_search without a query


def test_read_window(app):
    out = call(app, "scope_search", since="2026-01-05", until="2026-01-05")
    assert out["mode"] == "read" and out["total"] == 3 and out["has_more"] is False
    assert [m["evidence_id"] for m in out["results"]] == [f"village:chat:m000{i}" for i in (1, 2, 3)]
    assert out["results"][0]["author"] == "human:u0000000"
    assert assert_wrapped(out) == 3

    m4 = call(app, "scope_search", since="2026-01-06", until="2026-01-06")
    assert m4["results"][0]["evidence_id"] == "village:chat:m0004"
    assert EMAIL not in str(m4) and "[email]" in m4["results"][0]["text"]["content"]

    opus = call(app, "scope_search", since="2026-01-01", author="Opus 4.5")
    assert opus["total"] == 4 and opus["filters"]["author"] == "Claude Opus 4.5"
    assert call(app, "scope_search", channel="rest")["total"] == 1
    long = call(app, "scope_search", since="2026-01-13", until="2026-01-13", max_chars=40)
    assert long["results"][0]["text"]["truncated"] is True
    newest = call(app, "scope_search", newest_first=True, limit=1)
    assert newest["results"][0]["evidence_id"] == "village:chat:m0349" and newest["total"] == 260
    acts = call(app, "scope_search", table="actions")
    assert [r["evidence_id"] for r in acts["results"]] == ["village:event:e0001", "village:event:e0003", "village:event:e0004"]


def test_read_paging(app):
    page = call(app, "scope_search", since="2026-01-21", limit=100)
    assert page["total"] == 250 and page["returned"] == 100 and page["has_more"] is True
    assert page["next_offset"] == 100
    nxt = call(app, "scope_search", since="2026-01-21", limit=200, offset=page["next_offset"])
    assert nxt["results"][0]["evidence_id"] == "village:chat:m0200"
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
    assert out["busiest_day"] == {"day": "2026-01-05", "messages": 1}
    samples = out["samples"]
    assert samples["first"]["evidence_id"] == "village:chat:m0002"
    assert samples["last"]["evidence_id"] == "village:chat:m0008"
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
    assert out["samples"]["first"]["evidence_id"] == "village:chat:m0007"
    assert out["filters"]["since"] == "2026-01-10T00:00:00Z"
    gpt = call(app, "scope_agents", name="gpt-5.2")
    assert gpt["busiest_day"] == {"day": "2026-01-21", "messages": 250}
    assert "Unknown agent" in call_error(app, "scope_agents", name="Nobody")


# ----------------------------------------------------------------------------- timeline


def test_timeline_series(app):
    out = call(app, "scope_timeline")
    assert out["total"] == 260 and out["bins_returned"] == 9
    assert out["peak"] == {"bucket": "2026-01-21T00:00:00Z", "count": 250}
    assert out["series"][0] == {"bucket": "2026-01-05T00:00:00Z", "count": 3}
    assert sum(b["count"] for b in out["series"]) == 260
    assert any("omitted" in n for n in out["notes"])

    week = call(app, "scope_timeline", bin="week", author="Opus 4.5")
    assert week["total"] == 4 and week["filters"]["author"] == "Claude Opus 4.5"
    assert week["series"][0]["bucket"] == "2026-01-05T00:00:00Z"  # a Monday
    hour = call(app, "scope_timeline", bin="hour", since="2026-01-21", channel="general")
    assert hour["total"] == 250 and hour["bins_returned"] == 5


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
    assert gpt_opus["weight"] == 2 and gpt_opus["evidence_id"] == "village:chat:m0003"  # m0003 and m0005
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
    rec = call(app, "core_get", ids=gpt_opus["evidence_id"])["event"]
    assert rec["actor_id"] == GPT and OPUS in {r["agent_id"] for r in rec["recipients"]}


def test_comm_graph_replies_humans_and_filters(app):
    out = call(app, "scope_graph", edge_types="replies", reply_window_minutes=90)
    assert [(e["source"], e["target"], e["evidence_id"]) for e in out["edges"]] == [(GPT, OPUS, "village:chat:m0003")]
    assert out["totals"]["edges_by_type"] == {"reply": 1}

    with_h = call(app, "scope_graph", edge_types="replies", reply_window_minutes=90, include_humans=True)
    pairs = {(e["source"], e["target"]): e["evidence_id"] for e in with_h["edges"]}
    assert pairs[(OPUS, HUMAN)] == "village:chat:m0002"
    assert any(n["agent_id"] == HUMAN and n["name"] == "human:u0000000" for n in with_h["nodes"])

    heavy = call(app, "scope_graph", min_weight=2)
    assert heavy["edges"] and all(e["weight"] >= 2 for e in heavy["edges"])
    assert heavy["totals"]["edges_below_min_weight"] > 0

    rest = call(app, "scope_graph", channel="rest")
    assert rest["totals"]["messages_considered"] == 1
    assert all(e["evidence_id"] == "village:chat:m0006" for e in rest["edges"])
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
    expected = {"scope_agents", "scope_search", "scope_periods", "scope_timeline", "scope_graph"}

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
            assert hit["evidence_id"] == "village:chat:m0004" and hit["text"]["untrusted"] is True
            assert EMAIL not in str(res.structured_content)

            bad = await client.call_tool("core_get", {"ids": "village:chat:missing"})
            assert bad.is_error is True and "No 'chat' record" in bad.content[0].text

            graph = await client.call_tool("scope_graph", {"edge_types": "mentions"})
            assert graph.is_error is False and graph.structured_content["totals"]["edges"] > 0

    run(go())
