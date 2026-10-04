"""The swarm-mcp command line: info, add (AI Village and mapped datasets), export. All data is synthetic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_village
from setup_datasets import make_nested_jsonl, make_sqlite_board
from test_mapped_ingest import BOARD_SPEC

from swarm_mcp.cli import main
from swarm_mcp.scope import db

EMAIL = "bob.smith@gmail.com"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with a swarm.toml; the cwd, so Config.load() finds it."""
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "swarm.toml").write_text('[data]\ndir = "data"\nfindings = "findings"\nsweeps = "sweeps"\n')
    monkeypatch.chdir(root)
    for var in ("SWARM_DATA_DIR", "ANTHROPIC_API_KEY", "SWARM_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return root


def cli(*args: str) -> int:
    with pytest.raises(SystemExit) as e:
        main(list(args))
    return e.value.code


def counts(store: Path, source: str) -> dict[str, int]:
    with db.connect(store) as s:
        return {t: s.scalar(f"SELECT count(*) FROM {t} WHERE source = ?", [source]) for t in ("messages", "agents")}


def test_add_detects_ai_village_and_is_idempotent(project: Path, capsys):
    make_village(project / "data")
    store = project / "data" / "swarmscope.duckdb"
    assert cli("add", "data/ai-village", "--dry-run") == 0
    assert "nothing ingested" in capsys.readouterr().out and not store.exists()

    assert cli("add", "data/ai-village") == 0
    out = capsys.readouterr().out
    assert "detected the AI Village layout" in out and "ingested source 'village'" in out
    assert "260 messages" in out and "swarm-mcp info" in out
    assert counts(store, "village") == {"messages": 260, "agents": 4}
    assert cli("add", "data") == 0  # the folder that holds ai-village/ works too, and re-adding replaces
    assert counts(store, "village") == {"messages": 260, "agents": 4}
    assert cli("add", "data/ai-village", "--name", "other") == 2
    assert "always uses the source name 'village'" in capsys.readouterr().err
    assert cli("add", "data/nope") == 2


def test_info_reports_and_fails_on_bad_findings(project: Path, capsys):
    make_village(project / "data")
    assert cli("add", "data/ai-village") == 0
    capsys.readouterr()
    assert cli("info") == 0
    out = capsys.readouterr().out
    assert "Sources" in out and "village" in out and "260 messages" in out and "Findings  [ok]" in out
    assert "core_get, core_info" in out and "scope_search" in out
    assert cli("info", "--json") == 0
    info = json.loads(capsys.readouterr().out)
    assert info["config"]["config_file"] == str(project / "swarm.toml") and info["findings"]["ok"] is True

    fdir = project / "findings"
    fdir.mkdir()
    bad = {"finding_id": "f-1", "created_at": "2026-10-03T12:00:00Z", "claim": "c",
           "evidence_ids": ["village:chat:does-not-exist"], "status": "open"}  # fmt: skip
    (fdir / "findings.jsonl").write_text(json.dumps(bad) + "\n")
    assert cli("info") == 1
    out = capsys.readouterr().out
    assert "BAD EVIDENCE" in out and "village:chat:does-not-exist" in out


def test_add_with_a_mapping(project: Path, capsys):
    make_sqlite_board(project / "data" / "board")
    mapping = project / "board.json"
    mapping.write_text(json.dumps(BOARD_SPEC))
    store = project / "data" / "swarmscope.duckdb"

    assert cli("add", "data/board", "--mapping", str(mapping), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "dry run" in out and not store.exists()

    assert cli("add", "data/board", "--mapping", str(mapping)) == 0
    out = capsys.readouterr().out
    assert "ingested source 'board'" in out and "240 messages, 12 actions" in out
    assert counts(store, "board")["messages"] == 240
    assert cli("add", "data/board", "--mapping", str(mapping), "--name", "forum") == 2

    broken = dict(BOARD_SPEC, records=[dict(BOARD_SPEC["records"][0], text="no_such_field")])
    mapping.write_text(json.dumps(broken))
    assert cli("add", "data/board", "--mapping", str(mapping)) == 1
    assert "does not pass the check, so nothing was ingested" in capsys.readouterr().out


def test_add_drafts_a_mapping(project: Path, capsys):
    make_nested_jsonl(project / "data" / "crew")
    assert cli("add", "data/crew") == 0  # heuristic draft (--agent none) passes on this layout
    out = capsys.readouterr().out
    assert "drafting a mapping (--agent none)" in out and "ingested source 'crew'" in out
    assert (project / "mappings" / "crew.json").exists()
    assert counts(project / "data" / "swarmscope.duckdb", "crew")["messages"] == 300

    assert cli("add", "data/crew", "--name", "crew2", "--agent", "claude-code") == 0
    out = capsys.readouterr().out
    assert "/swarm-setup crew2" in out and (project / "mappings" / "crew2.task.md").exists()
    assert "swarm-mcp add" in (project / "mappings" / "crew2.task.md").read_text()
    assert counts(project / "data" / "swarmscope.duckdb", "crew2")["messages"] == 0  # nothing ingested

    assert cli("add", "data/crew", "--agent", "api") == 2
    assert "--agent none" in capsys.readouterr().err
    assert cli("add", "data/crew", "--name", "Bad Name") == 2


def test_export_from_the_store(project: Path, capsys):
    make_village(project / "data")
    assert cli("add", "data/ai-village") == 0
    capsys.readouterr()
    args = ("export", "--source", "village", "--channel", "general", "--since", "2026-01-05", "--until", "2026-01-06")
    assert cli(*args, "--out", "data/export") == 0
    out = capsys.readouterr().out
    assert "exported 4 records" in out and "village: 4 chat" in out and "check passed" in out
    text = (project / "data" / "export" / "events.jsonl").read_text()
    assert EMAIL not in text and "[email]" in text and "help@agentvillage.org" in text

    assert cli(*args, "--out", "data/export", "--json", "--with-agents") == 0
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] is True and res["records"]["events"] == 4 and res["records"]["agents"] == 4
    assert res["redaction"]["counts"]["email"] >= 1 and res["check"]["findings"] == []
    assert cli(*args, "--out", "data/export2", "--no-check") == 0
    assert "check skipped" in capsys.readouterr().out
    assert cli("export", "--out", "data/x", "--source", "nope") == 2


def test_removed_commands_are_gone(project: Path, capsys):
    for old in (["ingest", "ai_village", "data"], ["check-findings"], ["--list-modules"]):
        assert cli(*old) == 2  # falls through to the server's parser, which rejects it
