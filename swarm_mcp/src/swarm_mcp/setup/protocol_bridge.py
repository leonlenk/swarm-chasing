"""The ONE place where setup code meets the dataset/ingest format.

Everything in ``swarm_mcp.setup`` (the mapped adapter, the check, the agents)
produces and consumes the three record types below, built only on
``swarm_mcp.events`` (``make_event_id``, ``parse_event_id``, ``event_record``)
from origin/main. When the unified data format lands, adopt or replace it here:
convert ``AgentRecord`` / ``StandardRecord`` / ``PeriodRecord`` into its row
types (or make these classes aliases of them) and register ``MappedAdapter``
under the name ``mapped`` in its adapter registry. Nothing else should need to
change.

The minimal adapter protocol (``Adapter``):

    source: str                      # event-id source slug, e.g. "forum"
    description: str
    kinds: dict[str, KindInfo]       # every kind it emits (records, periods, "agent")
    agents()  -> Iterator[AgentRecord]
    records() -> Iterator[StandardRecord]
    periods() -> Iterator[PeriodRecord]

Conventions (same as the rest of swarm_mcp):
- record ids are event ids ``<source>:<kind>:<local_id>`` (``events.make_event_id``);
- an *actor key* is an agent id ``<source>:agent:<local_id>`` (``agent_key``) or
  a non-agent key such as ``human:<id>``; ``recipients`` are actor keys;
- ``reply_to`` is a full event id; times are ISO UTC strings ending in ``Z``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Literal, Protocol, runtime_checkable

from swarm_mcp.events import event_record, make_event_id, parse_event_id

ADAPTER_NAME = "mapped"  # registry name for the declarative adapter
TsQuality = Literal["exact", "approx", "derived", "missing"]
Category = Literal["message", "action", "other"]


@dataclass(frozen=True)
class KindInfo:
    description: str
    table: Literal["events", "agents", "periods"] = "events"
    category: Category = "other"


@dataclass
class AgentRecord:
    local_id: str
    display_name: str
    aliases: list[str] = field(default_factory=list)
    first_seen: str | None = None
    last_seen: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def agent_id(self, source: str) -> str:
        return agent_key(source, self.local_id)


@dataclass
class StandardRecord:
    """One timestamped record. ``as_event_record()`` gives the ``events.event_record`` shape."""

    event_id: str
    time: str | None
    actor: str | None
    actor_type: str | None = None
    location: str | None = None
    text: str = ""
    type: str | None = None
    recipients: list[str] = field(default_factory=list)
    reply_to: str | None = None
    ts_quality: TsQuality | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return parse_event_id(self.event_id).kind

    @property
    def local_id(self) -> str:
        return parse_event_id(self.event_id).local_id

    def as_event_record(self) -> dict[str, Any]:
        extra = {
            "type": self.type,
            "recipients": self.recipients or None,
            "reply_to": self.reply_to,
            "ts_quality": self.ts_quality,
            "meta": self.meta or None,
        }
        return event_record(
            self.event_id,
            time=self.time,
            actor=self.actor,
            actor_type=self.actor_type,
            location=self.location,
            text=self.text,
            **extra,
        )


@dataclass
class PeriodRecord:
    event_id: str
    label: str = ""
    start_time: str | None = None
    end_time: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Adapter(Protocol):
    source: str
    description: str
    kinds: dict[str, KindInfo]

    def agents(self) -> Iterator[AgentRecord]: ...

    def records(self) -> Iterator[StandardRecord]: ...

    def periods(self) -> Iterator[PeriodRecord]: ...


def agent_key(source: str, local_id: str) -> str:
    """Actor key of an agent: ``<source>:agent:<local_id>`` (itself an event id)."""
    return make_event_id(source, "agent", local_id)


def event_id(source: str, kind: str, local_id: str) -> str:
    return make_event_id(source, kind, local_id)


def to_dict(rec: AgentRecord | StandardRecord | PeriodRecord) -> dict[str, Any]:
    """JSON-friendly dict (for dumps and future row conversion)."""
    return asdict(rec)


def describe(adapter: Adapter) -> dict[str, Any]:
    return {
        "name": ADAPTER_NAME,
        "source": adapter.source,
        "description": adapter.description,
        "kinds": {k: asdict(v) for k, v in adapter.kinds.items()},
    }
