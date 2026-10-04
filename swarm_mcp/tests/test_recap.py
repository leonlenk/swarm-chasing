"""analysis/recap.py: window and period recaps, agent arcs and notable moments (synthetic data only).

Synthetic rows go into a COPY of the fixture store. Evidence ids are derived from the fixture's
own ids, so the tests don't depend on the id scheme (currently "village:msg:<id>").
"""

from __future__ import annotations

import math
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from swarm_mcp.scope import db
from swarm_mcp.scope.analysis import recap
from swarm_mcp.toolkit import ToolInputError

AGENTS = {"opus": "Claude Opus 4.5", "gpt": "GPT-5.2", "gem": "Gemini 2.5 Pro"}


def _copy_with(store_path: Path, tmp_path: Path, rows: list[tuple[str, datetime, str]], name: str = "copy") -> Path:
    """A copy of the fixture store plus agent messages (who, ts, text) in #general."""
    out = tmp_path / f"{name}.duckdb"
    shutil.copy(store_path, out)
    con = duckdb.connect(str(out))
    try:
        ids = dict(con.execute("SELECT display_name, agent_id FROM agents").fetchall())
        some = con.execute("SELECT evidence_id FROM messages LIMIT 1").fetchone()[0]
        prefix = some[: some.rfind(":") + 1]
        for k, (who, ts, text) in enumerate(rows):
            con.execute(
                """INSERT INTO messages (evidence_id, source, channel, author_id, recipient_ids, ts, ts_quality, content, meta)
                   VALUES (?, 'village', 'general', ?, [], ?, 'exact', ?, '{}')""",
                [f"{prefix}syn{name}{k:04d}", ids[AGENTS[who]], ts, text],
            )
    finally:
        con.close()
    return out


@pytest.fixture(autouse=True)
def _fresh_cache():
    recap._NOVEL_CACHE.clear()
    yield
    recap._NOVEL_CACHE.clear()


# ---------------------------------------------------------------- window recap


def test_window_recap_activity_and_mentions(store_path: Path):
    with db.connect(store_path) as s:
        r = recap.window_recap(s, "2026-01-01", "2026-01-08", baseline=None)  # [Jan 1, Jan 9)
    w = r["window"]
    assert w["since"] == "2026-01-01T00:00:00Z" and w["until"] == "2026-01-09T00:00:00Z"
    # Day 1 is Jan 5 (Pacific); Jan 1 00:00 UTC is still Dec 31 in Pacific time, so day -4
    assert w["baseline_since"] is None and w["days"] == "Days -4–4"
    act = {a["name"]: a for a in r["activity"]}
    assert (act["Claude Opus 4.5"]["messages"], act["Claude Opus 4.5"]["actions"]) == (2, 2)
    assert (act["GPT-5.2"]["messages"], act["GPT-5.2"]["actions"]) == (2, 1)
    assert act["Gemini 2.5 Pro"]["messages"] == 1
    humans = [a for a in r["activity"] if a["kind"] == "human"]
    assert len(humans) == 1 and humans[0]["messages"] == 1
    assert r["activity"][0]["name"] == "Claude Opus 4.5"  # most messages + actions
    assert r["totals"]["agent_messages"] == 5 and r["totals"]["messages"] == 6

    # mentions: Opus->GPT x2, Opus->Gemini, GPT->Opus x2, GPT->o3 (o3 never acts, so outside the shown set)
    men = r["mentions"]
    names = men["names"]
    got = {(names[i], names[j]): n for i, j, n in men["rows"]}
    assert got == {
        ("Claude Opus 4.5", "GPT-5.2"): 2,
        ("Claude Opus 4.5", "Gemini 2.5 Pro"): 1,
        ("GPT-5.2", "Claude Opus 4.5"): 2,
    }
    assert men["total"] == 6 and men["outside_shown"] == 1
    assert not any(n.startswith("human:") for n in names)


