"""The village module (docs resources), scope_periods over the village goals, the name matcher and helpers."""

from __future__ import annotations

from datetime import datetime
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
    assert rec.status == "skipped" and "swarm-mcp add data/ai-village" in rec.reasons[0]
    # raw data alone is not enough: the store is the source of truth
    rec = build_server(config_for(raw_data_dir)).swarm_registry.records["village"]
    assert rec.status == "skipped" and "store not found" in rec.reasons[0]


def test_resources_use_village_dir_override(tmp_path: Path, data_dir: Path):
    app = build_server(config_for(data_dir))
    assert app.swarm_registry.records["village"].resources == ["village://schema"]
    elsewhere = tmp_path / "docs"
    elsewhere.mkdir()
    (elsewhere / "CHANGELOG.md").write_text("# changes\n")
    app = build_server(config_for(data_dir, settings={"village": {"dir": str(elsewhere)}}))
    assert app.swarm_registry.records["village"].resources == ["village://changelog"]


def test_periods_list(app):
    out = call(app, "scope_periods")
    assert [g["index"] for g in out["periods"]] == [1, 2, 3]
    g1, g2, g3 = out["periods"]
    assert g1["label"]["content"].startswith("Collaboratively") and g1["label"]["untrusted"] is True
    assert g1["type"] == "collaborative" and g1["kind"] == "village_goal" and g1["source"] == "village"
    assert g1["evidence_id"] == "village:goal:g1" and g1["start"] == "2026-01-05T12:00:00Z"
    assert (g1["messages"], g1["active_agents"]) == (6, 3)
    assert g2["type"] == "competitive" and g2["duration_days"] == 7.0 and g2["messages"] == 3
    assert g3["type"] == "holiday" and g3["ongoing"] is True and g3["end"] is None and g3["messages"] == 251
    assert call(app, "scope_periods", source="village")["count"] == 3
    assert "Unknown source" in call_error(app, "scope_periods", source="nope")


def test_periods_per_agent(app):
    out = call(app, "scope_periods", agent="Opus 4.5")
    assert out["agent"] == "Claude Opus 4.5"
    assert [g["agent_messages"] for g in out["periods"]] == [2, 2, 0]
    assert "Unknown agent" in call_error(app, "scope_periods", agent="Nobody 9")


def test_period_detail(app):
    g = call(app, "scope_periods", name="charity")
    assert g["index"] == 1 and g["human_messages"] == 1
    speakers = {s["author"]: s["messages"] for s in g["top_speakers"]}
    assert speakers["Claude Opus 4.5"] == 2 and speakers["GPT-5.2"] == 2 and speakers["Gemini 2.5 Pro"] == 1
    assert g["busiest_day"] == {"day": "2026-01-05", "messages": 3, "village_day": 1, "tz": "America/Los_Angeles"}
    assert {c["channel"]: c["messages"] for c in g["channels"]} == {"general": 5, "rest": 1}
    assert g["actions"] == {"session_goal": 2, "session_summary": 1}
    assert "scope_graph" in g["notes"][0]
    assert call(app, "scope_periods", name="3")["evidence_id"] == "village:goal:g3"
    assert call(app, "scope_periods", name="village:goal:g2")["index"] == 2
    assert "out of range" in call_error(app, "scope_periods", name="9")
    assert "No period matches" in call_error(app, "scope_periods", name="knitting")
    assert "matches 2 periods" in call_error(app, "scope_periods", name="y")  # ambiguous substring (goals 1 and 3)


def test_village_has_no_tools(app):
    rec = app.swarm_registry.records["village"]
    assert rec.status == "loaded" and rec.tools == []
    tools = {t.name for t in app._tool_manager.list_tools()}
    assert not {t for t in tools if t.startswith("village_")}


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


def test_ai_village_parse_ts_converts_an_offset_to_utc():
    from swarm_mcp.scope.adapters.ai_village import parse_ts

    assert parse_ts("2025-12-29T10:00:00-08:00") == datetime(2025, 12, 29, 18, 0)  # converted, not stripped
    assert parse_ts("2025-12-29T18:00:00Z") == datetime(2025, 12, 29, 18, 0)
    assert parse_ts("2025-12-29 18:49:21.291984") == datetime(2025, 12, 29, 18, 49, 21, 291984)  # naive = UTC
    assert parse_ts("not a time") is None and parse_ts(None) is None
