"""swarm_mcp: a modular MCP server for the swarm-understanding toolkit.

Tool modules live in ``swarm_mcp.modules``; see ``ADDING_MODULES.md``.
"""

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("swarm-mcp")
except PackageNotFoundError:  # pragma: no cover - running from a raw checkout
    __version__ = "0.0.0+local"
