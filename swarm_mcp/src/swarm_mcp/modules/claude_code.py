"""Claude Code sessions recorded by the swarm-live hooks: bring them into the SwarmScope store.

The hooks write to a recordings SQLite file while sessions run (``swarm_mcp.live``); the ``claude_code``
adapter maps it onto the store as source ``claude-code``, after which every scope/findings/sweep tool and
``core_get`` work on it. ``claude_code_sync`` re-ingests it on demand. Inside the Claude Code plugin
(``CLAUDE_PLUGIN_DATA`` set) the server also syncs once at startup when there are new recordings; this module
loads before ``scope`` and ``findings``, so their store check then passes even in a fresh project.

Settings: ``[modules.claude_code] db`` in swarm.toml (or env ``SWARM_LIVE_DB``) for the recordings file.
"""

from __future__ import annotations

import os
from datetime import timezone
from pathlib import Path
from typing import Any

from swarm_mcp.live.store import default_db

NAME = "claude_code"
DESCRIPTION = (
    "Claude Code sessions recorded by the swarm-live hooks (main agents, subagents, prompts, tool calls, "
    "delegations, files): claude_code_sync loads the latest recordings into the store as source 'claude-code'."
)


def recordings_path(ctx) -> Path:
    override = ctx.setting("db")
    return Path(override).expanduser() if override else default_db()


def requires(ctx) -> list[str]:
    p = recordings_path(ctx)
    if p.exists():
        return []
    return [f"no swarm-live recordings at {p} (the plugin's hooks create it; or `swarm-live import <transcripts>`)"]


def _synced_at(ctx) -> float | None:
    """When the claude-code source was last ingested into the store (epoch seconds), if ever."""
    if not ctx.store_path.exists():
        return None
    from swarm_mcp.scope.adapters.claude_code import SOURCE

    with ctx.store() as s:
        if not s.has_table("sources"):
            return None
        t = s.scalar("SELECT ingested_at FROM sources WHERE source = ?", [SOURCE])
    return t.replace(tzinfo=timezone.utc).timestamp() if t else None


def _sync(ctx) -> dict[str, Any]:
    from swarm_mcp.scope.ingest import ingest

    # The claude-code source belongs to this module: a sync always replaces it, even when the recordings
    # file moved (the plugin's data dir vs ~/.swarm-live), which ingest would otherwise refuse as a SourceConflict.
    return ingest("claude_code", recordings_path(ctx), ctx.store_path, replace=True)


def _stale(ctx) -> bool:
    p = recordings_path(ctx)
    newest = max((q.stat().st_mtime for q in (p, p.with_name(p.name + "-wal")) if q.exists()), default=0)
    synced = _synced_at(ctx)
    return synced is None or newest > synced


def register(mcp, ctx) -> None:
    if os.environ.get("CLAUDE_PLUGIN_DATA"):
        try:
            if _stale(ctx):
                res = _sync(ctx)
                ctx.log.info("synced claude-code recordings at startup: %s", res["counts"])
        except Exception as e:  # noqa: BLE001 - a failed sync must not keep the server from starting
            ctx.log.warning("startup sync of claude-code recordings failed: %s: %s", type(e).__name__, e)

    @ctx.tool(read_only=False)
    def claude_code_sync() -> dict[str, Any]:
        """Load the latest recorded Claude Code sessions into the store (source 'claude-code'), replacing the
        previous copy of that source; other sources and findings are untouched. Call it before investigating
        sessions that are still running. Afterwards use the usual tools with source='claude-code': scope_search
        (prompts, delegations and agent text are messages; tool calls are table='actions'), scope_agents,
        scope_periods (one per session or subagent run), scope_graph (delegation and reporting edges) and core_get.
        If the scope tools were missing because the store did not exist yet, restart the server (/mcp) after
        the first sync."""
        before = ctx.store_path.exists()
        res = _sync(ctx)
        out = {
            "source": res["source"],
            "recordings": str(recordings_path(ctx)),
            "store": res["db"],
            "counts": res["counts"],
            "seconds": res["seconds"],
            "notes": [],
        }
        if not before:
            out["notes"].append("the store was just created: restart the MCP server (/mcp) to load the scope tools")
        return out
