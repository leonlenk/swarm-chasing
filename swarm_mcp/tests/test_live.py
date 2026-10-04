"""swarm-live recording (hook ingest, collector, transcript import), the claude_code adapter into the
SwarmScope store, and the claude_code module (sync tool, plugin startup sync)."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import call, config_for

from swarm_mcp.cli import main as cli_main
from swarm_mcp.live.collector import App, make_handler
from swarm_mcp.live.ingest import Ingestor, import_path
from swarm_mcp.live.store import Store, is_recordings_db
from swarm_mcp.scope import db
from swarm_mcp.scope.ingest import ingest
from swarm_mcp.server import build_server

T0 = 1_767_600_000.0  # 2026-01-05T08:00:00Z
MAIN, SUB = "claude-code:agent:s1:main", "claude-code:agent:s1:a1"


def hooks() -> list[dict]:
    base = {"session_id": "s1", "cwd": "/work/calc"}
    sub = {**base, "agent_id": "a1", "agent_type": "tester"}
    return [
        {**base, "hook_event_name": "SessionStart"},
        {**base, "hook_event_name": "UserPromptSubmit", "prompt": "Fix calc.py and run the tests"},
        {**base, "hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_use_id": "t0",
         "tool_input": {"file_path": "/work/calc/calc.py", "old_string": "a-b", "new_string": "a+b"}},
        {**base, "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_use_id": "t0", "tool_response": "ok"},
        {**base, "hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": "t1",
         "tool_input": {"subagent_type": "tester", "description": "run tests", "prompt": "Run pytest"}},
        {**sub, "hook_event_name": "SubagentStart"},
        {**sub, "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "t2",
         "tool_input": {"command": "pytest -q"}},
        {**sub, "hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_use_id": "t2",
         "error": "2 failed, 14 passed"},
        {**sub, "hook_event_name": "SubagentStop", "last_assistant_message": "Two tests fail in test_calc.py."},
        {**base, "hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_use_id": "t1",
         "tool_response": {"content": "Two tests fail in test_calc.py."}},
        {**base, "hook_event_name": "Stop", "last_assistant_message": "All tests pass."},
    ]  # fmt: skip


@pytest.fixture
def recordings(tmp_path: Path) -> Path:
    path = tmp_path / "rec" / "swarm-live.db"
    ing = Ingestor(Store(path))
    for i, p in enumerate(hooks()):
        ing.handle_hook(p, T0 + i * 10)
    return path


@pytest.fixture
def cc_app(tmp_path: Path, recordings: Path):
    data = tmp_path / "data"
    ingest("claude_code", recordings, data / "swarmscope.duckdb", progress=lambda _m: None)
    return build_server(config_for(data))


# ----------------------------------------------------------------------------- recording


def test_ingest_links_subagent_to_its_agent_call(recordings: Path):
    st = Store(recordings)
    sub = st.one("SELECT * FROM agents WHERE key='s1:a1'")
    assert sub["parent_key"] == "s1:main" and sub["task"] == "run tests: Run pytest" and sub["status"] == "stopped"
    assert st.one("SELECT linked_agent FROM actions WHERE id='t1'")["linked_agent"] == "s1:a1"
    assert st.one("SELECT status FROM actions WHERE id='t2'")["status"] == "error"
    assert is_recordings_db(recordings)


def test_collector_records_posted_hooks(tmp_path: Path):
    app = App(tmp_path / "c.db")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert json.loads(urllib.request.urlopen(f"{url}/health").read())["app"] == "swarm-live"
        body = json.dumps({**hooks()[1], "_swarm_live_pid": 4242}).encode()
        urllib.request.urlopen(urllib.request.Request(f"{url}/hook", data=body, method="POST")).read()
        app.q.join()
    finally:
        srv.shutdown()
    assert app.live == {"s1": 4242}
    assert app.store.one("SELECT task FROM agents WHERE key='s1:main'")["task"] == "Fix calc.py and run the tests"


def test_import_transcript(tmp_path: Path):
    lines = [
        {"type": "user", "sessionId": "s9", "timestamp": "2026-01-05T09:00:00Z", "message": {"content": "Do it"}},
        {"type": "assistant", "sessionId": "s9", "timestamp": "2026-01-05T09:00:05Z",
         "message": {"content": [{"type": "text", "text": "On it."},
                                 {"type": "tool_use", "id": "u1", "name": "Read", "input": {"file_path": "a.py"}}]}},
        {"type": "user", "sessionId": "s9", "timestamp": "2026-01-05T09:00:06Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "content": "print(1)"}]}},
    ]  # fmt: skip
    f = tmp_path / "s9.jsonl"
    f.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    st = Store(tmp_path / "i.db")
    assert import_path(st, str(f)) == 1
    assert st.one("SELECT status, output FROM actions WHERE id='u1'") == {"status": "ok", "output": "print(1)"}
    assert st.one("SELECT task FROM agents WHERE key='s9:main'")["task"] == "Do it"
    assert st.one("SELECT text FROM messages")["text"] == "On it."


# ----------------------------------------------------------------------------- adapter -> store


def test_adapter_maps_sessions_onto_the_schema(tmp_path: Path, recordings: Path):
    store = tmp_path / "s.duckdb"
    res = ingest("claude_code", recordings, store, progress=lambda _m: None)
    assert res["source"] == "claude-code"
    assert res["counts"] == {
        "agents": 2,  # main + tester
        "messages": 4,  # prompt, delegation, 2 final answers
        "actions": 3,  # Edit, Agent, Bash
        "periods": 2,  # the session and the subagent run
        "artifacts": 1,  # calc.py
        "touches": 1,  # the Edit (a successful file tool)
    }
    with db.connect(store) as s:
        msgs = s.all("SELECT * FROM messages ORDER BY ts")
        acts = {a["kind"]: a for a in s.all("SELECT * FROM actions")}
        art = s.one("SELECT * FROM artifacts")
        touch = s.one("SELECT * FROM touches")
    prompt, delegation, sub_final, main_final = msgs
    assert prompt["msg_type"] == "prompt" and prompt["author_id"] == "human:user" and prompt["recipient_ids"] == [MAIN]
    assert delegation["author_id"] == MAIN and delegation["recipient_ids"] == [SUB]
    assert delegation["content"] == "Run pytest" and delegation["channel"] == "session-s1"
    assert sub_final["author_id"] == SUB and sub_final["recipient_ids"] == [MAIN]
    assert sub_final["reply_to"] == delegation["evidence_id"] == "claude-code:msg:task-t1"
    assert main_final["author_id"] == MAIN and main_final["recipient_ids"] == ["human:user"]
    assert (
        acts["Bash"]["run_id"] == "claude-code:period:s1:a1" and json.loads(acts["Bash"]["meta"])["status"] == "error"
    )
    assert json.loads(acts["Agent"]["meta"])["spawned"] == SUB and acts["Edit"]["content"].endswith("calc.py")
    assert art["artifact_id"] == "claude-code:artifact:/work/calc/calc.py" and art["name"] == "calc.py"
    assert touch["record_id"] == "claude-code:event:t0" and touch["op"] == "modify"


def test_store_tools_work_on_recorded_sessions(cc_app):
    hit = call(cc_app, "scope_search", query="Two tests fail", source="claude-code")["results"][0]
    rec = call(cc_app, "core_get", ids=hit["evidence_id"])
    assert rec["author_id"] == SUB and rec["table"] == "messages"
    assert call(cc_app, "core_get", ids=SUB)["table"] == "agents"
    edges = {(e["source"], e["target"]) for e in call(cc_app, "scope_graph", source="claude-code")["edges"]}
    assert {(MAIN, SUB), (SUB, MAIN)} <= edges  # delegation down, report back up


def test_cli_add_detects_recordings(tmp_path: Path, recordings: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.chdir(tmp_path)
    store = tmp_path / "cli.duckdb"
    with pytest.raises(SystemExit) as e:
        cli_main(["add", str(recordings), "--db", str(store)])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "detected a swarm-live recordings database" in out and "claude-code" in out


# ----------------------------------------------------------------------------- module


def test_sync_tool_loads_recordings(tmp_path: Path, recordings: Path):
    data = tmp_path / "data"
    cfg = config_for(data, settings={"claude_code": {"db": str(recordings)}})
    app = build_server(cfg)
    rec = app.swarm_registry.records["claude_code"]
    assert rec.status == "loaded" and rec.tools == ["claude_code_sync"]
    out = call(app, "claude_code_sync")
    assert out["source"] == "claude-code" and out["counts"]["actions"] == 3
    assert any("restart" in n for n in out["notes"])  # the store did not exist before this sync
    assert build_server(cfg).swarm_registry.records["scope"].status == "loaded"


def test_plugin_syncs_at_startup(tmp_path: Path, recordings: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin"))
    data = tmp_path / "data"
    app = build_server(config_for(data, settings={"claude_code": {"db": str(recordings)}}))
    assert app.swarm_registry.records["scope"].status == "loaded"  # claude_code created the store first
    hits = call(app, "scope_search", query="Run pytest", source="claude-code")
    assert hits["results"][0]["evidence_id"] == "claude-code:msg:task-t1"


def test_module_skipped_without_recordings(data_dir: Path):
    rec = build_server(config_for(data_dir)).swarm_registry.records["claude_code"]
    assert rec.status == "skipped" and "no swarm-live recordings" in rec.reasons[0]
