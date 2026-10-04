"""Module selection env vars and config parsing."""

from __future__ import annotations

from pathlib import Path

from conftest import config_for

from swarm_mcp.config import Config, resolve_data_dir
from swarm_mcp.server import build_server


def _status(app) -> dict[str, str]:
    return {r.name: r.status for r in app.swarm_registry.records.values()}


# git/subtasks/wiki need a repo or wiki db, which the synthetic fixtures do not have
ALL = {
    "core": "loaded",
    "findings": "loaded",
    "scope": "loaded",
    "village": "loaded",
    "git": "skipped",
    "subtasks": "skipped",
    "wiki": "skipped",
}


def test_default_loads_all(data_dir: Path):
    assert _status(build_server(config_for(data_dir))) == ALL


def test_modules_allowlist_keeps_core(data_dir: Path):
    app = build_server(config_for(data_dir, SWARM_MCP_MODULES="village"))
    assert _status(app) == {**ALL, "findings": "skipped", "scope": "skipped"}
    app = build_server(config_for(data_dir, SWARM_MCP_MODULES="core"))
    st = _status(app)
    assert st["village"] == "skipped"
    assert "not selected" in app.swarm_registry.records["village"].reasons[0]


def test_disable_wins_and_can_disable_core(data_dir: Path):
    app = build_server(config_for(data_dir, SWARM_MCP_MODULES="village", SWARM_MCP_DISABLE="village, core"))
    assert set(_status(app).values()) == {"skipped"}


def test_store_paths(tmp_path: Path):
    cfg = Config.from_env({"SWARM_DATA_DIR": str(tmp_path)})
    assert cfg.store_path == tmp_path / "swarmscope.duckdb" and cfg.max_text == 500
    cfg = Config.from_env(
        {
            "SWARM_DATA_DIR": str(tmp_path),
            "SWARMSCOPE_DB": str(tmp_path / "x.duckdb"),
            "SWARMSCOPE_FINDINGS_DIR": str(tmp_path / "f"),
        }
    )
    assert cfg.store_path == tmp_path / "x.duckdb" and cfg.findings_path == tmp_path / "f"
    assert cfg.public()["db_path"] == str(tmp_path / "x.duckdb")


def test_unknown_module_names_are_noted(data_dir: Path):
    app = build_server(config_for(data_dir, SWARM_MCP_MODULES="village,nonsense"))
    assert any("nonsense" in n for n in app.swarm_registry.notes)


def test_config_parsing(tmp_path: Path):
    cfg = Config.from_env(
        {
            "SWARM_DATA_DIR": str(tmp_path),
            "SWARM_MCP_MODULES": " Village , core ",
            "SWARM_MCP_MAX_LIMIT": "50",
            "SWARM_MCP_DEFAULT_LIMIT": "oops",
            "SWARM_MCP_SCRUB": "off",
            "SWARM_MCP_EMAIL_ALLOWLIST": "example.org,agentvillage.org",
            "SWARM_VILLAGE_DIR": "/x",
        }
    )
    assert cfg.modules == ("village", "core")
    assert cfg.max_limit == 50 and cfg.default_limit == 20
    assert cfg.scrub is False
    assert cfg.email_allowlist == ("example.org", "agentvillage.org")
    assert cfg.module_setting("village", "dir") == "/x"


def test_relative_data_dir_resolves_against_project_root(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "data").mkdir()
    sub = tmp_path / "swarm_mcp"
    sub.mkdir()
    # like `uv run --directory swarm_mcp`: cwd is the subdir, data/ lives at the root
    assert resolve_data_dir("data", cwd=sub) == (tmp_path / "data").resolve()
    assert resolve_data_dir(str(tmp_path / "abs"), cwd=sub) == tmp_path / "abs"
