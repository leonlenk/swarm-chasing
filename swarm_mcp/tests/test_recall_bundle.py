"""``render recall``: the data RECALL reads (store payloads, live sessions, the index) and ``watch``."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from test_live import MAIN, SUB, T0, hooks

from swarm_mcp.cli import main as cli_main
from swarm_mcp.live.ingest import Ingestor
from swarm_mcp.live.store import Store
from swarm_mcp.scope.ingest import ingest
from swarm_mcp.scope.viz.recall_bundle import live_sessions, render_recall, watch
from swarm_mcp.toolkit import Scrubber


def record(path: Path, events: list[dict], t0: float = T0) -> Path:
    ing = Ingestor(Store(path))
    for i, p in enumerate(events):
        ing.handle_hook(p, t0 + i * 10)
    return path


def test_live_session_document(tmp_path: Path):
    rec = record(tmp_path / "rec.db", hooks())
    (doc,) = live_sessions(rec, scrub=Scrubber(), now=T0 + 10_000)
    assert doc["session"] == "s1" and doc["source"] == "claude-code" and doc["status"] == "stopped"
    assert not doc["synthetic"]
    names = {a["id"]: a["name"] for a in doc["agents"]}
    assert names == {MAIN: "Main agent · calc", SUB: "tester (subagent)"}
    assert {a["id"]: a["parent"] for a in doc["agents"]}[SUB] == MAIN
    # evidence ids are the store's, so RECALL records can be cited and re-read with core_get
    ids = {m["id"] for m in doc["messages"]} | {x["id"] for x in doc["actions"]}
    assert {"claude-code:msg:task-t1", "claude-code:event:t2"} <= ids
    bash = next(x for x in doc["actions"] if x["tool"] == "Bash")
    assert bash["command"] == "pytest -q" and bash["status"] == "error" and "2 failed" in bash["output"]
    assert bash["agent"] == SUB and bash["run"] == "claude-code:period:s1:a1"
    final = [m for m in doc["messages"] if m["type"] == "final"]
    assert [m["author"] for m in final] == [SUB, MAIN]


def test_live_session_is_running_until_it_stops(tmp_path: Path):
    rec = record(tmp_path / "rec.db", hooks()[:7])  # no Stop yet: the subagent is mid-run
    (doc,) = live_sessions(rec, scrub=Scrubber(), now=T0 + 80)
    assert doc["status"] == "running"
    (old,) = live_sessions(rec, scrub=Scrubber(), now=T0 + 10_000)  # nothing recorded for hours
    assert old["status"] == "stopped"


def test_live_text_is_masked(tmp_path: Path):
    events = hooks()
    events[1] = {**events[1], "prompt": "Email me at someone@example.com when the tests pass"}
    rec = record(tmp_path / "rec.db", events)
    (doc,) = live_sessions(rec, scrub=Scrubber(), now=T0)
    prompt = next(m for m in doc["messages"] if m["type"] == "prompt")
    assert "someone@example.com" not in prompt["text"] and "[email]" in prompt["text"]
    assert "someone@example.com" not in json.dumps(doc)


def test_render_recall_writes_store_payloads_live_sessions_and_index(tmp_path: Path):
    rec = record(tmp_path / "rec.db", hooks())
    db = tmp_path / "data" / "swarmscope.duckdb"
    ingest("claude_code", rec, db, progress=lambda _m: None)
    out = tmp_path / "recall-data"
    res = render_recall(db, out, recordings=rec, scrub=Scrubber())
    index = json.loads((out / "scope" / "index.json").read_text())
    assert index["v"] == 1 and index["store"] == "swarmscope.duckdb"
    (src,) = index["sources"]
    assert src["source"] == "claude-code" and src["counts"]["messages"] > 0
    explorer = json.loads((out / src["explorer"]["file"]).read_text())
    assert explorer["v"] == 2 and {"lanes", "dens", "x", "periods"} <= explorer.keys()  # the timeline payload
    (session,) = index["live"]["sessions"]
    assert session["id"] == "s1" and session["subagents"] == 1 and session["errors"] == 1
    assert json.loads((out / session["file"]).read_text())["session"] == "s1"
    assert res["live_sessions"] == 1


def test_skipped_parts_keep_their_previous_payloads(tmp_path: Path):
    rec = record(tmp_path / "rec.db", hooks())
    db = tmp_path / "data" / "swarmscope.duckdb"
    ingest("claude_code", rec, db, progress=lambda _m: None)
    out = tmp_path / "recall-data"
    render_recall(db, out, recordings=rec, scrub=Scrubber())
    render_recall(db, out, recordings=rec, scrub=Scrubber(), explorer=False, subtasks=False)  # a quick refresh
    (src,) = json.loads((out / "scope" / "index.json").read_text())["sources"]
    assert src["explorer"]["file"] == "scope/claude-code.explorer.json"


def test_render_recall_without_store_writes_live_only(tmp_path: Path):
    rec = record(tmp_path / "rec.db", hooks())
    out = tmp_path / "recall-data"
    render_recall(tmp_path / "missing.duckdb", out, recordings=rec, scrub=Scrubber())
    index = json.loads((out / "scope" / "index.json").read_text())
    assert index["store"] is None and index["sources"] == [] and len(index["live"]["sessions"]) == 1


def test_sessions_that_leave_the_window_are_removed(tmp_path: Path):
    rec = record(tmp_path / "rec.db", hooks())
    out = tmp_path / "recall-data"
    stale = out / "scope" / "live" / "gone.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    render_recall(tmp_path / "missing.duckdb", out, recordings=rec, scrub=Scrubber())
    assert not stale.exists() and (out / "scope" / "live" / "s1.json").exists()


def test_watch_picks_up_new_recordings(tmp_path: Path):
    rec = record(tmp_path / "rec.db", hooks()[:4])
    out = tmp_path / "recall-data"
    render_recall(tmp_path / "missing.duckdb", out, recordings=rec, scrub=Scrubber())
    done = threading.Event()
    t = threading.Thread(
        target=watch,
        args=(tmp_path / "missing.duckdb", out),
        kwargs={
            "recordings": rec,
            "scrub": Scrubber(),
            "interval": 0.05,
            "progress": lambda _m: None,
            "stop": done.is_set,
        },
    )
    t.start()
    try:
        ing = Ingestor(Store(rec))
        for i, p in enumerate(hooks()[4:], start=4):
            ing.handle_hook(p, T0 + i * 10)
        doc: dict = {}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            doc = json.loads((out / "scope" / "live" / "s1.json").read_text())
            if any(a["kind"] == "subagent" for a in doc["agents"]):
                break
            time.sleep(0.05)
        assert any(a["kind"] == "subagent" for a in doc["agents"])
    finally:
        done.set()
        t.join(timeout=5)


def test_cli_render_recall(tmp_path: Path, monkeypatch, capsys):
    rec = record(tmp_path / "rec.db", hooks())
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SWARM_DATA_DIR", str(data))
    out = tmp_path / "recall-data"
    with pytest.raises(SystemExit) as exit_:
        cli_main(["render", "recall", "--recordings", str(rec), "--out", str(out)])
    assert exit_.value.code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["live_sessions"] == 1 and (out / "scope" / "index.json").exists()
