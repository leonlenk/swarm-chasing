"""SwarmScope adapter for declaratively mapped datasets (``swarm_mcp.setup``).

``setup.mapping.MappedAdapter`` turns any dataset plus a mapping JSON into
``protocol_bridge`` records (``StandardRecord`` / ``AgentRecord`` /
``PeriodRecord``). This module converts those into the store's row types, so a
mapped dataset lands in the same tables as every other source:

    StandardRecord, category "message"  -> messages  (<source>:msg:<kind>/<id>; location -> channel,
                                                      msg_type = the dataset kind, meta.type = the type role)
    StandardRecord, category "action"   -> actions   (<source>:event:<kind>/<id>; kind = the dataset kind)
    StandardRecord, category "other"    -> actions   (same as "action")
    AgentRecord                          -> agents    (agent_id = <source>:agent:<local_id>)
    PeriodRecord                         -> periods   (<source>:period:<kind>/<id>; kind = the dataset kind)

Ids already use the schema kinds (see ``setup.mapping``), so they resolve through
``core_get`` and the scope tools like any other source's. ``source_meta`` (stored in
``sources.meta``) records the mapping path, its description and the dataset kind ->
category map. Records are yielded first (so agents carry first/last seen), then agents,
then periods. ``ingest_mapped`` loads it with the normal idempotent ``scope.ingest.ingest``
(all rows of the source are replaced).
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


def record_row(rec: StandardRecord, source: str) -> tuple[str, dict[str, Any]]:
    """(table, row) for one standard record: ``msg`` ids go to messages, ``event`` ids to actions."""
    ts = _ts(rec.time)
    quality = rec.ts_quality or ("exact" if ts else "missing")
    if ts is None:
        quality = "missing"
    meta: dict[str, Any] = {"native_id": rec.local_id, **(rec.meta or {})}
    # always stored, so readers never guess "agent" for an unmatched (external:...) or missing actor
    meta["actor_type"] = rec.actor_type or ("external" if rec.actor else UNKNOWN_ACTOR)
    if rec.type:
        meta.setdefault("type", rec.type)
    if rec.schema_kind == "msg":
        return "messages", {
            "evidence_id": rec.event_id,
            "source": source,
            "channel": rec.location,
            "author_id": rec.actor or UNKNOWN_ACTOR,
            "recipient_ids": list(rec.recipients or []),
            "reply_to": rec.reply_to,
            "ts": ts,
            "ts_quality": quality,
            "msg_type": rec.kind,
            "content": rec.text or "",
            "meta": meta,
        }
    for key, value in (("location", rec.location), ("reply_to", rec.reply_to)):
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
    return {
        "evidence_id": period.event_id,
        "source": source,
        "kind": period.kind,
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
        """Dataset kind -> category: message/action/other for records, "period" for periods."""
        kinds = self.mapped.kinds.items()
        return {k: ("period" if v.table == "periods" else v.category) for k, v in kinds if v.table != "agents"}

    @property
    def source_meta(self) -> dict[str, Any]:
        """Stored in ``sources.meta``: where the mapping is and which dataset kinds it maps."""
        return {
            "mapping": self.mapping_path,
            "description": self.mapped.description,
            "categories": self.categories(),
        }

    def inspect(self, path: Path | None = None) -> dict[str, Any]:
        return {**describe(self.mapped), "root": str(self.mapped.root), "mapping": self.mapping_path}

    def load(self, path: Path | None = None, **_: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        """Records first (so agents carry first/last seen), then agents, then periods."""
        src = self.source
        for rec in self.mapped.records():
            yield record_row(rec, src)
        for agent in self.mapped.agents():
            yield "agents", agent_row(agent, src)
        for period in self.mapped.periods():
            yield "periods", period_row(period, src)
