"""analysis/series.py: daily metric series with a trailing rolling mean and a 95% band (synthetic data only)."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest

from swarm_mcp.scope import db
from swarm_mcp.scope.analysis.series import count_band, metric_series, read_sweep, rolling, wilson
from swarm_mcp.toolkit import ToolInputError


def _ids_by_content(store_path: Path) -> dict[str, str]:
    """Fixture message ids keyed by a content prefix (so tests don't hard-code the id scheme)."""
    with db.connect(store_path) as s:
        rows = s.all("SELECT evidence_id, content FROM messages ORDER BY ts, evidence_id")
    return {r["content"][:12]: r["evidence_id"] for r in rows}


# ---------------------------------------------------------------- the math, by hand


def test_count_band_hand_computed():
    m, lo, hi = count_band([2, 4])  # mean 3, sample var 2 < mean 3, so se = sqrt(3 / 2)
    assert m == 3
    assert lo == pytest.approx(3 - 1.959964 * math.sqrt(1.5), abs=1e-5)
    assert hi == pytest.approx(3 + 1.959964 * math.sqrt(1.5), abs=1e-5)
    m, lo, hi = count_band([10, 10, 10])  # zero variance falls back to Poisson: se = sqrt(10 / 3)
    assert (m, round(lo, 3), round(hi, 3)) == (10, round(10 - 1.959964 * math.sqrt(10 / 3), 3), 13.578)
    assert count_band([0, 0])[1] == 0.0  # clipped at zero


def test_wilson_matches_reference():
    lo, hi = wilson(5, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.2366, 0.7634)
    lo, hi = wilson(0, 4)
    assert lo == 0.0 and hi == pytest.approx(0.4899, abs=1e-4)


def test_rolling_window_over_active_days():
    d = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 5)]  # Jan 4 inactive
    num = {d[0]: 2, d[1]: 4, d[3]: 6}  # silent on the active Jan 3
    pts = rolling(d, num, num, kind="count", window=3, span=(d[0], d[3]))
    assert [p[0] for p in pts] == [0, 1, 2, 3]
    assert [p[2] for p in pts] == [2, 3, 2, 3]  # [2], [2,4], [2,4,0], [0,6]: Jan 4 is not a zero day
    assert pts[2][1] == 0  # the raw value on a silent active day

    hits = {d[0]: 1, d[1]: 1, d[3]: 3}
    den = {d[0]: 2, d[1]: 4, d[3]: 6}
    pts = rolling(d, hits, den, kind="rate", window=3, span=(d[0], d[3]))
    assert pts[0][1] == 0.5 and pts[1][1] == 0.25 and pts[2][1] is None
    assert pts[2][2] == pytest.approx(2 / 6)  # pooled (1 + 1 + 0) / (2 + 4 + 0)
    assert pts[3][2] == pytest.approx(3 / 6)  # Jan 3 has no records, Jan 5 has 3 of 6
    assert pts[3][3:5] == pytest.approx(wilson(3, 6))


# ---------------------------------------------------------------- on the synthetic store


def test_messages_by_agent(store_path: Path):
    with db.connect(store_path) as s:
        r = metric_series(s, metric="messages", by="agent")
    assert r["kind"] == "count" and r["window"] == 7
    assert [g["name"] for g in r["groups"]] == ["GPT-5.2", "Claude Opus 4.5", "Gemini 2.5 Pro"]
    assert [g["total_den"] for g in r["groups"]] == [253, 4, 2]
    assert r["excluded"]["human_messages"] == 1
    # Village days: day 1 is the first village goal's Pacific date (2026-01-05)
    assert r["days"]["day_one"] == "2026-01-05" and r["day_numbers"][0] == 1
    assert len(r["dates"]) == len(r["starts"]) == len(r["day_numbers"])
    # the 250 filler messages at 00:00-04:09 UTC on Jan 21 fall on the Pacific date Jan 20 (Day 16)
    gpt = r["groups"][0]
    by_day = {r["day_numbers"][p[0]]: p[1] for p in gpt["points"]}
    assert by_day[16] == 250
    assert all(len(p) == 7 for p in gpt["points"])
    assert any("trailing 7-day mean" in n for n in r["notes"])


