"""TEMPLATE module -- copy me. Files starting with "_" are never loaded.

To add a module:
    cp _template.py mymodule.py   # then edit NAME/DESCRIPTION/register()
Restart the server; core_info will show it (or why it was skipped).

The contract (all at module top level):
    NAME          str   tool-name prefix; tools become "<NAME>_<function name>"
    DESCRIPTION   str   one line, shown in core_info and server instructions
    requires(ctx) optional -> list[str]; non-empty = reasons the module can't load
    register(mcp, ctx)    adds tools/resources/prompts; may raise (module is skipped)

``ctx`` (swarm_mcp.context.ModuleContext) gives you:
    ctx.config / ctx.data_dir     global config; SWARM_DATA_DIR resolved to a Path
    ctx.setting("key", default)   per-module setting: [modules.<name>] key = ... in swarm.toml
    ctx.lazy("key", loader)       compute-once, thread-safe, shared cache
    ctx.log                       logger -> stderr (NEVER print to stdout in stdio mode)
    ctx.limit(limit)              (effective_limit, note) using default 20 / max 200
    ctx.untrusted(text, max_chars) dataset text -> {"content", "untrusted": True}, masked, capped
                                  (default 500). Use it for EVERY agent/human-authored string you return.
    ctx.store()                   ``with ctx.store() as s:`` short-lived SwarmScope DuckDB Store
    ctx.scrub(text)               mask emails/phones per privacy config (untrusted() already does this)
    @ctx.tool()                   register "<NAME>_<fn name>" with clean error handling
    @ctx.resource("path")         register a resource at "<NAME>://path"
    @ctx.prompt()                 register a prompt "<NAME>_<fn name>"
Use only ctx.* to register things; ``mcp`` is passed for the contract but its
API is SDK-version-specific (swarm_mcp/sdk.py is the one adapter).
Helpers in swarm_mcp.toolkit: ToolInputError, untrusted, truncate, snippet, parse_time, iso.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from swarm_mcp.toolkit import ToolInputError

NAME = "example"
DESCRIPTION = "One-line description of what these tools are for."


def requires(ctx) -> list[str]:
    """Return reasons this module cannot load (empty list = OK). Keep it cheap."""
    path = ctx.data_dir / "example-dataset"
    return [] if path.exists() else [f"dataset not found: {path}"]


def _load(ctx) -> list[dict[str, Any]]:
    # Heavy work goes here; it runs once, on first tool call (not at startup).
    return [{"id": i, "text": f"row {i}"} for i in range(1000)]


def register(mcp, ctx) -> None:
    # Tool: becomes "example_search". Typed params + Field descriptions become the
    # JSON schema the LLM sees; the docstring becomes the tool description.
    @ctx.tool()
    def search(
        query: Annotated[str, Field(description="Case-insensitive substring to look for.")],
        limit: Annotated[int, Field(description="Max results (default 20, max 200).")] = 20,
    ) -> dict[str, Any]:
        """Search example rows. Returns matching rows, newest first."""
        if not query.strip():
            raise ToolInputError("query must not be empty")  # caller sees exactly this message
        limit, note = ctx.limit(limit)
        rows = ctx.lazy("rows", lambda: _load(ctx))
        hits = [r for r in rows if query.lower() in r["text"].lower()]
        out = []
        for r in hits[:limit]:
            # dataset text is always wrapped: {"content": masked+capped text, "untrusted": True}
            out.append({"id": r["id"], "text": ctx.untrusted(r["text"])})
        return {"total_matches": len(hits), "returned": len(out), "results": out, "notes": [n for n in [note] if n]}

    # Resource (read-only context a client can attach): served at "example://readme".
    @ctx.resource("readme", description="What the example dataset contains.")
    def readme() -> str:
        return "Example dataset: 1000 synthetic rows."

    # Prompt (a reusable message template): registered as "example_investigate".
    @ctx.prompt()
    def investigate(topic: str) -> str:
        """Start an investigation of a topic."""
        return f"Use the {NAME}_* tools to investigate: {topic}"
