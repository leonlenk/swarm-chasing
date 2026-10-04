"""Module selection and swarm.toml config parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import config_for

from swarm_mcp.config import Config, ConfigError, resolve_data_dir
from swarm_mcp.server import build_server


def _status(app) -> dict[str, str]:
    return {r.name: r.status for r in app.swarm_registry.records.values()}


# subtasks needs units (periods) in the store, which the synthetic fixtures do not have
ALL = {
    "core": "loaded",
    "findings": "loaded",
    "scope": "loaded",
    "village": "loaded",
    "subtasks": "skipped",
    "investigate": "loaded",  # needs no data
    "sweep": "loaded",  # needs no data
}


def test_default_loads_all(data_dir: Path):
    assert _status(build_server(config_for(data_dir))) == ALL


def test_modules_allowlist_keeps_core(data_dir: Path):
    app = build_server(config_for(data_dir, modules="village"))
    assert _status(app) == {
        **ALL,
        "findings": "skipped",
        "scope": "skipped",
        "investigate": "skipped",
        "sweep": "skipped",
    }
    app = build_server(config_for(data_dir, modules="core"))
    st = _status(app)
    assert st["village"] == "skipped"
    assert "not selected" in app.swarm_registry.records["village"].reasons[0]


def test_disable_wins_and_can_disable_core(data_dir: Path):
    app = build_server(config_for(data_dir, modules="village", disable="village, core"))
    assert set(_status(app).values()) == {"skipped"}


def test_store_paths(tmp_path: Path):
    cfg = Config.load({"SWARM_DATA_DIR": str(tmp_path)}, cwd=tmp_path)
    assert cfg.store_path == tmp_path / "swarmscope.duckdb" and cfg.max_text == 500
    assert cfg.findings_path == tmp_path / "findings" and cfg.sweeps_path == tmp_path / "sweeps"
    (tmp_path / "swarm.toml").write_text('[data]\ndir = "d"\ndb = "x.duckdb"\nfindings = "f"\nsweeps = "/abs/sw"\n')
    cfg = Config.load({}, cwd=tmp_path)
    assert cfg.config_file == tmp_path / "swarm.toml" and cfg.data_dir == tmp_path / "d"
    assert cfg.store_path == tmp_path / "x.duckdb" and cfg.findings_path == tmp_path / "f"
    assert cfg.sweeps_path == Path("/abs/sw")
    assert cfg.public()["db_path"] == str(tmp_path / "x.duckdb")
    # SWARM_DATA_DIR wins over [data] dir
    assert Config.load({"SWARM_DATA_DIR": str(tmp_path / "e")}, cwd=tmp_path).data_dir == tmp_path / "e"


def test_unknown_module_names_are_noted(data_dir: Path):
    app = build_server(config_for(data_dir, modules=["village", "nonsense"]))
    assert any("nonsense" in n for n in app.swarm_registry.notes)


def test_config_parsing(tmp_path: Path):
    (tmp_path / "swarm.toml").write_text(
        """
[server]
modules = [" Village ", "core"]
max_limit = 50
[privacy]
scrub = false
email_allowlist = ["example.org", "agentvillage.org"]
[llm]
model = "claude-haiku-4-5"
effort = "none"
fallbacks = false
concurrency = 2
prices = { "my-model" = [1, 5] }
[modules.village]
dir = "/x"
"""
    )
    cfg = Config.load({"ANTHROPIC_API_KEY": "sk-x", "SWARM_MCP_MAX_LIMIT": "7"}, cwd=tmp_path)
    assert cfg.modules == ("village", "core")
    assert cfg.max_limit == 50 and cfg.default_limit == 20  # old SWARM_MCP_* env vars are ignored
    assert cfg.scrub is False
    assert cfg.email_allowlist == ("example.org", "agentvillage.org")
    assert cfg.module_setting("village", "dir") == "/x"
    assert (cfg.llm_model, cfg.llm_effort, cfg.llm_fallbacks, cfg.llm_concurrency) == ("claude-haiku-4-5", "none", False, 2)
    assert cfg.llm_prices == {"my-model": (1.0, 5.0)} and cfg.api_key == "sk-x"
    assert "sk-x" not in json.dumps(cfg.public()) and cfg.public()["llm"]["api_key_set"] is True
    assert Config.load({"SWARM_LLM_MODEL": "claude-opus-5-5"}, cwd=tmp_path).llm_model == "claude-opus-5-5"
    # git/wiki/village keep their per-module env fallback
    assert Config.load({"SWARM_GIT_DIR": "/repos"}, cwd=tmp_path).module_setting("git", "dir") == "/repos"
    (tmp_path / "swarm.toml").write_text("[nope]\n")
    with pytest.raises(ConfigError, match="unknown section"):
        Config.load({}, cwd=tmp_path)
    (tmp_path / "swarm.toml").write_text('[server]\nmax_text = "lots"\n')
    with pytest.raises(ConfigError, match="max_text"):
        Config.load({}, cwd=tmp_path)


def test_relative_data_dir_resolves_against_project_root(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "data").mkdir()
    sub = tmp_path / "swarm_mcp"
    sub.mkdir()
    # like `uv run --directory swarm_mcp`: cwd is the subdir, data/ lives at the root
    assert resolve_data_dir("data", cwd=sub) == (tmp_path / "data").resolve()
    assert resolve_data_dir(str(tmp_path / "abs"), cwd=sub) == tmp_path / "abs"
