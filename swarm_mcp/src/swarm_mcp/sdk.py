"""The only place that touches the MCP SDK's server API.

Modules never import from ``mcp`` directly; they use ``ctx.tool`` /
``ctx.resource`` / ``ctx.prompt`` (see context.py), which call into here.
If the SDK changes (as it did from 1.x FastMCP to 2.x MCPServer), only this
file should need updating.
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, ToolAnnotations

App = MCPServer

__all__ = [
    "App",
    "ToolError",
    "PromptArgumentError",
    "new_app",
    "set_instructions",
    "snapshot",
    "rollback",
    "add_tool",
    "add_resource",
    "add_prompt",
    "run_stdio",
]


class PromptArgumentError(MCPError):
    """Bad prompt arguments. The client gets the message as an invalid-params error; any other
    exception raised while rendering a prompt reaches it only as 'Internal server error'."""

    def __init__(self, message: str) -> None:
        super().__init__(INVALID_PARAMS, message)


def new_app(name: str, version: str, log_level: str = "INFO") -> MCPServer:
    level = log_level if log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "INFO"
    return MCPServer(name, version=version, instructions="(loading modules)", log_level=level)  # type: ignore[arg-type]


def set_instructions(app: MCPServer, text: str) -> None:
    with contextlib.suppress(AttributeError):
        app._lowlevel_server.instructions = text


def snapshot(app: MCPServer) -> dict[str, set[str]]:
    """Names of everything currently registered (used to attribute and roll back)."""
    return {
        "tools": {t.name for t in app._tool_manager.list_tools()},
        "resources": {str(r.uri) for r in app._resource_manager.list_resources()},
        "templates": {t.uri_template for t in app._resource_manager.list_templates()},
        "prompts": {p.name for p in app._prompt_manager.list_prompts()},
    }


def rollback(app: MCPServer, before: dict[str, set[str]]) -> None:
    """Remove everything registered since ``before`` (a failed module's partial work)."""
    after = snapshot(app)
    for name in after["tools"] - before["tools"]:
        with contextlib.suppress(Exception):
            app.remove_tool(name)
    for name in after["prompts"] - before["prompts"]:
        with contextlib.suppress(Exception):
            app.remove_prompt(name)
    for uri in after["resources"] - before["resources"]:
        app._resource_manager._resources.pop(uri, None)
    for uri in after["templates"] - before["templates"]:
        app._resource_manager._templates.pop(uri, None)


def add_tool(
    app: MCPServer,
    fn: Callable[..., Any],
    *,
    name: str,
    description: str | None,
    title: str | None = None,
    read_only: bool = True,
    structured_output: bool | None = None,
) -> None:
    app.add_tool(
        fn,
        name=name,
        title=title,
        description=description,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False) if read_only else None,
        structured_output=structured_output,
    )


def add_resource(
    app: MCPServer, fn: Callable[..., Any], *, uri: str, description: str | None, mime_type: str | None = None
) -> None:
    app.resource(uri, description=description, mime_type=mime_type)(fn)


def add_prompt(app: MCPServer, fn: Callable[..., Any], *, name: str, description: str | None) -> None:
    app.prompt(name=name, description=description)(fn)


def run_stdio(app: MCPServer) -> None:
    """Serve over stdio. The SDK (2.x) points fd 1 at stderr while serving, so
    stray prints from tools can't corrupt the protocol stream."""
    app.run("stdio")
