"""Server introspection: which modules loaded, which were skipped and why."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from swarm_mcp import __version__

NAME = "core"
DESCRIPTION = "Server introspection: loaded/skipped modules (with reasons), version, data dir and config."


def register(mcp, ctx) -> None:
    @ctx.tool()
    def list_modules(
        include_skipped: Annotated[
            bool, Field(description="Also list modules that were skipped, with reasons.")
        ] = True,
    ) -> dict[str, Any]:
        """List the server's modules: loaded ones with their tool names, skipped ones with the reason they did not load."""
        reg = ctx.registry
        out: dict[str, Any] = {"loaded": [r.as_dict() for r in reg.loaded]}
        if include_skipped:
            out["skipped"] = [r.as_dict() for r in reg.skipped]
        if reg.notes:
            out["notes"] = reg.notes
        return out

    @ctx.tool()
    def server_info() -> dict[str, Any]:
        """Server version, resolved data directory, effective configuration and cache status."""
        reg = ctx.registry
        return {
            "name": "swarm",
            "version": __version__,
            "data_dir": str(ctx.config.data_dir),
            "config": ctx.config.public(),
            "modules": {"loaded": [r.name for r in reg.loaded], "skipped": [r.name for r in reg.skipped]},
            "cache": ctx.cache.stats(),
        }
