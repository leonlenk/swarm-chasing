"""live module: hook ingest into the store, the collector endpoint, transcript import, and the MCP tools/event ids."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import call, call_error, config_for

from swarm_mcp.modules.live.collector import App, make_handler
from swarm_mcp.modules.live.ingest import Ingestor, import_path
from swarm_mcp.modules.live.store import Store
from swarm_mcp.server import build_server

T0 = 1_767_600_000.0  # 2026-01-05T08:00:00Z


def hooks() -> list[dict]:
    base = {"session_id": "s1", "cwd": "/work/calc"}
    return [
        {**base, "hook_event_name": "SessionStart"},
        {**base, "hook_event_name": "UserPromptSubmit", "prompt": "Fix calc.py and run the tests"},
        {
            **base,
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_use_id": "t1",
            "tool_input": {"subagent_type": "tester", "description": "run tests", "prompt": "Run pytest"},
        },
        {**base, "hook_event_name": "SubagentStart", "agent_id": "a1", "agent_type": "tester"},
        {
            **base,
            "hook_event_name": "PreToolUse",
            "agent_id": "a1",
            "agent_type": "tester",
            "tool_name": "Bash",
            "tool_use_id": "t2",
            "tool_input": {"command": "pytest -q"},
        },
        {
            **base,
            "hook_event_name": "PostToolUseFailure",
            "agent_id": "a1",
            "agent_type": "tester",
            "tool_name": "Bash",
            "tool_use_id": "t2",
            "error": "2 failed, 14 passed; contact bob@gmail.com",
        },
        {
            **base,
            "hook_event_name": "SubagentStop",
            "agent_id": "a1",
            "agent_type": "tester",
            "last_assistant_message": "Two tests fail in test_calc.py.",
        },
        {
            **base,
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "tool_use_id": "t1",
            "tool_response": {"content": "Two tests fail in test_calc.py."},
        },
        {**base, "hook_event_name": "Stop", "last_assistant_message": "All tests pass."},
    ]


@pytest.fixture
def live_db(tmp_path: Path) -> Path:
    db = tmp_path / "live.db"
    ing = Ingestor(Store(db))
    for i, p in enumerate(hooks()):
        ing.handle_hook(p, T0 + i * 10)
    return db


@pytest.fixture
def live_app(tmp_path: Path, live_db: Path):
    return build_server(config_for(tmp_path / "data", SWARM_LIVE_DB=str(live_db), SWARM_MCP_MODULES="live"))


def test_ingest_links_subagent_to_its_agent_call(live_db: Path):
    st = Store(live_db)
    sub = st.one("SELECT * FROM agents WHERE key='s1:a1'")
    assert sub["parent_key"] == "s1:main" and sub["task"] == "run tests: Run pytest" and sub["status"] == "stopped"
    assert st.one("SELECT linked_agent FROM actions WHERE id='t1'")["linked_agent"] == "s1:a1"
    assert st.one("SELECT status FROM actions WHERE id='t2'")["status"] == "error"


def test_sessions_and_agents(live_app):
    out = call(live_app, "live_sessions")
    (s,) = out["sessions"]
    assert s["main_agent"] == "live:agent:s1:main" and s["task"] == "Fix calc.py and run the tests"
    assert (s["agents"], s["actions"], s["failed_actions"], s["running"]) == (2, 2, 1, False)
    assert s["started"] == "2026-01-05T08:00:00Z"
    tree = call(live_app, "live_agents", session="live:agent:s1:a1")
    by_id = {a["event_id"]: a for a in tree["agents"]}
    sub = by_id["live:agent:s1:a1"]
    assert sub["parent"] == "live:agent:s1:main" and sub["depth"] == 1 and sub["actor_type"] == "subagent:tester"
    assert sub["failed_actions"] == 1 and by_id["live:agent:s1:main"]["depth"] == 0
    assert "No session" in call_error(live_app, "live_agents", session="nope")


def test_timeline_filters_and_records(live_app):
    main_only = call(live_app, "live_timeline", agent="live:agent:s1:main")
    assert [e["kind"] for e in main_only["events"]] == ["prompt", "action", "message"]
    spawn = main_only["events"][1]
    assert spawn["event_id"] == "live:action:t1" and spawn["spawned"] == "live:agent:s1:a1"
    assert spawn["text"].startswith("Agent: run tests")
    tree = call(live_app, "live_timeline", agent="live:agent:s1:main", include_subagents=True, kinds=["action"])
    assert [e["event_id"] for e in tree["events"]] == ["live:action:t1", "live:action:t2"]
    failed = tree["events"][1]
    assert failed["status"] == "error" and failed["actor"].startswith("tester#a1") and "[email]" in failed["output"]
    page = call(live_app, "live_timeline", session="s1", limit=2)
    assert page["total_matches"] == 5 and page["has_more"] and page["next_offset"] == 2
    late = call(live_app, "live_timeline", since="2026-01-05T08:01:00Z")
    assert [e["kind"] for e in late["events"]] == ["message", "message"]


def test_event_ids_expand_through_core(tmp_path: Path, live_db: Path):
    app = build_server(config_for(tmp_path / "data", SWARM_LIVE_DB=str(live_db), SWARM_MCP_MODULES="core,live"))
    out = call(app, "core_get_event", event_id="live:action:t2")
    assert out["event"]["tool"] == "Bash" and out["before"] == []
    assert [e["event_id"] for e in out["after"]] == ["live:message:1"]
    assert out["after"][0]["channel"] == "final"
    agent = call(app, "core_get_event", event_id="live:agent:s1:main")
    assert agent["before"] == [] and [a["event_id"] for a in agent["after"]] == ["live:agent:s1:a1"]
    many = call(app, "core_get_events", event_ids=["live:prompt:2", "live:action:missing"])
    assert many["events"][0]["actor_type"] == "human" and len(many["errors"]) == 1


def test_module_skipped_without_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    app = build_server(config_for(tmp_path / "data", SWARM_LIVE_DB=str(tmp_path / "none.db")))
    (rec,) = [r for r in app.swarm_registry.skipped if r.name == "live"]
    assert "no swarm-live database" in rec.reasons[0]


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
        {
            "type": "assistant",
            "sessionId": "s9",
            "timestamp": "2026-01-05T09:00:05Z",
            "message": {
                "content": [
                    {"type": "text", "text": "On it."},
                    {"type": "tool_use", "id": "u1", "name": "Read", "input": {"file_path": "a.py"}},
                ]
            },
        },
        {
            "type": "user",
            "sessionId": "s9",
            "timestamp": "2026-01-05T09:00:06Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "content": "print(1)"}]},
        },
    ]
    f = tmp_path / "s9.jsonl"
    f.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    st = Store(tmp_path / "i.db")
    assert import_path(st, str(f)) == 1
    assert st.one("SELECT status, output FROM actions WHERE id='u1'") == {"status": "ok", "output": "print(1)"}
    assert st.one("SELECT task FROM agents WHERE key='s9:main'")["task"] == "Do it"
    assert st.one("SELECT text FROM messages")["text"] == "On it."
