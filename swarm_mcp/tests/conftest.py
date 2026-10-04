"""Shared fixtures. All data is synthetic and generated in tmp_path at test time."""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from swarm_mcp.config import Config
from swarm_mcp.server import build_server

A_OPUS = "a0000000-0000-0000-0000-00000000opus"
A_GPT = "a0000000-0000-0000-0000-000000000gpt"
A_GEM = "a0000000-0000-0000-0000-00000000gemi"
A_O3 = "a0000000-0000-0000-0000-0000000000o3"
R_GEN = "r0000000-0000-0000-0000-0000000general"
R_REST = "r0000000-0000-0000-0000-00000000000rest"
HUMAN_ID = "u0000000-0000-0000-0000-0000000000hum"

LONG_TEXT = "Long report. " + ("lorem ipsum " * 300) + "THE END"


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _msg(i: int, ts: str, content: str, agent: str | None = None, room: str = R_GEN) -> dict[str, Any]:
    return {
        "id": f"m{i:04d}",
        "agent_speaker_id": agent,
        "user_speaker_id": None if agent else HUMAN_ID,
        "speaker_type": "agent" if agent else "user",
        "content": content,
        "room_id": room,
        "created_at": ts,
        "updated_at": ts,
        "has_been_approved": None,
    }


def make_village(root: Path) -> Path:
    """Write a tiny but realistic ai-village directory and return its path."""
    d = root / "ai-village"
    d.mkdir(parents=True)
    _write(
        d / "agents.jsonl.gz",
        [
            {
                "id": A_OPUS,
                "name": "Claude Opus 4.5",
                "model_string": "claude-opus-4-5-20251101",
                "created_at": "2026-01-01 10:00:00.000000",
                "is_participating": True,
            },
            {
                "id": A_GPT,
                "name": "GPT-5.2",
                "model_string": "gpt-5.2-2025-12-11",
                "created_at": "2026-01-02 10:00:00.000000",
                "is_participating": True,
            },
            {
                "id": A_GEM,
                "name": "Gemini 2.5 Pro",
                "model_string": "gemini-2.5-pro",
                "created_at": "2025-12-01 10:00:00.000000",
                "is_participating": True,
            },
            {
                "id": A_O3,
                "name": "o3",
                "model_string": "o3-2025-04-16",
                "created_at": "2025-11-01 10:00:00.000000",
                "is_participating": False,
            },
        ],
    )
    _write(
        d / "chat_rooms.jsonl.gz",
        [{"id": R_GEN, "name": "general", "deleted_at": None}, {"id": R_REST, "name": "rest", "deleted_at": None}],
    )
    _write(
        d / "village_goals.jsonl.gz",
        [  # deliberately out of order
            {
                "id": "g2",
                "goal": "Compete to build the best game!",
                "start_time": "2026-01-12 12:00:00",
                "end_time": "2026-01-19 12:00:00",
            },
            {
                "id": "g1",
                "goal": "Collaboratively choose a charity",
                "start_time": "2026-01-05 12:00:00",
                "end_time": "2026-01-12 12:00:00",
            },
            {
                "id": "g3",
                "goal": "Holiday: do whatever you like!",
                "start_time": "2026-01-19 12:00:00",
                "end_time": None,
            },
        ],
    )
    rows = [
        _msg(
            1,
            "2026-01-05 13:00:00.000001",
            "Hello agents, the charity goal starts now. Email help@agentvillage.org for help.",
        ),
        _msg(2, "2026-01-05 14:00:00.000000", "Hi GPT-5.2 and Gemini 2.5, let's pick a charity together.", A_OPUS),
        _msg(3, "2026-01-05 15:00:00.000000", "Agreed Opus 4.5! I genuinely like GiveDirectly.", A_GPT),
        _msg(
            4,
            "2026-01-06 09:00:00.000000",
            "Contact bob.smith@gmail.com or call +1 415 555 0134 / (415) 555-0199.",
            A_GEM,
        ),
        _msg(5, "2026-01-07 10:00:00.000000", "Opus 4.5 and Claude Opus 4.5 are the same agent; o3 left.", A_GPT),
        _msg(6, "2026-01-08 11:00:00.000000", "Resting here. GPT 5.2 is busy.", A_OPUS, R_REST),
        _msg(7, "2026-01-13 12:30:00.000000", LONG_TEXT, A_OPUS),
        _msg(8, "2026-01-14 08:00:00.000000", "Game idea: genuinely fun puzzles. Ask Gemini 2.5 Pro.", A_OPUS),
        _msg(
            9,
            "2026-01-15 08:00:00.000000",
            "GPT-5.2 here: version 1.234.5 shipped on 2026-01-15, 21,596 stations.",
            A_GPT,
        ),
        _msg(10, "2026-01-20 08:00:00.000000", "Holiday! Opus 4.5, GPT-5.2: enjoy.", A_GEM),
    ]
    rows += [
        _msg(100 + i, f"2026-01-21 {i // 60:02d}:{i % 60:02d}:00.000000", f"filler message {i}", A_GPT)
        for i in range(250)
    ]
    _write(d / "chat_messages.jsonl.gz", list(reversed(rows)))  # file order != time order
    _write(d / "events.jsonl.gz", EVENTS)
    (d / "SCHEMA.md").write_text("# schema\nsynthetic\n")
    return d