def test_rising_term_hand_computed(store_path: Path, tmp_path: Path):
    t = datetime(2026, 1, 9, 10, 0)
    rows = [("gem", datetime(2026, 1, 6, 12, 0), "the zorblat")]  # once in the baseline week
    rows += [(who, t + timedelta(minutes=5 * k), "the zorblat") for k, who in enumerate(["opus", "gpt", "gem"] * 2)]
    copy = _copy_with(store_path, tmp_path, rows)
    with db.connect(copy) as s:
        r = recap.window_recap(s, "2026-01-08", "2026-01-14", min_term_msgs=4, min_term_agents=2)  # vs Jan 1-8
    assert r["window"]["baseline_since"] == "2026-01-01T00:00:00Z"
    top = r["rising_terms"][0]
    assert (top["term"], top["n"], top["n_before"], top["agents"]) == ("zorblat", 6, 1, 3)
    # window: 3 fixture + 6 synthetic agent messages; baseline: 4 fixture + 1 synthetic
    ni, nj, n, nb = 9, 5, 6, 1
    a0 = max(0.1 * (ni + nj), 1.0)
    a = a0 * (n + nb) / (ni + nj)
    delta = math.log((n + a) / (ni + a0 - n - a)) - math.log((nb + a) / (nj + a0 - nb - a))
    z = delta / math.sqrt(1 / (n + a) + 1 / (nb + a))
    assert top["score"] == round(z, 2) and top["log_odds"] == round(delta, 3)
    assert top["why"] == "6 of 9 msgs vs 1 of 5 before, 3 agents"
    assert top["first_id"].endswith("syncopy0001")  # the first use in the window, not the baseline one
    assert [x["term"] for x in r["rising_terms"]] == ["zorblat"]  # "the" is a stopword: no bigram


def test_bursts_are_runs_with_small_gaps(store_path: Path):
    with db.connect(store_path) as s:
        r = recap.window_recap(s, "2026-01-19", "2026-01-22", baseline=None)
        b = r["bursts"][0]
        assert (b["channel"], b["n"], b["agents"]) == ("general", 250, ["GPT-5.2"])
        assert (b["start"], b["end"]) == ("2026-01-21T00:00:00Z", "2026-01-21T04:09:00Z")
        assert len(b["ids"]) == 40 and b["ids"][0] == b["first_id"]
        assert b["day"] == 16  # 00:00 UTC on Jan 21 is still Jan 20 in Pacific time
        # one-minute spacing splits into single messages when the allowed gap is 30 s
        r = recap.window_recap(s, "2026-01-19", "2026-01-22", baseline=None, gap_minutes=0.5)
        assert max(x["n"] for x in r["bursts"]) == 1


def test_window_recap_input_checks(store_path: Path):
    with db.connect(store_path) as s:
        with pytest.raises(ToolInputError, match="after since"):
            recap.window_recap(s, "2026-01-08", "2026-01-01T00:00")
        with pytest.raises(ToolInputError, match="baseline"):
            recap.window_recap(s, "2026-01-08", "2026-01-10", baseline=("2026-01-07", "2026-01-09"))
        r = recap.window_recap(s, None, None, baseline=None)  # the whole store
        assert r["totals"]["agent_messages"] == 259


def test_period_recaps(store_path: Path):
    with db.connect(store_path) as s:
        out = recap.period_recaps(s)
    assert [p["label"] for p in out] == [
        "Collaboratively choose a charity",
        "Compete to build the best game!",
        "Holiday: do whatever you like!",
    ]
    assert [p["day_from"] for p in out] == [1, 8, 15]
    first, second, third = (p["recap"] for p in out)
    assert first["window"]["baseline_since"] is None
    assert (
        second["window"]["baseline_since"] == out[0]["start"] and second["window"]["baseline_until"] == out[1]["start"]
    )
    assert third["window"]["until"] >= "2026-01-21T04:09:00Z"  # open-ended: runs to the last message
    assert third["bursts"][0]["n"] == 250
    assert sum(p["recap"]["totals"]["agent_messages"] for p in out) == 259
    assert "notes" in out[0]


# ---------------------------------------------------------------- novel terms, agent arc, moments


def _term_rows() -> list[tuple[str, datetime, str]]:
    """'glimmerfax': coined by Gemini on Jan 9, then used twice each by Opus and GPT."""
    d = datetime(2026, 1, 9, 18, 0)
    return [
        ("gem", d, "the glimmerfax"),
        ("opus", d + timedelta(days=1), "the glimmerfax"),
        ("opus", d + timedelta(days=2), "the glimmerfax"),
        ("gpt", d + timedelta(days=3), "the glimmerfax"),
        ("gpt", d + timedelta(days=4), "the glimmerfax"),
    ]