def test_mention_rate_by_lab_and_utc_dates(store_path: Path):
    with db.connect(store_path) as s:
        r = metric_series(s, metric="mention_rate", by="lab", day_one=False)
    assert r["days"] is None and r["day_numbers"] is None
    labs = {g["name"]: g for g in r["groups"]}
    assert list(labs) == ["Anthropic", "OpenAI", "Google"]
    # Claude Opus 4.5 names another agent in 3 of its 4 messages; Gemini in 1 of 2
    assert (labs["Anthropic"]["total_num"], labs["Anthropic"]["total_den"]) == (3, 4)
    assert (labs["Google"]["total_num"], labs["Google"]["total_den"]) == (1, 2)
    assert r["dates"][0] == "2026-01-05" and r["starts"][0] == "2026-01-05T00:00:00Z"


def test_channel_and_window_filters(store_path: Path):
    with db.connect(store_path) as s:
        r = metric_series(s, by="channel", since="2026-01-05", until="2026-01-10")
        assert {g["name"]: g["total_den"] for g in r["groups"]} == {"#general": 4, "#rest": 1}
        r = metric_series(s, by="all", channel="rest")
        assert r["groups"][0]["name"] == "All agents" and r["groups"][0]["total_den"] == 1


def test_sweep_join(store_path: Path, tmp_path: Path):
    ids = _ids_by_content(store_path)
    m2, m3, m5, m6, m8 = (
        ids[k] for k in ("Hi GPT-5.2 a", "Agreed Opus ", "Opus 4.5 and", "Resting here", "Game idea: g")
    )
    lines = [
        {"type": "meta", "sweep_id": "sw-test", "rubric": "Does the agent propose a plan?"},
        {"type": "verdict", "event_id": m2, "verdict": "yes", "i": 0},
        {"type": "verdict", "event_id": m3, "verdict": "yes", "i": 1},
        {"type": "verdict", "event_id": m3, "verdict": "no", "i": 1},  # a rerun: the last line wins
        {"type": "verdict", "event_id": m5, "verdict": "yes", "i": 2},
        {"type": "verdict", "event_id": m6, "verdict": "unclear", "i": 3},
        {"type": "verdict", "event_id": m8, "verdict": None, "error": "boom", "i": 4},  # failed call
        {"type": "verdict", "event_id": m2.rsplit(":", 1)[0] + ":nope", "verdict": "yes", "i": 5},
        {"type": "summary", "n": 6},
    ]
    path = tmp_path / "sw-test.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    sw = read_sweep(path)
    assert sw["errors"] == 1 and sw["bad_lines"] == 1 and sw["verdicts"][m3] == "no"
    with db.connect(store_path) as s:
        r = metric_series(s, metric="sweep", sweep_path=path, by="agent", day_one=False)
    assert r["kind"] == "rate" and r["verdict"] == "yes"
    g = {x["name"]: (x["total_num"], x["total_den"]) for x in r["groups"]}
    assert g == {"Claude Opus 4.5": (1, 2), "GPT-5.2": (1, 2)}  # 'unclear' stays in the denominator
    assert r["excluded"] == {
        "sweep_records": 5,
        "not_in_store_or_filters": 1,
        "human_records": 0,
        "failed_calls": 1,
    }
    assert any("Does the agent propose a plan?" in n for n in r["notes"])


def test_bad_input(store_path: Path, tmp_path: Path):
    with db.connect(store_path) as s:
        with pytest.raises(ToolInputError, match="metric must be"):
            metric_series(s, metric="vibes")
        with pytest.raises(ToolInputError, match="by must be"):
            metric_series(s, by="mood")
        with pytest.raises(ToolInputError, match="needs sweep_path"):
            metric_series(s, metric="sweep")
        with pytest.raises(ToolInputError, match="only used"):
            metric_series(s, sweep_path=tmp_path / "x.jsonl")
        with pytest.raises(ToolInputError, match="not found"):
            metric_series(s, metric="sweep", sweep_path=tmp_path / "missing.jsonl")
        with pytest.raises(ToolInputError, match="window"):
            metric_series(s, window=0)
        with pytest.raises(ToolInputError, match="Unknown channel"):
            metric_series(s, channel="nope")
