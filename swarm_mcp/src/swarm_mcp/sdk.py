"""The only place that touches the MCP SDK's server API.

Modules never import from ``mcp`` directly; they use ``ctx.tool`` /
``ctx.resource`` / ``ctx.prompt`` (see context.py), which call into here.
If the SDK changes (as it did from 1.x FastMCP to 2.x MCPServer), only this
file should need updating.
"""

from __future__ import annotations

import contextlib
import functools
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, ToolAnnotations
from pydantic import ValidationError

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
    "validation_message",
]


class PromptArgumentError(MCPError):
    """Bad prompt arguments. The client gets the message as an invalid-params error; any other
    exception raised while rendering a prompt reaches it only as 'Internal server error'."""

    def __init__(self, message: str) -> None:
        super().__init__(INVALID_PARAMS, message)


def new_app(name: str, version: str, log_level: str = "INFO") -> MCPServer:
    level = log_level if log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "INFO"
    app = MCPServer(name, version=version, instructions="(loading modules)", log_level=level)  # type: ignore[arg-type]
    _short_validation_errors(app)
    return app


# ---------------------------------------------------------------------------- argument errors

_KINDS = {
    "int": "an integer", "float": "a number", "string": "a string", "bool": "true or false", "list": "a list",
    "dict": "an object", "model": "an object", "literal": "one of the allowed values",
}  # fmt: skip
_BOUNDS = {"greater_than_equal": ">=", "less_than_equal": "<=", "greater_than": ">", "less_than": "<"}
_BOUND_KEYS = {"greater_than_equal": "ge", "less_than_equal": "le", "greater_than": "gt", "less_than": "lt"}


def _is_branch_tag(part: object) -> bool:
    """A union-branch marker pydantic puts in an error location (``str``, ``list[str]``, ``function-after[...]``)."""
    return isinstance(part, str) and (part in ("str", "int", "float", "bool", "list", "dict", "none") or "[" in part)


def _got(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def validation_message(exc: ValidationError) -> str:
    """Pydantic argument errors as one line, e.g. ``max_chars: must be >= 20 (got 19); n: required``."""
    by_path: dict[str, list[dict[str, Any]]] = {}
    for err in exc.errors(include_url=False):
        path = ".".join(str(p) for p in err["loc"] if not _is_branch_tag(p)) or "arguments"
        by_path.setdefault(path, []).append(err)
    parts = []
    for path, errs in by_path.items():
        err = errs[0]
        kind, ctx = err["type"], err.get("ctx") or {}
        if len(errs) > 1 and all(_is_branch_tag(e["loc"][-1]) for e in errs):  # str | list[str] got neither
            what = "must be " + " or ".join(_KINDS.get(e["type"].split("_")[0], str(e["loc"][-1])) for e in errs)
        elif kind == "missing":
            parts.append(f"{path}: required")
            continue
        elif kind in _BOUNDS:
            what = f"must be {_BOUNDS[kind]} {ctx.get(_BOUND_KEYS[kind])}"
        elif kind == "literal_error":
            what = f"must be one of {ctx.get('expected')}"
        elif kind.endswith(("_type", "_parsing")) and kind.split("_")[0] in _KINDS:
            what = f"must be {_KINDS[kind.split('_')[0]]}"
        else:
            msg = str(err.get("msg") or kind)
            what = msg[:1].lower() + msg[1:]
        parts.append(f"{path}: {what} (got {_got(err.get('input'))})")
    return "; ".join(parts)


def _short_validation_errors(app: MCPServer) -> None:
    """Report arguments that fail a tool's input schema as one line (``validation_message``) instead of
    pydantic's multi-line text with documentation URLs. The SDK validates before our tool wrapper runs, so
    this wraps the app's ``call_tool`` (used by both the stdio handler and in-process calls)."""
    original = app.call_tool

    @functools.wraps(original)
    async def call_tool(name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        try:
            return await original(name, arguments, context)
        except ToolError as e:
            cause = e.__cause__
            if isinstance(e, UnexpectedToolError) or not isinstance(cause, ValidationError):
                raise
            raise ToolError(f"Error executing tool {name}: {validation_message(cause)}") from cause

    app.call_tool = call_tool  # type: ignore[method-assign]


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