def _event(i: int, ts: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"e{i:04d}", "event_index": i, "created_at": ts, "updated_at": ts, "village_id": "v1", "data": data}


EVENTS = [
    _event(
        1,
        "2026-01-05 13:30:00.000000",
        {
            "actionType": "START_USING_COMPUTER",
            "agentId": A_OPUS,
            "computerUseSessionId": "s1",
            "sessionGoal": "Research charities",
        },
    ),
    _event(2, "2026-01-05 13:45:00.000000", {"actionType": "AGENT_TALK", "speakerId": A_OPUS, "content": "noise"}),
    _event(
        3,
        "2026-01-05 14:30:00.000000",
        {
            "actionType": "STOP_USING_COMPUTER",
            "agentId": A_OPUS,
            "summary": "Found GiveDirectly; mail bob.smith@gmail.com",
        },
    ),
    _event(
        4,
        "2026-01-06 10:00:00.000000",
        {
            "actionType": "CONSOLIDATE",
            "agentId": A_GPT,
            "computerUseSessionId": "s2",
            "nextSessionGoal": "Draft the vote",
        },
    ),
    _event(5, "2026-01-06 11:00:00.000000", {"actionType": "WAIT", "agentId": A_GPT}),
]


def build_store(data_dir: Path) -> Path:
    """Ingest the synthetic village under ``data_dir`` into ``data_dir/swarmscope.duckdb``."""
    from swarm_mcp.scope.ingest import ingest

    db = data_dir / "swarmscope.duckdb"
    ingest("ai_village", data_dir / "ai-village", db, progress=lambda _m: None)
    return db


@pytest.fixture
def raw_data_dir(tmp_path: Path) -> Path:
    """Synthetic raw dataset only (no store)."""
    make_village(tmp_path / "data")
    return tmp_path / "data"


@pytest.fixture
def data_dir(raw_data_dir: Path) -> Path:
    """Synthetic raw dataset plus the ingested store at the default location."""
    build_store(raw_data_dir)
    return raw_data_dir


@pytest.fixture
def store_path(data_dir: Path) -> Path:
    return data_dir / "swarmscope.duckdb"


def config_for(
    data_dir: Path,
    *,
    modules: str | list[str] | None = None,
    disable: str | list[str] | None = None,
    db: str | Path | None = None,
    findings: str | Path | None = None,
    sweeps: str | Path | None = None,
    llm: dict[str, Any] | None = None,
    settings: dict[str, dict[str, Any]] | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """A Config as if read from a swarm.toml next to ``data_dir`` (no real env leaks in)."""
    data: dict[str, Any] = {
        "data": {
            "dir": str(data_dir),
            "findings": str(findings or data_dir.parent / "findings"),
            "sweeps": str(sweeps or data_dir.parent / "sweeps"),
        },
        "server": {},
    }
    if db:
        data["data"]["db"] = str(db)
    if modules is not None:
        data["server"]["modules"] = modules
    if disable is not None:
        data["server"]["disable"] = disable
    if llm:
        data["llm"] = llm
    # the claude_code module reads ~/.swarm-live by default: point it at a missing file unless a test sets it
    data["modules"] = {"claude_code": {"db": str(data_dir / "no-recordings.db")}, **(settings or {})}
    return Config.from_dict(data, env=env or {}, root=data_dir.parent)


@pytest.fixture
def app(data_dir: Path):
    return build_server(config_for(data_dir))


def run(coro):
    return asyncio.run(coro)


def call(app, tool: str, **args: Any) -> dict[str, Any]:
    """Call a tool in-process; return its structured result or raise AssertionError with the error text."""
    res = run(app.call_tool(tool, args))
    if res.is_error:
        raise AssertionError(res.content[0].text)
    if res.structured_content is not None:
        return res.structured_content
    return json.loads(res.content[0].text)


def call_error(app, tool: str, **args: Any) -> str:
    """Call a tool expected to fail; return the error text."""
    try:
        res = run(app.call_tool(tool, args))
    except Exception as e:  # in-process call_tool raises ToolError instead of returning is_error
        return str(e)
    assert res.is_error, f"expected an error from {tool}"
    return res.content[0].text


@pytest.fixture
def fake_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create a throwaway module package; returns (package_name, add_module(name, source))."""
    pkg = f"fakemods_{uuid.uuid4().hex[:8]}"
    pdir = tmp_path / "pkgs" / pkg
    pdir.mkdir(parents=True)
    (pdir / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path / "pkgs"))

    def add(name: str, source: str) -> None:
        (pdir / f"{name}.py").write_text(source)

    yield pkg, add
    for mod in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
        del sys.modules[mod]
