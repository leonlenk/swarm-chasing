"""SwarmScope adapter for declaratively mapped datasets (``swarm_mcp.setup``).

``setup.mapping.MappedAdapter`` turns any dataset plus a mapping JSON into
``protocol_bridge`` records (``StandardRecord`` / ``AgentRecord`` /
``PeriodRecord``). This module converts those into the store's existing row
types, so a mapped dataset lands in the same tables as AI Village:

    StandardRecord, category "message"  -> messages   (location -> channel, type -> msg_type)
    StandardRecord, category "action"   -> actions    (kind = the record's kind)
    StandardRecord, category "other"    -> actions    (kind = the record's kind)
    AgentRecord                          -> agents     (agent_id = <source>:agent:<local_id>)
    PeriodRecord                         -> periods    (kind = the period's kind)

Evidence ids are kept exactly as the mapping produced them (``<source>:<kind>:<id>``),
and the mapping's kinds are recorded in ``sources.meta.kinds``, so they resolve
through ``core_get`` and the scope tools. Records are yielded first (so
agents carry first/last seen), then agents, then periods. ``ingest_mapped`` loads
it with the normal idempotent ``scope.ingest.ingest`` (all rows of the source are
replaced).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from swarm_mcp.setup.mapping import MappedAdapter
from swarm_mcp.setup.protocol_bridge import AgentRecord, PeriodRecord, StandardRecord, agent_key, describe

UNKNOWN_ACTOR = "unknown"  # author_id/agent_id for records the mapping gave no actor


def _ts(value: str | None) -> datetime | None:
    """ISO UTC string (``...Z``) -> naive UTC datetime, the store's timestamp convention."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def record_row(rec: StandardRecord, source: str, category: str) -> tuple[str, dict[str, Any]]:
    """(table, row) for one standard record; ``category`` is the mapping's category for its kind."""
    ts = _ts(rec.time)
    quality = rec.ts_quality or ("exact" if ts else "missing")
    if ts is None:
        quality = "missing"
    meta: dict[str, Any] = {"kind": rec.kind, "category": category, **(rec.meta or {})}
    if rec.actor_type:
        meta["actor_type"] = rec.actor_type
    if category == "message":
        return "messages", {
            "evidence_id": rec.event_id,
            "source": source,
            "channel": rec.location,
            "author_id": rec.actor or UNKNOWN_ACTOR,
            "recipient_ids": list(rec.recipients or []),
            "reply_to": rec.reply_to,
            "ts": ts,
            "ts_quality": quality,
            "msg_type": rec.type,
            "content": rec.text or "",
            "meta": meta,
        }
    for key, value in (("type", rec.type), ("location", rec.location), ("reply_to", rec.reply_to)):
        if value:
            meta.setdefault(key, value)
    if rec.recipients:
        meta.setdefault("recipients", list(rec.recipients))
    return "actions", {
        "evidence_id": rec.event_id,
        "source": source,
        "agent_id": rec.actor or UNKNOWN_ACTOR,
        "run_id": None,
        "seq": None,
        "ts": ts,
        "ts_quality": quality,
        "kind": rec.kind,
        "content": rec.text or "",
        "meta": meta,
    }


def agent_row(agent: AgentRecord, source: str) -> dict[str, Any]:
    return {
        "agent_id": agent_key(source, agent.local_id),
        "source": source,
        "display_name": agent.display_name or agent.local_id,
        "aliases": list(agent.aliases or []),
        "first_seen": _ts(agent.first_seen),
        "last_seen": _ts(agent.last_seen),
        "meta": {"native_id": agent.local_id, **(agent.meta or {})},
    }


def period_row(period: PeriodRecord, source: str) -> dict[str, Any]:
    kind = period.event_id.split(":", 2)[1]
    return {
        "evidence_id": period.event_id,
        "source": source,
        "kind": kind,
        "label": period.label or "",
        "start_ts": _ts(period.start_time),
        "end_ts": _ts(period.end_time),
        "meta": dict(period.meta or {}),
    }


class MappedStoreAdapter:
    """``scope.adapters.base.Adapter`` over a ``MappedAdapter`` (a mapping + a dataset path)."""

    name = "mapped"

    def __init__(self, mapped: MappedAdapter, mapping_path: str | Path | None = None):
        self.mapped = mapped
        self.source = mapped.source
        self.mapping_path = str(Path(mapping_path).resolve()) if mapping_path else None

    @classmethod
    def from_file(cls, mapping: str | Path, path: str | Path | None = None) -> "MappedStoreAdapter":
        return cls(MappedAdapter.from_file(mapping, path), mapping)

    def categories(self) -> dict[str, str]:
        """kind -> category for the record kinds."""
        return {k: v.category for k, v in self.mapped.kinds.items() if v.table == "events"}

    @property
    def source_meta(self) -> dict[str, Any]:
        """Stored in ``sources.meta``: the kinds make mapped evidence ids resolvable."""
        return {
            "mapping": self.mapping_path,
            "description": self.mapped.description,
            "kinds": {k: v.description for k, v in self.mapped.kinds.items()},
            "categories": self.categories(),
        }

    def inspect(self, path: Path | None = None) -> dict[str, Any]:
        return {**describe(self.mapped), "root": str(self.mapped.root), "mapping": self.mapping_path}

    def load(self, path: Path | None = None, **_: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        """Records first (so agents carry first/last seen), then agents, then periods."""
        src, cats = self.source, self.categories()
        for rec in self.mapped.records():
            yield record_row(rec, src, cats.get(rec.kind, "message"))
        for agent in self.mapped.agents():
            yield "agents", agent_row(agent, src)
        for period in self.mapped.periods():
            yield "periods", period_row(period, src)
