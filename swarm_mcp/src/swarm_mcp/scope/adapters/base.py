"""The adapter protocol.

An adapter knows one dataset layout. ``inspect(path)`` describes what is there
(files, row counts where cheap, field names) without loading it;
``load(path)`` streams ``(table, row)`` pairs where ``table`` is one of
``schema.RECORD_MODELS`` (agents, messages, actions, periods) and ``row`` is a
dict matching that model. ``ingest.ingest`` validates and bulk-loads them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable


@runtime_checkable
class Adapter(Protocol):
    name: str  # CLI name, e.g. "ai_village"
    source: str  # evidence-id source prefix, e.g. "village"

    def inspect(self, path: Path) -> dict[str, Any]: ...

    def load(self, path: Path, **options: Any) -> Iterator[tuple[str, dict[str, Any]]]: ...
