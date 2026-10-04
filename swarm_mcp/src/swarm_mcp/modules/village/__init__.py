"""AI Village (AI Digest): the dataset's own docs as resources.

The tools that used to live here are generic now and work on every source in the
SwarmScope store: ``scope_periods`` lists the weekly village goals (with the
heuristic goal type) and per-goal activity; ``scope_search``/``scope_agents``/
``scope_timeline``/``scope_graph`` do the rest. This module keeps the
README/SCHEMA/CHANGELOG resources (from ``<data dir>/ai-village`` or
``[modules.village] dir``) and re-exports ``goal_type`` for older imports (name
matching lives in ``scope.names``). Data comes from the store
(``swarm-mcp add data/ai-village``).
"""

from __future__ import annotations

from pathlib import Path

from swarm_mcp.scope.adapters.ai_village import goal_type  # noqa: F401 - re-exported for older imports

NAME = "village"
DESCRIPTION = (
    "AI Village docs (AI Digest; frontier-model agents sharing a group chat with weekly goals): README, schema "
    "and changelog resources. Use scope_periods for the goal sequence and scope_* for everything else."
)
SOURCE = "village"


def village_dir(ctx) -> Path:
    override = ctx.setting("dir")
    return Path(override).expanduser() if override else ctx.data_dir / "ai-village"


def requires(ctx) -> list[str]:
    path = ctx.store_path
    if not path.exists():
        return [f"SwarmScope store not found at {path}; run `swarm-mcp add data/ai-village`"]
    try:
        with ctx.store() as s:
            if not s.scalar("SELECT count(*) FROM sources WHERE source = ?", [SOURCE]):
                return [f"no AI Village data in {path}; run `swarm-mcp add data/ai-village`"]
    except Exception as e:  # noqa: BLE001 - e.g. a store from an older schema
        return [f"cannot read {path}: {type(e).__name__}: {e}"]
    return []


def register(mcp, ctx) -> None:
    root = village_dir(ctx)
    for fname, path, desc in (
        ("README.md", "readme", "AI Village dataset card (what each file is, processing notes)."),
        ("SCHEMA.md", "schema", "Column-level schema for every AI Village table."),
        (
            "CHANGELOG.md",
            "changelog",
            "Dated scaffolding changes and agent roster; read before drawing conclusions over time.",
        ),
    ):
        if (root / fname).exists():
            ctx.resource(path, description=desc, mime_type="text/markdown")(_file_reader(root / fname, path))


def _file_reader(file: Path, name: str):
    def read() -> str:
        return file.read_text(encoding="utf-8")

    read.__name__ = f"village_{name}"
    return read