def test_novel_terms_and_agent_arc(store_path: Path, tmp_path: Path):
    copy = _copy_with(store_path, tmp_path, _term_rows())
    with db.connect(copy) as s:
        nt = recap.novel_terms(s)
        assert nt["min_msgs"] == 2
        g = nt["terms"]["glimmerfax"]
        assert (g["n"], g["agents"], g["adopters"], g["adopters_fast"]) == (5, 3, 2, 2)
        assert {"claude", "opus", "gpt-5", "gemini"} <= recap._name_tokens(s)  # terms made only of these are left out

        gem = recap.agent_arc(s, "Gemini 2.5 Pro")
        coined = [t for t in gem["terms"] if t["role"] == "coined"]
        assert [t["term"] for t in coined] == ["glimmerfax"] and coined[0]["adopters"] == 2
        assert coined[0]["day"] == 5  # Jan 9, Pacific

        opus = recap.agent_arc(s, "Opus 4.5")  # an alias resolves too
    assert opus["name"] == "Claude Opus 4.5" and opus["lab"] == "Anthropic"
    adopted = [t for t in opus["terms"] if t["role"] == "adopted"]
    assert adopted[0]["term"] == "glimmerfax" and adopted[0]["coined_by"] == "Gemini 2.5 Pro" and adopted[0]["n"] == 2
    # bins: 7-day bins from Village day 1; their messages add up to the agent's messages (4 fixture + 2)
    assert opus["bins"][0]["day"] == 1 and sum(b["messages"] for b in opus["bins"]) == 6
    assert sum(b["actions"] for b in opus["bins"]) == 2
    # partners in the first goal: Opus named GPT twice and Gemini once; GPT named Opus twice
    p1 = opus["partners"][0]
    assert p1["label"] == "Collaboratively choose a charity"
    assert [(x["name"], x["n"]) for x in p1["top_mentioned"]] == [("GPT-5.2", 2), ("Gemini 2.5 Pro", 1)]
    assert p1["top_mentioned_by"][0] == {"agent_id": p1["top_mentioned_by"][0]["agent_id"], "name": "GPT-5.2", "n": 2}
    assert any("novel term" in n for n in opus["notes"])
    with db.connect(copy) as s, pytest.raises(ToolInputError):
        recap.agent_arc(s, "nobody-by-that-name")


def test_novel_terms_count_single_uses_and_skip_ordinary_words(store_path: Path, tmp_path: Path):
    """A coinage picked up once each by two other agents counts (it used to need two uses each and
    five messages); an ordinary word that first shows up late does not, and neither does a word a
    human used first."""
    d = datetime(2026, 1, 9, 18, 0)
    rows = [
        ("gem", d, "trying a quillmesh for the list"),
        ("opus", d + timedelta(days=1), "the quillmesh helps"),
        ("gpt", d + timedelta(days=2), "quillmesh, nice"),
        ("gem", d, "the signup sheet"),
        ("opus", d + timedelta(days=1), "signup done"),
        ("gpt", d + timedelta(days=2), "signups open"),
    ]
    copy = _copy_with(store_path, tmp_path, rows)
    con = duckdb.connect(str(copy))
    try:  # a human says "frobnitzer" before any agent does
        some = con.execute("SELECT evidence_id FROM messages LIMIT 1").fetchone()[0]
        con.execute(
            """INSERT INTO messages (evidence_id, source, channel, author_id, recipient_ids, ts, ts_quality, content, meta)
               VALUES (?, 'village', 'general', 'human:host', [], ?, 'exact', 'try the frobnitzer', '{}')""",
            [some[: some.rfind(":") + 1] + "synhuman", d - timedelta(hours=1)],
        )
    finally:
        con.close()
    more = [(who, d + timedelta(days=k + 1), "frobnitzer works") for k, who in enumerate(["gem", "opus", "gpt"])]
    copy2 = _copy_with(copy, tmp_path, more, name="copy2")
    with db.connect(copy2) as s:
        nt = recap.novel_terms(s)
        q = nt["terms"]["quillmesh"]
        assert (q["n"], q["agents"], q["adopters"], q["adopters_fast"]) == (3, 3, 2, 2)
        assert "signup" not in nt["terms"] and "signups" not in nt["terms"]  # common English
        assert "frobnitzer" not in nt["terms"]  # a human used it first
        mm = recap.moments_page(s, kinds=["first_use"], limit=10)["items"]
    assert [m["term"] for m in mm] == ["quillmesh"] and mm[0]["score"] == 2 and len(mm[0]["ids"]) == 3


