"""scope_recap and scope_moments: the MCP tools over analysis/recap.py (synthetic data only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from conftest import A_GEM, A_GPT, A_OPUS, call, call_error, config_for

from swarm_mcp.bench.generate import generate
from swarm_mcp.server import build_server

TEXT_KEYS = {"content", "snippet", "label", "text", "term", "reason", "first_snippet"}
OPUS = f"village:agent:{A_OPUS}"
GPT = f"village:agent:{A_GPT}"
GEM = f"village:agent:{A_GEM}"


def assert_wrapped(obj: Any, path: str = "$") -> int:
    """Every dataset-text field must be an untrusted wrapper. Returns how many were checked."""
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


# ----------------------------------------------------------------------------- scope_recap


def test_recap_period_counts_match_the_store(app, store_path: Path):
    out = call(app, "scope_recap", period="1")  # goal 1: 2026-01-05 12:00 .. 2026-01-12 12:00
    assert out["period"]["index"] == 1 and out["period"]["kind"] == "village_goal"
    assert out["window"]["since"] == "2026-01-05T12:00:00Z" and out["window"]["until"] == "2026-01-12T12:00:00Z"
    assert out["window"]["days"] == "Days 1–8"  # village goals give Village days (day 1 = goal 1's date)
    con = duckdb.connect(str(store_path), read_only=True)
    try:
        w = "ts >= TIMESTAMP '2026-01-05 12:00:00' AND ts < TIMESTAMP '2026-01-12 12:00:00'"
        per_author = dict(con.execute(f"SELECT author_id, count(*) FROM messages WHERE {w} GROUP BY 1").fetchall())
        actions = dict(con.execute(f"SELECT agent_id, count(*) FROM actions WHERE {w} GROUP BY 1").fetchall())
        agents = {r[0] for r in con.execute("SELECT agent_id FROM agents").fetchall()}
    finally:
        con.close()
    got = {a["agent_id"]: (a["messages"], a["actions"]) for a in out["agents"]}
    want = {a: (per_author.get(a, 0), actions.get(a, 0)) for a in (set(per_author) | set(actions)) & agents}
    assert got == want
    humans = sum(n for a, n in per_author.items() if a.startswith("human:"))
    assert out["totals"]["humans"]["messages"] == humans == 1
    assert out["totals"]["messages"] == sum(per_author.values())
    assert out["totals"]["agents_active"] == len(want)
    # GPT-5.2's message names Opus 4.5 (m0003); Opus names GPT-5.2 and Gemini (m0002)
    pairs = {(p["from_id"], p["to_id"]): p["messages"] for p in out["pairs"]}
    assert pairs[(GPT, OPUS)] >= 1 and pairs[(OPUS, GPT)] >= 1
    assert assert_wrapped(out) >= 1  # the period label at least
    for t in out["threads"]:
        assert t["evidence_ids"] and t["first_snippet"]["untrusted"] is True


def test_recap_threads_list_humans_apart_from_agents(app):
    out = call(app, "scope_recap", period="1", top=50)
    threads = out["threads"]
    assert threads and all("humans" in t and "external" in t for t in threads)
    with_human = [t for t in threads if t["humans"]]
    assert with_human, threads  # goal 1 has one human message
    for t in threads:
        names = [x["content"] if isinstance(x, dict) else x for x in t["agents"]]
        assert not any(n.startswith("human:") for n in names), t["agents"]
    assert all(
        (h["content"] if isinstance(h, dict) else h).startswith("human:") for t in with_human for h in t["humans"]
    )


def test_recap_window_masks_snippets_and_rises(app):
    out = call(app, "scope_recap", since="2026-01-20", until="2026-01-22", top=5)
    assert out["period"] is None
    # a bare `until` date includes that day: [01-20, 01-23); the baseline is the previous 3 days
    assert out["window"]["until"] == "2026-01-23T00:00:00Z"
    assert (
        out["window"]["baseline_since"] == "2026-01-17T00:00:00Z"
        and out["window"]["baseline_until"] == "2026-01-20T00:00:00Z"
    )
    # the 250 filler messages on 2026-01-21 form the busiest thread; "filler" is a rising term
    assert out["threads"][0]["messages"] >= 200 and out["threads"][0]["channel"] == "general"
    # the filler is one agent's, and a rising term needs >= 2 agents, so nothing qualifies here
    assert all(t["agents"] >= 2 for t in out["rising_terms"])
    assert len(out["agents"]) <= 5 and len(out["rising_terms"]) <= 5
    assert_wrapped(out)
    # the email in m0004 never leaves unmasked
    out = call(app, "scope_recap", since="2026-01-06", until="2026-01-07", max_chars=200)
    assert "bob.smith@gmail.com" not in str(out)


def test_recap_errors(app):
    assert "either period or since/until" in call_error(app, "scope_recap", period="1", since="2026-01-05")
    assert "pass period" in call_error(app, "scope_recap")
    assert "out of range" in call_error(app, "scope_recap", period="99")
    assert "Unknown source" in call_error(app, "scope_recap", since="2026-01-05", source="nope")
    assert "must be before" in call_error(app, "scope_recap", since="2026-01-10", until="2026-01-05")
    assert "Unknown channel" in call_error(app, "scope_recap", since="2026-01-05", channel="nope")
    err = call_error(app, "scope_recap", since="2026-01-05", max_chars=5)  # PR #5's range: 20..20000
    assert "max_chars" in err


# ----------------------------------------------------------------------------- scope_moments


@pytest.fixture(scope="module")
def bench_app(tmp_path_factory):
    from swarm_mcp.scope.ingest import ingest

    out = tmp_path_factory.mktemp("moments") / "seed1"
    res = generate(out, 1)
    db = out / "bench.duckdb"
    ingest("ai_village", Path(res["dataset_dir"]), db, progress=lambda _m: None)
    return build_server(config_for(out, db=db)), res["truth"]


def test_moments_surface_the_planted_silence(bench_app):
    app, truth = bench_app
    gap = truth["integrity"]["gaps"][0]  # a planted agent that goes quiet mid-run, then returns
    out = call(app, "scope_moments", kinds=["silence"], limit=10)
    hits = [m for m in out["moments"] if m["agent_id"] == gap["actor"]]
    assert hits, out["moments"]
    m = hits[0]
    assert m["kind"] == "silence" and m["score_kind"] == "expected_missing_messages" and m["score"] > 0
    assert m["day"] >= 1 and m["evidence_ids"] and all(":msg:" in e for e in m["evidence_ids"])
    assert "consecutive active days" in m["reason"]["content"]
    # the silence sits between the planted start and end
    assert gap["start"][:10] <= m["time"][:10] <= gap["end"][:10]


def test_moments_paging_kinds_and_wrapping(bench_app):
    app, _ = bench_app
    first = call(app, "scope_moments", limit=2)
    assert first["returned"] == 2 and first["offset"] == 0
    assert first["total"] == sum(first["by_kind"].values()) and first["total"] > 2
    assert first["has_more"] and first["next_offset"] == 2
    second = call(app, "scope_moments", limit=2, offset=2)
    pos = [m["position"] for m in first["moments"] + second["moments"]]
    assert pos == list(range(1, len(pos) + 1)) and len(pos) >= 3
    # ranking interleaves kinds: the best of each kind before any kind's second best
    kinds_seen = [(m["kind"], m["rank_in_kind"]) for m in first["moments"] + second["moments"]]
    ranks = [r for _, r in kinds_seen]
    assert ranks == sorted(ranks)
    every = call(app, "scope_moments", limit=100)
    assert every["returned"] == every["total"] and not every["has_more"]
    assert assert_wrapped(every) >= every["returned"]  # every reason is wrapped
    only = call(app, "scope_moments", kinds=["first_use"], limit=5)
    assert set(only["by_kind"]) == {"first_use"} and all(m["kind"] == "first_use" for m in only["moments"])
    assert all(m["term"]["untrusted"] is True for m in only["moments"])
    past = call(app, "scope_moments", limit=5, offset=every["total"] + 10)
    assert past["returned"] == 0 and "past the last moment" in " ".join(past["notes"])


def test_moments_first_use_surfaces_the_planted_terms(bench_app):
    """Both planted coinages surface (one copied by four agents with mostly single uses, one used by
    just two agents), the copied one first, and no ordinary word does."""
    app, truth = bench_app
    planted = truth["diffusion"]
    out = call(app, "scope_moments", kinds=["first_use"], limit=20)
    got = [m["term"]["content"] for m in out["moments"]]
    assert sorted(got) == sorted(planted), got
    copied = next(t for t, d in planted.items() if d["kind"] == "copied")
    top = out["moments"][0]
    assert got[0] == copied and top["score"] == len(planted[copied]["adopters"])
    assert top["agent_id"] == planted[copied]["first_actor"] and top["evidence_ids"][0] == planted[copied]["first"]
    assert top["evidence_ids"] == planted[copied]["all_use_event_ids"]


def test_moments_window_and_errors(bench_app):
    app, truth = bench_app
    gap = truth["integrity"]["gaps"][0]
    before = call(app, "scope_moments", kinds=["silence"], until=gap["start"][:10])
    assert all(m["agent_id"] != gap["actor"] for m in before["moments"])
    assert "must be before" in call_error(app, "scope_moments", since="2031-03-10", until="2031-03-05")
    assert "Unknown source" in call_error(app, "scope_moments", source="nope")
    assert "kinds" in call_error(app, "scope_moments", kinds=["bursts"])
    assert "limit" in call_error(app, "scope_moments", limit=101)


def test_recap_rising_terms_on_a_bench_goal(bench_app):
    app, _ = bench_app
    out = call(app, "scope_recap", period="3", top=8)
    assert out["window"]["days"] and out["window"]["baseline_since"] < out["window"]["since"]
    terms = out["rising_terms"]
    assert terms and len(terms) <= 8
    for t in terms:
        assert t["messages"] >= 4 and t["agents"] >= 2 and t["score"] > 0 and t["log_odds"] > 0
        assert t["term"]["untrusted"] is True and ":msg:" in t["first_evidence_id"]
    scores = [t["score"] for t in terms]
    assert scores == sorted(scores, reverse=True)
    assert out["agents"] and all(a["messages"] + a["actions"] > 0 for a in out["agents"])
    assert_wrapped(out)
