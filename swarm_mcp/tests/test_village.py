"""The village module (goal views over the store), the ingest-time name matcher, and shared helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import call, call_error, config_for

from swarm_mcp.modules.village import goal_type
from swarm_mcp.scope.names import NameMatcher, lab_for
from swarm_mcp.server import build_server
from swarm_mcp.toolkit import Scrubber, ToolInputError, parse_time


def test_requires_reports_missing_store(tmp_path: Path, raw_data_dir: Path):
    app = build_server(config_for(tmp_path / "nothing"))
    rec = app.swarm_registry.records["village"]
    assert rec.status == "skipped" and "swarm-mcp ingest ai_village" in rec.reasons[0]
    # raw data alone is not enough: the store is the source of truth
    rec = build_server(config_for(raw_data_dir)).swarm_registry.records["village"]
    assert rec.status == "skipped" and "store not found" in rec.reasons[0]


def test_resources_use_village_dir_override(tmp_path: Path, data_dir: Path):
    app = build_server(config_for(data_dir))
    assert app.swarm_registry.records["village"].resources == ["village://schema"]
    elsewhere = tmp_path / "docs"
    elsewhere.mkdir()
    (elsewhere / "CHANGELOG.md").write_text("# changes\n")
    app = build_server(config_for(data_dir, SWARM_VILLAGE_DIR=str(elsewhere)))
    assert app.swarm_registry.records["village"].resources == ["village://changelog"]


def test_goals(app):
    out = call(app, "village_goals")
    assert [g["index"] for g in out["goals"]] == [1, 2, 3]
    g1, g2, g3 = out["goals"]
    assert g1["goal"].startswith("Collaboratively") and g1["type"] == "collaborative"
    assert g1["evidence_id"] == "village:goal:g1" and g1["start"] == "2026-01-05T12:00:00Z"
    assert (g1["messages"], g1["active_agents"]) == (6, 3)
    assert g2["type"] == "competitive" and g2["duration_days"] == 7.0 and g2["messages"] == 3
    assert g3["type"] == "holiday" and g3["ongoing"] is True and g3["end"] is None and g3["messages"] == 251


def test_goals_per_agent(app):
    out = call(app, "village_goals", agent="Opus 4.5")
    assert out["agent"] == "Claude Opus 4.5"
    assert [g["agent_messages"] for g in out["goals"]] == [2, 2, 0]
    assert "Unknown agent" in call_error(app, "village_goals", agent="Nobody 9")


def test_goal_detail(app):
    g = call(app, "village_goal", goal="charity")
    assert g["index"] == 1 and g["human_messages"] == 1
    speakers = {s["author"]: s["messages"] for s in g["top_speakers"]}
    assert speakers["Claude Opus 4.5"] == 2 and speakers["GPT-5.2"] == 2 and speakers["Gemini 2.5 Pro"] == 1
    assert g["busiest_day"] == {"day": "2026-01-05", "messages": 3}
    assert {c["channel"]: c["messages"] for c in g["channels"]} == {"general": 5, "rest": 1}
    assert g["actions"] == {"session_goal": 2, "session_summary": 1}
    assert "scope_comm_graph" in g["notes"][0]
    assert call(app, "village_goal", goal="3")["evidence_id"] == "village:goal:g3"
    assert call(app, "village_goal", goal="village:goal:g2")["index"] == 2
    assert "out of range" in call_error(app, "village_goal", goal="9")
    assert "No village goal" in call_error(app, "village_goal", goal="knitting")
    assert "matches 2 goals" in call_error(app, "village_goal", goal="y")  # ambiguous substring (goals 1 and 3)


def test_dropped_tools_are_gone(app):
    tools = {t.name for t in app._tool_manager.list_tools()}
    assert {"village_goals", "village_goal"} <= tools
    assert not tools & {"village_agents", "village_search_chat", "village_messages", "village_agent_activity"}


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
    assert "Opus 4.5" in m.aliases_for("opus45") and "Sonnet 3.7" in m.aliases_for("s37")


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
