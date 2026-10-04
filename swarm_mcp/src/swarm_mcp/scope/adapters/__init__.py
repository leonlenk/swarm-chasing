"""Dataset adapters. Each maps one dataset onto the unified schema."""

from __future__ import annotations

from swarm_mcp.scope.adapters.ai_village import AiVillageAdapter
from swarm_mcp.scope.adapters.base import Adapter
from swarm_mcp.scope.adapters.claude_code import ClaudeCodeAdapter
from swarm_mcp.scope.adapters.git_repo import GitRepoAdapter
from swarm_mcp.scope.adapters.wiki_db import WikiAdapter

ADAPTERS: dict[str, type] = {
    "ai_village": AiVillageAdapter,
    "claude_code": ClaudeCodeAdapter,
    "git": GitRepoAdapter,
    "wiki": WikiAdapter,
}


def get_adapter(name: str, source: str | None = None) -> Adapter:
    """An adapter instance. ``source`` overrides the evidence-id source prefix for adapters that take one
    (git and wiki default to the repo / folder name)."""
    if name == "mapped":
        raise ValueError(
            "The mapped adapter needs a mapping file: swarm-mcp add <path> --mapping M "
            "(library: scope.ingest.ingest_mapped(mapping, path, db))."
        )
    try:
        cls = ADAPTERS[name]
    except KeyError:
        raise ValueError(f"Unknown adapter {name!r}. Available: {', '.join(sorted(ADAPTERS))}") from None
    if source is None:
        return cls()
    try:
        return cls(source=source)
    except TypeError:
        raise ValueError(f"Adapter {name!r} has a fixed source and does not take --source") from None


__all__ = ["ADAPTERS", "Adapter", "get_adapter"]
