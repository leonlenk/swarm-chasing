"""Synthetic swarm benchmark: generator, files, plants, reference solver, scorer, CLI."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import call, config_for

from swarm_mcp.bench import generate
from swarm_mcp.bench.__main__ import main as bench_main
from swarm_mcp.bench._common import (
    TS_FMT,
    agent_eid,
    chat_eid,
    event_eid,
    name_pattern,
    read_jsonl_gz,
    skeleton,
)
from swarm_mcp.bench.reference import solve
from swarm_mcp.bench.score import score
from swarm_mcp.scope.adapters.ai_village import AiVillageAdapter
from swarm_mcp.server import build_server

# Field names of the real AI Village export (keys of the first row of each file).
EXPORT_KEYS = {
    "agents.jsonl.gz": {
        "created_at", "current_computer_use_session_id", "current_human_use_session_request_id", "current_room_id",
        "emoji", "goal", "id", "input_tokens_used", "is_participating", "is_paused_for_google_sign_in", "is_pending",
        "is_updating_memory", "last_seen_event_index", "model_string", "money", "name", "output_tokens_used",
        "paused_until", "paused_until_task_id", "status_message", "updated_at", "village_id",
    },
    "chat_messages.jsonl.gz": {
        "agent_speaker_id", "content", "created_at", "has_been_approved", "id", "room_id", "speaker_type",
        "updated_at", "user_speaker_id",
    },
    "chat_rooms.jsonl.gz": {
        "blacklisted_agent_names", "created_at", "deleted_at", "id", "last_nudger_run_at",
        "last_nudger_run_chat_message_id", "name", "updated_at", "village_id", "whitelisted_agent_names",
    },
    "village_goals.jsonl.gz": {"created_at", "end_time", "goal", "id", "start_time", "updated_at", "village_id"},
    "agent_memories.jsonl.gz": {"agent_id", "content", "created_at", "id", "updated_at"},
    "villages.jsonl.gz": {
        "active_agent_id", "created_at", "id", "is_chat_open", "name", "schedule", "slug", "turn_id", "updated_at",
        "village_goal",
    },
    "events.jsonl.gz": {"created_at", "data", "event_index", "id", "updated_at", "village_id"},
}  # fmt: skip
EVENT_DATA_KEYS = {
    "AGENT_TALK": {
        "actionType",
        "content",
        "cost",
        "inputTokens",
        "messageId",
        "output",
        "outputTokens",
        "roomId",
        "speakerId",
        "speakerType",
    },
    "START_USING_COMPUTER": {
        "actionType",
        "agentId",
        "computerUseSessionId",
        "cost",
        "inputTokens",
        "output",
        "outputTokens",
        "roomId",
        "sessionGoal",
        "shortDisplayedSessionGoal",
    },
    "STOP_USING_COMPUTER": {"actionType", "agentId", "cost", "inputTokens", "output", "outputTokens", "summary"},
    "CONSOLIDATE": {
        "actionType",
        "agentId",
        "computerUseSessionId",
        "cost",
        "inputTokens",
        "nextSessionGoal",
        "nextShortDisplayedSessionGoal",
        "output",
        "outputTokens",
        "roomId",
    },
    "USER_TALK": {"actionType", "speakerName", "messageId", "roomId", "content"},
}


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    out = tmp_path_factory.mktemp("bench") / "seed5"
    res = generate(out, 5)
    ds = Path(res["dataset_dir"])
    rows = {f.name: list(read_jsonl_gz(f)) for f in ds.glob("*.jsonl.gz")}
    return {"out": out, "ds": ds, "truth": res["truth"], "rows": rows}


def _digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _msgs(bench) -> dict[str, dict]:
    return {r["id"]: r for r in bench["rows"]["chat_messages.jsonl.gz"]}


def _native(eid: str) -> str:
    return eid.split(":", 2)[2]


# ---------------------------------------------------------------- generation
def test_generation_is_deterministic(tmp_path: Path):
    a, b, c = (generate(tmp_path / n, s, days=14, msgs_per_day=40) for n, s in (("a", 11), ("b", 11), ("c", 12)))
    da, db = _digest(tmp_path / "a"), _digest(tmp_path / "b")
    assert da == db and "truth.json" in da and "ai-village/events.jsonl.gz" in da
    assert a["truth"] == b["truth"]
    assert _digest(tmp_path / "c") != da and c["truth"] != a["truth"]


def test_generate_rejects_too_small(tmp_path: Path):
    with pytest.raises(ValueError, match="n_agents"):
        generate(tmp_path, 0, n_agents=8)
    with pytest.raises(ValueError, match="days"):
        generate(tmp_path, 0, days=5)


# ---------------------------------------------------------------- files
def test_files_parse_and_match_export_fields(bench):
    ds = bench["ds"]
    for name in ("README.md", "SCHEMA.md", "CHANGELOG.md", "manifest.json"):
        assert (ds / name).is_file()
    for name, keys in EXPORT_KEYS.items():
        rows = bench["rows"][name]
        assert rows, name
        assert all(set(r) == keys for r in rows), name
    manifest = json.loads((ds / "manifest.json").read_text())
    assert manifest["rowCounts"] == {k.removesuffix(".jsonl.gz"): len(v) for k, v in bench["rows"].items()}
    for rows in bench["rows"].values():
        for r in rows:
            for k in ("created_at", "updated_at", "start_time"):
                if r.get(k):
                    datetime.strptime(r[k], TS_FMT)
    events = bench["rows"]["events.jsonl.gz"]
    idx = [e["event_index"] for e in events]
    assert idx == sorted(idx) and len(set(idx)) == len(idx)
    assert [e["created_at"] for e in events] == sorted(e["created_at"] for e in events)
    kinds = Counter(e["data"]["actionType"] for e in events)
    assert set(kinds) == set(EVENT_DATA_KEYS) and all(kinds.values())
    for e in events:
        assert set(e["data"]) == EVENT_DATA_KEYS[e["data"]["actionType"]], e["data"]["actionType"]
    agents = {a["id"] for a in bench["rows"]["agents.jsonl.gz"]}
    rooms = {r["id"] for r in bench["rows"]["chat_rooms.jsonl.gz"]}
    for m in bench["rows"]["chat_messages.jsonl.gz"]:
        assert m["room_id"] in rooms
        assert (m["speaker_type"] == "agent") == (m["agent_speaker_id"] in agents)
        assert m["speaker_type"] == "agent" or m["user_speaker_id"]
    assert {a["agent_id"] for a in bench["rows"]["agent_memories.jsonl.gz"]} <= agents


def test_store_backed_modules_read_it(bench, tmp_path: Path):
    """Ingested into a SwarmScope store, the scope/village modules load it and find the planted first use."""
    from swarm_mcp.scope.ingest import ingest

    db = tmp_path / "bench.duckdb"
    ingest("ai_village", bench["ds"], db)
    app = build_server(config_for(bench["out"], db=db))
    assert app.swarm_registry.records["village"].status == "loaded"
    assert app.swarm_registry.records["scope"].status == "loaded"
    out = call(app, "scope_agents", source="village")
    assert out["total"] == bench["truth"]["params"]["n_agents"]
    term, t = next((k, v) for k, v in bench["truth"]["diffusion"].items() if v["kind"] == "copied")
    hits = call(app, "scope_search", query=term, limit=50)
    assert hits["total"] == len(t["all_use_event_ids"])
    assert [h["evidence_id"] for h in hits["results"]] == t["all_use_event_ids"]
    first = hits["results"][0]
    assert first["evidence_id"] == t["first"] and first["author_id"] == t["first_actor"]
    assert first["text"]["untrusted"] is True and term in first["text"]["content"].lower()
    got = call(app, "core_get", ids=t["first"])
    assert (got["evidence_id"], got["table"], got["source"]) == (t["first"], "messages", "village")
    assert got["author_id"] == t["first_actor"] and got["content"]["untrusted"] is True
    # every truth id is a store id: a batch core_get resolves them all
    ids = [*t["all_use_event_ids"], *bench["truth"]["coordinators"]["session_goal_event_ids"], t["first_actor"]]
    batch = call(app, "core_get", ids=ids)
    assert batch["errors"] in ({}, []) and batch["returned"] == batch["requested"] == len(ids)
    assert [r["evidence_id"] for r in batch["results"]] == ids


def test_scope_adapter_smoke_ingest(bench):
    adapter = AiVillageAdapter()
    info = adapter.inspect(bench["ds"])
    assert info["missing_required"] == []
    by_table: dict[str, list] = {}
    for table, row in adapter.load(bench["ds"]):
        by_table.setdefault(table, []).append(row)
    rows, truth = bench["rows"], bench["truth"]
    assert len(by_table["messages"]) == len(rows["chat_messages.jsonl.gz"])
    assert len(by_table["agents"]) == len(rows["agents.jsonl.gz"])
    assert not any(a["meta"].get("placeholder") for a in by_table["agents"])
    assert len(by_table["periods"]) == len(rows["village_goals.jsonl.gz"])
    n_actions = sum(
        e["data"]["actionType"] in EVENT_DATA_KEYS and e["data"]["actionType"] not in ("AGENT_TALK", "USER_TALK")
        for e in rows["events.jsonl.gz"]
    )
    assert len(by_table["actions"]) == n_actions
    # adapter ids are the truth ids
    msg_ids = {r["evidence_id"] for r in by_table["messages"]}
    assert all(e in msg_ids for t in truth["diffusion"].values() for e in t["all_use_event_ids"])
    action_ids = {r["evidence_id"] for r in by_table["actions"]}
    assert set(truth["coordinators"]["session_goal_event_ids"]) <= action_ids
    assert {r["agent_id"] for r in by_table["agents"]} == set(truth["agents"])
    # the named adopter is a recipient of the mention message
    t = next(v for v in truth["diffusion"].values() if v["kind"] == "copied")
    named = next(a for a in t["adopters"] if a["exposure"] == "mention")
    mention = next(r for r in by_table["messages"] if r["evidence_id"] == named["basis_event_id"])
    assert named["actor"] in mention["recipient_ids"]


def test_scope_full_ingest_when_available(bench, tmp_path: Path):
    ingest = pytest.importorskip("swarm_mcp.scope.ingest", reason="SwarmScope store not on this branch yet")
    res = ingest.ingest("ai_village", bench["ds"], tmp_path / "bench.duckdb")
    assert res["counts"]["messages"] == len(bench["rows"]["chat_messages.jsonl.gz"])


# ---------------------------------------------------------------- plants
def test_planted_copied_and_parallel_terms(bench):
    truth, msgs = bench["truth"], _msgs(bench)
    rooms = {r["id"]: r["name"] for r in bench["rows"]["chat_rooms.jsonl.gz"]}
    names = {a["id"]: a["name"] for a in bench["rows"]["agents.jsonl.gz"]}
    kinds = {v["kind"]: v for v in truth["diffusion"].values()}
    copied, parallel = kinds["copied"], kinds["parallel"]
    for t in (copied, parallel):
        pat = name_pattern(t["term"])
        uses = sorted((m for m in msgs.values() if pat.search(m["content"])), key=lambda m: m["created_at"])
        assert [chat_eid(m["id"]) for m in uses] == t["all_use_event_ids"]
        assert t["first"] == chat_eid(uses[0]["id"]) and t["first_actor"] == agent_eid(uses[0]["agent_speaker_id"])
        assert t["first"] == f"village:msg:{uses[0]['id']}"  # the store's message id
    # (a) research regulars copy it; one outsider adopts it after being named in a message containing it
    assert copied["first_room"] == "research"
    labels = Counter((a["label"], a["exposure"]) for a in copied["adopters"])
    assert labels[("likely_copier", "room")] >= 3 and labels[("likely_copier", "mention")] == 1
    named = next(a for a in copied["adopters"] if a["exposure"] == "mention")
    basis = msgs[_native(named["basis_event_id"])]
    assert copied["term"] in basis["content"].lower() and name_pattern(named["name"]).search(basis["content"])
    named_id = _native(named["actor"])
    assert not any(m["agent_speaker_id"] == named_id and rooms[m["room_id"]] == "research" for m in msgs.values())
    # (b) the parallel inventors share no room and no use of the term names the second one
    (q,) = parallel["adopters"]
    assert q["label"] == "possibly_independent" and q["basis_event_id"] is None
    p_id, q_id = _native(parallel["first_actor"]), _native(q["actor"])
    rooms_of = {a: {m["room_id"] for m in msgs.values() if m["agent_speaker_id"] == a} for a in (p_id, q_id)}
    assert rooms[msgs[_native(parallel["first"])]["room_id"]] not in {rooms[r] for r in rooms_of[q_id]}
    assert rooms[msgs[_native(q["first_event_id"])]["room_id"]] not in {rooms[r] for r in rooms_of[p_id]}
    assert not any(name_pattern(names[q_id]).search(msgs[_native(e)]["content"]) for e in parallel["all_use_event_ids"])


def test_planted_coordinator(bench):
    truth, msgs = bench["truth"], _msgs(bench)
    c = truth["coordinators"]
    (k,) = c["ranked"]
    events = {e["id"]: e for e in bench["rows"]["events.jsonl.gz"]}
    assert len(c["directive_event_ids"]) >= 6
    assert len(c["reply_event_ids"]) >= 2 * len(c["directive_event_ids"])
    for e in c["directive_event_ids"]:
        assert msgs[_native(e)]["agent_speaker_id"] == _native(k)
    for e in c["reply_event_ids"]:
        m = msgs[_native(e)]
        assert m["agent_speaker_id"] != _native(k) and name_pattern(c["name"]).search(m["content"])
    for e in c["session_goal_event_ids"]:
        d = events[_native(e)]["data"]
        assert d["actionType"] in ("START_USING_COMPUTER", "CONSOLIDATE")
        assert c["name"] in (d.get("sessionGoal") or d.get("nextSessionGoal"))
    decoy = _native(c["decoys"][0]["actor"])
    counts = Counter(m["agent_speaker_id"] for m in msgs.values() if m["agent_speaker_id"])
    assert counts.most_common(1)[0][0] == decoy  # the naive "most active" answer is wrong


def test_planted_gap_homoglyph_and_attribution(bench):
    truth, msgs = bench["truth"], _msgs(bench)
    integ = truth["integrity"]
    agents = {a["id"]: a for a in bench["rows"]["agents.jsonl.gz"]}
    # (d) gap: no chat or event activity inside the window; agent still participating
    (gap,) = integ["gaps"]
    who = _native(gap["actor"])
    start, end = (datetime.strptime(gap[k], TS_FMT) for k in ("start", "end"))
    assert end - start >= timedelta(days=gap["silent_days"] - 1) and agents[who]["is_participating"]
    times = [m["created_at"] for m in msgs.values() if m["agent_speaker_id"] == who]
    for e in bench["rows"]["events.jsonl.gz"]:
        if who in (e["data"].get("agentId"), e["data"].get("speakerId")):
            times.append(e["created_at"])
    assert not any(gap["start"] < t < gap["end"] for t in times)
    assert min(times) < gap["start"] and max(times) > gap["end"]
    # (e) homoglyph pair: same skeleton, different names, one has a Cyrillic letter; look-alike names itself
    (col,) = integ["name_collisions"]
    a, b = (agents[_native(x)]["name"] for x in col["agents"])
    assert a != b and skeleton(a) == skeleton(b) and a.isascii() != b.isascii()
    assert col["confusables"][0]["codepoint"].startswith("U+04")
    look_id = next(_native(x) for x in col["agents"] if not agents[_native(x)]["name"].isascii())
    for e in col["self_reference_event_ids"]:
        m = msgs[_native(e)]
        assert m["agent_speaker_id"] == look_id and agents[look_id]["name"] in m["content"]
    # (f) attribution: 3 chat rows without AGENT_TALK, 1 with a different speakerId
    talk = {
        e["data"]["messageId"]: e for e in bench["rows"]["events.jsonl.gz"] if e["data"]["actionType"] == "AGENT_TALK"
    }
    issues = Counter(x["issue"] for x in integ["attribution_issues"])
    assert issues == {"missing_event": 3, "speaker_mismatch": 1}
    for x in integ["attribution_issues"]:
        m = msgs[_native(x["event_id"])]
        if x["issue"] == "missing_event":
            assert m["id"] not in talk
        else:
            e = talk[m["id"]]
            assert event_eid(e["id"]) == x["talk_event_id"] and e["data"]["speakerId"] != m["agent_speaker_id"]
            assert agent_eid(e["data"]["speakerId"]) == x["event_speaker"]
    human = [m for m in msgs.values() if m["speaker_type"] == "user"]
    assert human and all(m["id"] not in talk for m in human)  # humans use USER_TALK, not issues


# ---------------------------------------------------------------- reference + scorer
@pytest.mark.parametrize("seed,size", [(0, "small"), (1, "small"), (2, "small"), (3, "medium")])
def test_reference_solver_scores_high(tmp_path: Path, seed: int, size: str):
    from swarm_mcp.bench import generate_size

    res = generate_size(tmp_path, seed, size)
    out = solve(res["dataset_dir"], list(res["truth"]["diffusion"]))
    report = score(res["truth"], out)
    for task, f1 in report["summary"]["f1"].items():
        assert f1 >= 0.95, (task, report["tasks"])
    assert report["summary"]["missing"] == []
    assert report["tasks"]["diffusion"]["basis_accuracy"] >= 0.95
    assert report["tasks"]["coordinators"]["ranks"] == {res["truth"]["coordinators"]["ranked"][0]: 1}


@pytest.fixture(scope="module")
def solved(bench):
    return solve(bench["ds"], list(bench["truth"]["diffusion"]))


def test_scorer_penalizes_wrong_labels(bench, solved):
    truth = bench["truth"]
    assert score(truth, solved)["summary"]["macro_f1"] == 1.0
    bad = copy.deepcopy(solved)
    n_adopters = 0
    for t in bad["diffusion"].values():
        for a in t["adopters"]:
            a["label"] = "possibly_independent" if a["label"] == "likely_copier" else "likely_copier"
            n_adopters += 1
    d = score(truth, bad)["tasks"]["diffusion"]
    assert d["fp"] == n_adopters and d["fn"] == n_adopters and d["f1"] < 0.5
    # one wrong label costs one FP and one FN
    one = copy.deepcopy(solved)
    next(iter(one["diffusion"].values()))["adopters"][0]["label"] = "possibly_independent"
    d1 = score(truth, one)["tasks"]["diffusion"]
    assert (d1["fp"], d1["fn"]) == (1, 1) and 0.5 < d1["f1"] < 1.0
    # wrong attribution issue type
    wrong = copy.deepcopy(solved)
    for x in wrong["integrity"]["attribution_issues"]:
        x["issue"] = "speaker_mismatch" if x["issue"] == "missing_event" else "missing_event"
    assert score(truth, wrong)["tasks"]["integrity"]["subtasks"]["attribution_issues"]["f1"] == 0.0


def test_scorer_other_penalties_and_leniency(bench, solved):
    truth = bench["truth"]
    decoy = truth["coordinators"]["decoys"][0]["actor"]
    swapped = copy.deepcopy(solved)
    swapped["coordinators"]["coordinators"].insert(0, {"actor": decoy, "score": 99.0, "example_event_ids": []})
    c = score(truth, swapped)["tasks"]["coordinators"]
    assert c["f1"] == 0.0 and c["mrr"] == 0.5
    shifted = copy.deepcopy(solved)
    for g in shifted["integrity"]["gaps"]:
        g["start"], g["end"] = "2031-01-01 00:00:00", "2031-01-02 00:00:00"
    gaps = score(truth, shifted)["tasks"]["integrity"]["subtasks"]["gaps"]
    assert (gaps["tp"], gaps["fp"], gaps["fn"]) == (0, 1, 1)
    extra = copy.deepcopy(solved)
    extra["integrity"]["name_collisions"].append({"agents": [decoy, truth["coordinators"]["ranked"][0]]})
    assert score(truth, extra)["tasks"]["integrity"]["subtasks"]["name_collisions"]["precision"] == 0.5
    # leniency: old 'chat' kind alias, display names / bare uuids as actors, talk event id for a mismatch
    assert "village:msg:" in json.dumps(solved) and "village:chat:" not in json.dumps(solved)
    alias = json.loads(json.dumps(solved).replace("village:msg:", "village:chat:"))
    names = {eid: a["name"] for eid, a in truth["agents"].items()}
    for t in alias["diffusion"].values():
        for a in t["adopters"]:
            a["actor"] = names[a["actor"]]
    for c in alias["coordinators"]["coordinators"]:
        c["actor"] = _native(c["actor"])
    for x in alias["integrity"]["attribution_issues"]:
        if x.get("talk_event_id"):
            x["event_id"] = x["talk_event_id"]
    assert score(truth, alias)["summary"]["macro_f1"] == 1.0
    # an old truth.json (v1, 'chat' ids) still scores new 'msg' outputs
    old_truth = json.loads(json.dumps(truth).replace("village:msg:", "village:chat:"))
    assert score(old_truth, solved)["summary"]["macro_f1"] == 1.0
    # missing tasks are reported
    assert set(score(truth, {"diffusion": solved["diffusion"]})["summary"]["missing"]) == {"coordinators", "integrity"}


def test_cli_generate_reference_score(tmp_path: Path, capsys):
    out = tmp_path / "run"
    assert bench_main(["generate", "--out", str(out), "--seed", "4", "--days", "14", "--msgs-per-day", "40"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["params"] == {"n_agents": 12, "days": 14, "msgs_per_day": 40} and len(summary["terms"]) == 2
    outputs = tmp_path / "outputs.json"
    assert bench_main(["reference", "--data", str(out), "--out", str(outputs)]) == 0
    capsys.readouterr()
    assert bench_main(["score", "--truth", str(out / "truth.json"), "--outputs", str(outputs)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["macro_f1"] == 1.0
    assert bench_main(["generate", "--out", str(out), "--n-agents", "3"]) == 2
