"""The ONE place where setup code meets the dataset/ingest format.

Everything in ``swarm_mcp.setup`` (the mapped adapter, the check, the agents)
produces and consumes the three record types below, built on the store's id and
record conventions: ``scope.evidence`` (``make``, ``parse``) and
``scope.records.event_record``. ``scope.adapters.mapped`` converts them into the
store's row types (messages, actions, agents, periods).

The minimal adapter protocol (``Adapter``):

    source: str                      # evidence-id source slug, e.g. "forum"
    description: str
    kinds: dict[str, KindInfo]       # every dataset kind it emits (records, periods, "agent")
    agents()  -> Iterator[AgentRecord]
    records() -> Iterator[StandardRecord]
    periods() -> Iterator[PeriodRecord]

Conventions (same as the rest of swarm_mcp):
- ids are evidence ids with SCHEMA kinds only (``scope.evidence``): a record of the
  dataset kind ``post`` in category ``message`` is ``<source>:msg:post/<local_id>``;
  categories ``action``/``other`` give ``<source>:event:<kind>/<local_id>``; periods
  ``<source>:period:<kind>/<local_id>`` (``record_id``). The dataset kind always prefixes
  the local id, so entries sharing a schema kind never collide and ids stay stable when a
  mapping gains entries. The dataset kind is also kept on the record (``kind``);
- an *actor key* is an agent id ``<source>:agent:<local_id>`` (``agent_key``) or
  a non-agent key such as ``human:<id>``; ``recipients`` are actor keys;
- ``reply_to`` is a full evidence id; times are ISO UTC strings ending in ``Z``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Literal, Protocol, runtime_checkable

from swarm_mcp.scope import evidence
from swarm_mcp.scope.records import event_record

ADAPTER_NAME = "mapped"  # registry name for the declarative adapter
TsQuality = Literal["exact", "approx", "derived", "missing"]
Category = Literal["message", "action", "other"]

# mapping category -> schema kind of the record's evidence id
CATEGORY_SCHEMA_KIND: dict[str, str] = {"message": "msg", "action": "event", "other": "event"}
PERIOD_SCHEMA_KIND = "period"


@dataclass(frozen=True)
class KindInfo:
    description: str
    table: Literal["events", "agents", "periods"] = "events"
    category: Category = "other"

    @property
    def schema_kind(self) -> str:
        """The evidence-id kind records of this dataset kind get (msg, event, period or agent)."""
        if self.table == "agents":
            return "agent"
        if self.table == "periods":
            return PERIOD_SCHEMA_KIND
        return CATEGORY_SCHEMA_KIND[self.category]


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
    """One timestamped record. ``as_event_record()`` gives the ``scope.records.event_record`` shape.

    ``event_id`` uses the schema kind (``msg``/``event``); ``kind`` is the dataset's own record
    type (the mapping entry's ``kind``, e.g. "post"); ``local_id`` is the dataset's id."""

    event_id: str
    kind: str
    local_id: str
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
    def schema_kind(self) -> str:
        return evidence.parse(self.event_id).kind

    def as_event_record(self) -> dict[str, Any]:
        type_key = "msg_type" if self.schema_kind == "msg" else "action_kind"
        extra = {
            type_key: self.kind,
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
    kind: str = ""  # the dataset's period type (the mapping entry's ``kind``)
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
    """Actor key of an agent: ``<source>:agent:<local_id>`` (itself an evidence id)."""
    return evidence.make(source, "agent", local_id)


def record_id(source: str, schema_kind: str, kind: str, local_id: str) -> str:
    """Evidence id of a mapped record or period: ``<source>:<schema_kind>:<kind>/<local_id>``."""
    if not local_id:
        raise ValueError("local_id must not be empty")
    return evidence.make(source, schema_kind, f"{kind}/{local_id}")


def to_dict(rec: AgentRecord | StandardRecord | PeriodRecord) -> dict[str, Any]:
    """JSON-friendly dict (for dumps and future row conversion)."""
    return asdict(rec)


def describe(adapter: Adapter) -> dict[str, Any]:
    return {
        "name": ADAPTER_NAME,
        "source": adapter.source,
        "description": adapter.description,
        "kinds": {k: {**asdict(v), "schema_kind": v.schema_kind} for k, v in adapter.kinds.items()},
    }
