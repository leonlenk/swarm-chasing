"""Every village tool against the synthetic dataset, plus helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import A_GEM, call, call_error, config_for

from swarm_mcp.modules.village import goal_type
from swarm_mcp.modules.village.names import NameMatcher, lab_for
from swarm_mcp.server import build_server
from swarm_mcp.toolkit import Scrubber, ToolInputError, parse_time


def test_requires_reports_missing_data(tmp_path: Path):
    app = build_server(config_for(tmp_path / "nothing"))
    rec = app.swarm_registry.records["village"]
    assert rec.status == "skipped" and "ai-village" in rec.reasons[0]
    (tmp_path / "nothing" / "ai-village").mkdir(parents=True)
    rec = build_server(config_for(tmp_path / "nothing")).swarm_registry.records["village"]
    assert any("chat_messages.jsonl.gz" in r for r in rec.reasons)


def test_village_dir_override(tmp_path: Path, data_dir: Path):
    app = build_server(config_for(tmp_path / "elsewhere", SWARM_VILLAGE_DIR=str(data_dir / "ai-village")))
    assert app.swarm_registry.records["village"].status == "loaded"


def test_agents(app):
    out = call(app, "village_agents")
    assert out["count"] == 4 and out["human_message_count"] == 1
    by = {a["name"]: a for a in out["agents"]}
    assert [a["name"] for a in out["agents"]][0] == "o3"  # sorted by join date
    gpt = by["GPT-5.2"]
    assert gpt["lab"] == "OpenAI" and gpt["model"].startswith("gpt-5.2")
    assert gpt["message_count"] == 253 and gpt["first_message"] == "2026-01-05T15:00:00Z"
    assert by["Claude Opus 4.5"]["lab"] == "Anthropic" and by["Gemini 2.5 Pro"]["lab"] == "Google DeepMind"
    assert by["o3"]["message_count"] == 0 and by["o3"]["participating"] is False
    active = call(app, "village_agents", include_departed=False, sort_by="messages")
    assert "o3" not in [a["name"] for a in active["agents"]] and active["agents"][0]["name"] == "GPT-5.2"


def test_goals(app):
    out = call(app, "village_goals")
    assert [g["index"] for g in out["goals"]] == [1, 2, 3]
    g1, g2, g3 = out["goals"]
    assert g1["goal"].startswith("Collaboratively") and g1["type"] == "collaborative"
    assert g2["type"] == "competitive" and g2["duration_days"] == 7.0
    assert g3["type"] == "holiday" and g3["ongoing"] is True and g3["end"] is None


def test_search_basic_filters_and_snippet(app):
    out = call(app, "village_search_chat", query="genuinely")
    assert out["total_matches"] == 2
    assert {r["speaker"] for r in out["results"]} == {"GPT-5.2", "Claude Opus 4.5"}
    assert all("genuinely" in r["snippet"] for r in out["results"])
    out = call(app, "village_search_chat", query="genuinely", agent="Opus 4.5")  # short form
    assert out["total_matches"] == 1 and out["filters"]["agent"] == "Claude Opus 4.5"
    out = call(app, "village_search_chat", query="GPT", room="rest")
    assert out["total_matches"] == 1 and out["results"][0]["room"] == "rest"
    out = call(app, "village_search_chat", query="genuinely", since="2026-01-10")
    assert out["total_matches"] == 1
    out = call(app, "village_search_chat", query="genuinely", until="2026-01-05")  # bare date = whole day
    assert out["total_matches"] == 1
    out = call(app, "village_search_chat", query="THE END", case_sensitive=True, snippet_chars=60)
    snip = out["results"][0]["snippet"]
    assert snip.startswith("…") and snip.endswith("THE END") and len(snip) < 120
    out = call(app, "village_search_chat", query="charity", agent="human")
    assert out["total_matches"] == 1 and out["results"][0]["speaker"].startswith("human:")


def test_search_regex_paging_and_limits(app):
    out = call(app, "village_search_chat", query=r"filler message \d+$", regex=True)
    assert out["total_matches"] == 250 and out["returned"] == 20 and out["has_more"] is True
    out = call(app, "village_search_chat", query="filler", limit=1000)
    assert out["returned"] == 200 and any("capped at the maximum of 200" in n for n in out["notes"])
    page2 = call(app, "village_search_chat", query="filler", limit=5, offset=5)
    assert [r["snippet"] for r in page2["results"]][0] == "filler message 5"
    newest = call(app, "village_search_chat", query="filler", limit=1, newest_first=True)
    assert newest["results"][0]["snippet"] == "filler message 249"


def test_search_errors_are_clean(app):
    assert "Invalid regex" in call_error(app, "village_search_chat", query="(oops", regex=True)
    assert "must not be empty" in call_error(app, "village_search_chat", query="  ")
    err = call_error(app, "village_search_chat", query="x", room="nope")
    assert "Unknown room" in err and "general" in err
    err = call_error(app, "village_search_chat", query="x", agent="Claud Opus 4.5x")
    assert "Unknown agent" in err and "Traceback" not in err
    assert "Could not parse since" in call_error(app, "village_search_chat", query="x", since="last week")


def test_privacy_scrubbing(app):
    out = call(app, "village_search_chat", query="gmail")
    snip = out["results"][0]["snippet"]
    assert "bob.smith" not in snip and "[email]" in snip
    msgs = call(app, "village_messages", start="2026-01-05", end="2026-01-07")["messages"]
    text = {m["id"]: m["content"] for m in msgs}
    assert "help@agentvillage.org" in text["m0001"]  # allow-listed domain kept
    assert text["m0004"] == "Contact [email] or call [phone] / [phone]."
    # dates, versions and counts are not phone numbers
    nine = call(app, "village_messages", start="2026-01-15", end="2026-01-15")["messages"][0]["content"]
    assert "1.234.5" in nine and "2026-01-15" in nine and "21,596" in nine


def test_scrubbing_can_be_disabled(data_dir: Path):
    app = build_server(config_for(data_dir, SWARM_MCP_SCRUB="0"))
    msgs = call(app, "village_messages", start="2026-01-06", end="2026-01-06")["messages"]
    assert "bob.smith@gmail.com" in msgs[0]["content"]


def test_messages_window_paging_and_truncation(app):
    out = call(app, "village_messages", start="2026-01-05", end="2026-01-07")  # bare end date = whole day
    assert [m["id"] for m in out["messages"]] == ["m0001", "m0002", "m0003", "m0004", "m0005"]  # chronological
    assert out["has_more"] is False and out["next_start"] is None
    out = call(app, "village_messages", start="2026-01-21", limit=10)
    assert out["returned"] == 10 and out["total_in_window"] == 250 and out["has_more"]
    nxt = call(app, "village_messages", start=out["next_start"], limit=1)
    assert nxt["messages"][0]["content"] == "filler message 10"
    out = call(app, "village_messages", start="2026-01-13", end="2026-01-14", max_chars=100)
    m = out["messages"][0]
    assert m["truncated"] is True and "[truncated," in m["content"] and len(m["content"]) < 160
    assert any("truncated to 100 chars" in n for n in out["notes"])
    out = call(app, "village_messages", start="2026-01-01", agent="gpt 5.2", room="rest")
    assert out["returned"] == 0
    out = call(app, "village_messages", start="2026-01-01", limit=0)
    assert out["returned"] == 1 and "raised to 1" in out["notes"][0]
    assert "must be after start" in call_error(app, "village_messages", start="2026-01-05", end="2026-01-04")


def test_agent_activity(app):
    out = call(app, "village_agent_activity", agent="GPT-5.2", bucket="week")
    assert out["agent"] == "GPT-5.2" and out["message_count"] == 253
    assert out["counts"] == [
        {"bucket": "2026-01-05", "messages": 2},
        {"bucket": "2026-01-12", "messages": 1},
        {"bucket": "2026-01-19", "messages": 250},
    ]
    names = {n["agent"]: n for n in out["names_most"]}
    # msg 3 names Opus once; msg 5 names Opus twice (short + full form) and o3 once
    assert names["Claude Opus 4.5"] == {"agent": "Claude Opus 4.5", "messages": 2, "mentions": 3}
    assert names["o3"]["messages"] == 1
    assert "GPT-5.2" not in names  # self-mention in msg 9 excluded
    named_by = {n["speaker"]: n["messages"] for n in out["named_by_most"]}
    assert named_by == {"Claude Opus 4.5": 2, "Gemini 2.5 Pro": 1}
    assert out["rooms"] == {"general": 253}
    assert "GPT-5.2" in out["aliases_matched"]

    by_goal = call(app, "village_agent_activity", agent="Opus 4.5", bucket="goal")
    assert [c["bucket"][:2] for c in by_goal["counts"]] == ["1:", "2:"]
    by_day = call(
        app, "village_agent_activity", agent="Claude Opus 4.5", bucket="day", since="2026-01-08", until="2026-01-13"
    )
    assert by_day["counts"] == [{"bucket": "2026-01-08", "messages": 1}, {"bucket": "2026-01-13", "messages": 1}]
    month = call(app, "village_agent_activity", agent=A_GEM, bucket="month")
    assert month["counts"] == [{"bucket": "2026-01", "messages": 2}]
    assert "needs a specific agent" in call_error(app, "village_agent_activity", agent="human")


def test_name_matcher_boundaries():
    m = NameMatcher(
        [
            ("opus45", "Claude Opus 4.5"),
            ("opus4", "Claude Opus 4"),
            ("cc", "Opus 4.5 (Claude Code)"),
            ("gpt5", "GPT-5"),
            ("gpt52", "GPT-5.2"),
            ("gem3", "Gemini 3 Pro"),
            ("gem31", "Gemini 3.1 Pro"),
            ("s37", "Claude 3.7 Sonnet"),
        ]
    )
    found = m.find(
        "Opus 4.5, opus 4, GPT 5.2, gpt-5, Gemini 3.1 and Gemini 3 met Sonnet 3.7 and Opus 4.5.1 and Opus 4.5 (Claude Code)"
    )
    assert found == ["opus45", "opus4", "gpt52", "gpt5", "gem31", "gem3", "s37", "cc"]
    assert m.resolve("@opus 4.5") == "opus45" and m.resolve("GPT5.2") == "gpt52" and m.resolve("human") == "human"
    with pytest.raises(ToolInputError, match="ambiguous"):
        m.resolve("Gemini")
    with pytest.raises(ToolInputError, match="Unknown agent"):
        m.resolve("Llama 9")


def test_helpers():
    assert lab_for("claude-code::claude-opus-4-5") == "Anthropic"
    assert lab_for("o4-mini-2025-04-16") == "OpenAI" and lab_for("z-ai/glm-5.2") == "Zhipu AI (Z.ai)"
    assert lab_for("tinker://abc/kimi-leader").startswith("Moonshot")
    assert goal_type("Choose your own goal!") == "self_directed"
    s = Scrubber(True, ["agentvillage.org"])
    assert s("a@b.agentvillage.org x@y.com") == "a@b.agentvillage.org [email]"
    assert s("call 555-123-4567 or +44 20 7946 0958, not 2026-01-05 or v1.2.3 or 12345678") == (
        "call [phone] or [phone], not 2026-01-05 or v1.2.3 or 12345678"
    )
    assert parse_time("2026-01-05") == "2026-01-05 00:00:00.000000"
    assert parse_time("2026-01-05", end=True) == "2026-01-06 00:00:00.000000"
    assert parse_time("2026-01-05T10:00:00+02:00") == "2026-01-05 08:00:00.000000"
    assert parse_time("2026-02", end=True) == "2026-03-01 00:00:00.000000"
    assert parse_time(None) is None
    with pytest.raises(ToolInputError):
        parse_time("soon")


def test_chat_is_loaded_lazily_and_once(app):
    cache = app.swarm_cache
    assert "village:chat" not in cache
    call(app, "village_goals")
    assert "village:chat" not in cache  # goals don't need chat
    call(app, "village_search_chat", query="x")
    assert "village:chat" in cache
    first = cache.get("village:chat", lambda: None)
    call(app, "village_messages", start="2026-01-01")
    assert cache.get("village:chat", lambda: None) is first