def test_js_distance():
    assert recap._js_distance({"a": 3}, {"a": 1}) == 0.0
    assert recap._js_distance({"a": 1}, {"b": 1}) == pytest.approx(1.0)
    assert recap._js_distance({"a": 1, "b": 1}, {"a": 1}) == pytest.approx(0.55793, abs=1e-4)
    assert recap._js_distance({}, {"a": 1}) is None


def test_notable_moments_burst_and_first_use(store_path: Path, tmp_path: Path):
    base = datetime(2026, 1, 22, 18, 0)  # 10:00 Pacific, same date
    rows = _term_rows()
    rows += [("gpt", base + timedelta(days=k), f"note {k}") for k in range(14)]  # 1 a day for 14 days
    rows += [("gpt", base + timedelta(days=14, minutes=m), "busy") for m in range(30)]  # then 30 on Feb 5
    copy = _copy_with(store_path, tmp_path, rows)
    with db.connect(copy) as s:
        mm = recap.notable_moments(s, top=25)
        late = recap.notable_moments(s, since="2026-02-01", until="2026-02-28")
    bursts = [m for m in mm if m["kind"] == "burst" and m["agent"] == "GPT-5.2"]
    feb5 = next(m for m in bursts if m["day"] == 32)
    # prior 14 active days: 1 message each, sd 0 -> floored at max(sqrt(1), 1) = 1: z = (30 - 1) / 1
    assert feb5["score"] == 29.0
    assert feb5["why"] == "GPT-5.2: 30 msgs on Day 32 vs 1 ± 0 in the prior 14 active days (z = 29.0)"
    assert len(feb5["ids"]) == 30 and feb5["t"] == "2026-02-05T18:00:00Z"
    assert any(m["kind"] == "burst" and m["channel"] == "general" and m["agent"] is None for m in mm)
    fu = next(m for m in mm if m["kind"] == "first_use")
    assert (fu["term"], fu["agent"], fu["score"], fu["day"]) == ("glimmerfax", "Gemini 2.5 Pro", 2, 5)
    assert len(fu["ids"]) == 5  # every use, oldest first
    assert [m["t"] for m in mm] == sorted(m["t"] for m in mm)
    assert all({"kind", "t", "end", "agent", "channel", "score", "why", "ids", "rank"} <= set(m) for m in mm)
    assert [m["day"] for m in late] == [32, 32]  # only the Feb 5 bursts fall in the window


def test_notable_moments_silence(store_path: Path, tmp_path: Path):
    start = datetime(2026, 2, 10, 18, 0)
    rows = [("gpt", start + timedelta(days=k), f"keepalive {k}") for k in range(20)]  # every day Feb 10 - Mar 1
    rows += [("opus", start + timedelta(days=k, minutes=m), f"work {k}") for k in range(10) for m in range(5)]
    rows += [("opus", start + timedelta(days=14, minutes=m), "back") for m in range(5)]  # quiet Feb 20-23
    copy = _copy_with(store_path, tmp_path, rows)
    with db.connect(copy) as s:
        mm = recap.notable_moments(s, top=40)
    sil = [m for m in mm if m["kind"] == "silence"]
    assert len(sil) == 1 and sil[0]["agent"] == "Claude Opus 4.5"
    # prior 14 active days (Jan 13, 14, 15, 20, Feb 10-19): Opus sent 1 + 1 + 0 + 0 + 50 = 52
    assert sil[0]["score"] == round(4 * 52 / 14, 1)
    assert "no messages on 4 consecutive active days (Days 47–50)" in sil[0]["why"]
    assert sil[0]["end"] == "2026-02-24T18:00:00Z" and len(sil[0]["ids"]) == 5
