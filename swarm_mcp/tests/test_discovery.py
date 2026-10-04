"""Module discovery, the module contract and failure isolation."""

from __future__ import annotations

import importlib
from pathlib import Path

from conftest import call, config_for

from swarm_mcp.server import build_server

GOOD = '''
NAME = "good"
DESCRIPTION = "A working module."
def register(mcp, ctx):
    @ctx.tool()
    def hello(name: str = "world") -> dict[str, str]:
        """Say hello."""
        return {"greeting": f"hello {name}"}
'''

NEEDS_DATA = """
NAME = "needsdata"
DESCRIPTION = "Needs a dataset that does not exist."
def requires(ctx):
    return [f"dataset missing: {ctx.data_dir / 'nope'}"]
def register(mcp, ctx):
    raise AssertionError("register must not be called when requires() fails")
"""

BOOM = """
NAME = "boom"
DESCRIPTION = "Raises halfway through register()."
def register(mcp, ctx):
    @ctx.tool()
    def partial() -> dict:
        return {}
    raise RuntimeError("kaboom")
"""

BAD_IMPORT = """
import definitely_not_a_real_package_xyz
NAME = "badimport"
def register(mcp, ctx): ...
"""

REQUIRES_RAISES = """
NAME = "reqraise"
def requires(ctx):
    raise ValueError("cannot even check")
def register(mcp, ctx): ...
"""

NO_PREFIX = """
NAME = "rude"
def register(mcp, ctx):
    mcp.add_tool(lambda: {}, name="unprefixed_tool", description="x")
"""

PRINTS = """
print("this must not reach stdout")
NAME = "noisy"
def register(mcp, ctx):
    print("nor this")
    @ctx.tool()
    def ping() -> dict:
        return {"ok": True}
"""


def _modules(app) -> dict:
    return {r.name: r for r in app.swarm_registry.records.values()}


def test_real_package_skips_template(tmp_path: Path):
    app = build_server(config_for(tmp_path / "empty"))
    names = set(_modules(app))
    assert "core" in names
    assert "_template" not in names and "example" not in names
    # data modules are discovered but skipped (no store), each with a reason
    for name in ("village", "scope", "findings"):
        rec = _modules(app)[name]
        assert rec.status == "skipped" and "not found" in rec.reasons[0], (name, rec.reasons)


def test_template_is_a_valid_module(tmp_path: Path, fake_modules):
    """Copying _template.py into a modules package must yield a working module."""
    pkg, add = fake_modules
    template = Path(importlib.import_module("swarm_mcp.modules").__path__[0]) / "_template.py"
    add("example", template.read_text())
    (tmp_path / "data" / "example-dataset").mkdir(parents=True)
    app = build_server(config_for(tmp_path / "data"), package=pkg)
    rec = _modules(app)["example"]
    assert rec.status == "loaded", rec.reasons
    assert rec.tools == ["example_search"]
    assert rec.resources == ["example://readme"] and rec.prompts == ["example_investigate"]
    out = call(app, "example_search", query="row 999", limit=500)
    assert out["total_matches"] == 1 and "capped" in out["notes"][0]


def test_failures_are_isolated_and_recorded(tmp_path: Path, fake_modules, capsys):
    pkg, add = fake_modules
    for name, src in [
        ("good", GOOD),
        ("needsdata", NEEDS_DATA),
        ("boom", BOOM),
        ("badimport", BAD_IMPORT),
        ("reqraise", REQUIRES_RAISES),
        ("rude", NO_PREFIX),
        ("noisy", PRINTS),
    ]:
        add(name, src)
    add("_private", "raise SystemExit('underscore modules must never be imported')")
    app = build_server(config_for(tmp_path), package=pkg)
    mods = _modules(app)

    assert mods["good"].status == "loaded" and mods["good"].tools == ["good_hello"]
    assert call(app, "good_hello", name="swarm") == {"greeting": "hello swarm"}

    assert mods["needsdata"].status == "skipped"
    assert "dataset missing" in mods["needsdata"].reasons[0]

    assert mods["boom"].status == "skipped"
    assert "register() raised RuntimeError: kaboom" in mods["boom"].reasons[0]
    tool_names = {t.name for t in app._tool_manager.list_tools()}
    assert "boom_partial" not in tool_names  # partial registration rolled back

    assert mods["badimport"].status == "skipped" and "import failed" in mods["badimport"].reasons[0]
    assert mods["reqraise"].status == "skipped" and "requires() raised" in mods["reqraise"].reasons[0]

    assert mods["rude"].status == "loaded" and "not prefixed" in mods["rude"].warnings[0]
    assert "_private" not in mods

    # module prints went to stderr, never stdout
    out = capsys.readouterr()
    assert "must not reach stdout" not in out.out and "nor this" not in out.out
    assert "must not reach stdout" in out.err


def test_core_reports_skips(data_dir: Path):
    app = build_server(config_for(data_dir, disable="village"))
    info = call(app, "core_info")
    out = info["modules"]
    # data-free modules (and the store-backed ones, which only need a writable data dir) stay loaded
    assert [m["name"] for m in out["loaded"]] == ["core", "findings", "investigate", "scope", "subtasks", "sweep"]
    skipped = {m["name"]: m for m in out["skipped"]}
    assert skipped["village"]["reasons"] == ["disabled via [server] disable in swarm.toml"]
    assert info["config"]["data_dir"] == str(data_dir) and info["config"]["disable"] == ["village"]
