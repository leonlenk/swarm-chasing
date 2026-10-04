"""Shared event ids, the standard event record, and retrieval by id.

Every piece of evidence a tool returns carries an ``event_id``:

    <source>:<kind>:<local_id>

- ``source``: the dataset (and module) that owns the record, e.g. ``village``.
- ``kind``: the record type inside that source, e.g. ``chat``. Later: ``event``
  (the activity timeline), ``turn`` (a computer-use turn), ``commit``...
- ``local_id``: the source's own id. It may itself contain ``:``.

Ids are opaque to callers: they only pass them back, to ``core_get`` or
to any tool that takes event ids. Derived results (search hits, subtasks,
handoffs, claims) cite event ids instead of copying text around.

Modules make their records retrievable by registering one resolver per source
with ``@ctx.event_source(kinds={...})``. ``core_get`` parses the id,
finds the resolver and returns the record plus its surrounding context in one
standard shape, whatever the source.

The standard record (``event_record``) has these keys; modules may add more:

    event_id, source, kind, time, actor, actor_type, location, text
    (+ truncated: true when text was cut)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from swarm_mcp.toolkit import ToolInputError

_PART = re.compile(r"^[a-z][a-z0-9_-]*$")


def make_event_id(source: str, kind: str, local_id: str) -> str:
    """Build ``<source>:<kind>:<local_id>``. ``source`` and ``kind`` are lowercase slugs."""
    for name, part in (("source", source), ("kind", kind)):
        if not _PART.match(part):
            raise ValueError(f"event id {name} must be a lowercase slug, got {part!r}")
    if not local_id:
        raise ValueError("event id local_id must not be empty")
    return f"{source}:{kind}:{local_id}"


@dataclass(frozen=True)
class EventId:
    source: str
    kind: str
    local_id: str

    def __str__(self) -> str:
        return f"{self.source}:{self.kind}:{self.local_id}"


def parse_event_id(value: str) -> EventId:
    """Split an event id. Raises ``ToolInputError`` with the expected format on bad input."""
    parts = (value or "").strip().split(":", 2)
    if len(parts) != 3 or not all(parts) or not _PART.match(parts[0]) or not _PART.match(parts[1]):
        raise ToolInputError(
            f"Malformed event_id {value!r}. Expected '<source>:<kind>:<id>', e.g. 'village:chat:<uuid>', "
            "exactly as returned by another tool."
        )
    return EventId(*parts)


def event_record(
    event_id: str,
    *,
    time: str | None,
    actor: str | None,
    actor_type: str | None = None,
    location: str | None = None,
    text: str = "",
    truncated: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """The standard record every source returns. ``extra`` keys are appended after the standard ones."""
    eid = parse_event_id(event_id)
    d: dict[str, Any] = {
        "event_id": event_id,
        "source": eid.source,
        "kind": eid.kind,
        "time": time,
        "actor": actor,
        "actor_type": actor_type,
        "location": location,
        "text": text,
    }
    if truncated:
        d["truncated"] = True
    d.update({k: v for k, v in extra.items() if v is not None})
    return d


class EventNotFound(LookupError):
    """Raised by a resolver when the id is well-formed but no such record exists."""


class Resolver(Protocol):
    def __call__(self, kind: str, local_id: str, *, before: int, after: int, max_chars: int) -> dict[str, Any]:
        """Return ``{"event": record, "before": [records], "after": [records], "context": "<what before/after are>"}``.

        ``before``/``after`` are the number of neighbouring records wanted (0 = none). Raise
        ``EventNotFound`` for an unknown id.
        """
        ...


@dataclass
class EventSource:
    name: str
    owner: str  # module that registered it
    kinds: dict[str, str]  # kind -> one-line description
    resolve: Resolver
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.name,
            "module": self.owner,
            "description": self.description,
            "kinds": [
                {"kind": k, "description": v, "id_format": f"{self.name}:{k}:<id>"} for k, v in self.kinds.items()
            ],
        }


@dataclass
class EventSources:
    """All registered sources, keyed by source name."""

    by_name: dict[str, EventSource] = field(default_factory=dict)

    def add(self, src: EventSource) -> None:
        if not _PART.match(src.name):
            raise ValueError(f"event source name must be a lowercase slug, got {src.name!r}")
        if src.name in self.by_name:
            raise ValueError(f"event source {src.name!r} already registered by module {self.by_name[src.name].owner!r}")
        self.by_name[src.name] = src

    def drop_owner(self, owner: str) -> None:
        for name in [n for n, s in self.by_name.items() if s.owner == owner]:
            del self.by_name[name]

    def lookup(self, event_id: str) -> tuple[EventId, EventSource]:
        eid = parse_event_id(event_id)
        src = self.by_name.get(eid.source)
        if src is None:
            known = ", ".join(sorted(self.by_name)) or "none loaded"
            raise ToolInputError(f"Unknown event source {eid.source!r} in {event_id!r}. Loaded sources: {known}.")
        if eid.kind not in src.kinds:
            raise ToolInputError(
                f"Source {eid.source!r} has no kind {eid.kind!r}. Kinds: {', '.join(sorted(src.kinds))}."
            )
        return eid, src
