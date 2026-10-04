"""Dataset adapters. Each maps one dataset onto the unified schema."""

from __future__ import annotations

from swarm_mcp.scope.adapters.ai_village import AiVillageAdapter
from swarm_mcp.scope.adapters.base import Adapter

ADAPTERS: dict[str, type] = {"ai_village": AiVillageAdapter}


def get_adapter(name: str) -> Adapter:
    if name == "mapped":
        raise ValueError(
            "The mapped adapter needs a mapping file: use scope.ingest.ingest_mapped(mapping, path, db) "
            "(see scope.adapters.mapped)."
        )
    try:
        return ADAPTERS[name]()
    except KeyError:
        raise ValueError(
            f"Unknown adapter {name!r}. Available: {', '.join(sorted(ADAPTERS))} (and 'mapped', with a mapping file)"
        ) from None


__all__ = ["ADAPTERS", "Adapter", "get_adapter"]
