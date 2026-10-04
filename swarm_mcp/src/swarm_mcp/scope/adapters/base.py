"""The adapter protocol.

An adapter knows one dataset layout. ``inspect(path)`` describes what is there
(files, row counts where cheap, field names) without loading it;
``load(path)`` streams ``(table, row)`` pairs where ``table`` is one of
``schema.RECORD_MODELS`` (agents, messages, actions, periods, artifacts, touches) and ``row`` is a
dict matching that model. ``ingest.ingest`` validates and bulk-loads them. An optional ``notes``
list (the dataset's blind spots) is stored with the source and shown by ``core_info``.

Evidence ids use schema kinds, never dataset-specific ones: ``<source>:msg:``, ``:event:``,
``:agent:``, ``:period:``, ``:artifact:`` (see ``evidence.py``); put the dataset's own type
(commit, revision, pull_request...) in the record's ``kind`` / ``msg_type`` field.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable


@runtime_checkable
class Adapter(Protocol):
    name: str  # CLI name, e.g. "ai_village"
    source: str  # evidence-id source prefix, e.g. "village"; may be set by load() from the path (git, wiki)

    def inspect(self, path: Path) -> dict[str, Any]: ...

    def load(self, path: Path, **options: Any) -> Iterator[tuple[str, dict[str, Any]]]: ...
