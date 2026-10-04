"""The unified SwarmScope schema: pydantic row models plus the DuckDB DDL.

Every adapter maps its dataset onto these tables. Evidence IDs
(``{source}:{kind}:{native_id}``, see ``evidence.py``) are the primary keys of
the record tables, so any row a tool returns can be cited and re-resolved.

Tables:
  agents    one row per agent (``agent_id`` is itself an evidence id)
  messages  chat/communication records
  actions   non-chat agent activity (session goals/summaries, ...)
  periods   dataset-defined spans: time periods (AI Village: weekly goals) and episodes of work
            that group records (a pull request, a run); records point at theirs via ``run_id``
  artifacts things agents create and change: files, wiki pages, documents...
  touches   which record did what to which artifact (create / modify / delete / read / mention)
  findings  claims recorded by investigators, each citing evidence ids
  sources   one row per ingested source (adapter, path, counts, time)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TsQuality = Literal["exact", "approx", "derived", "missing"]


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Agent(_Row):
    agent_id: str  # evidence id, e.g. "village:agent:<uuid>"
    source: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class Message(_Row):
    evidence_id: str  # "village:msg:<uuid>"
    source: str
    channel: str | None = None
    author_id: str  # agent_id, or "human:<user id>"
    recipient_ids: list[str] = Field(default_factory=list)
    reply_to: str | None = None
    ts: datetime | None = None
    ts_quality: TsQuality = "exact"
    msg_type: str | None = None
    content: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class Action(_Row):
    evidence_id: str  # "village:event:<uuid>"
    source: str
    agent_id: str
    run_id: str | None = None
    seq: int | None = None
    ts: datetime | None = None
    ts_quality: TsQuality = "exact"
    kind: str
    content: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class Period(_Row):
    evidence_id: str  # "village:goal:<uuid>"
    source: str
    kind: str  # e.g. "village_goal"
    label: str
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


TouchOp = Literal["create", "modify", "delete", "read", "mention"]


class Artifact(_Row):
    artifact_id: str  # evidence id, e.g. "rpg-game:artifact:src/talents.js"
    source: str
    kind: str  # e.g. "file", "page"
    name: str
    meta: dict[str, Any] = Field(default_factory=dict)  # e.g. {"hub": true}, {"role": "test"}, page_family


class Touch(_Row):
    touch_id: str  # "<record id>|<op>|<artifact id>"
    source: str
    record_id: str  # evidence id of the message or action
    artifact_id: str
    op: TouchOp
    ts: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)  # e.g. {"added": 12, "lines": [...], "imports": [...]}


class Finding(_Row):
    finding_id: str
    created_at: datetime
    claim: str
    evidence_ids: list[str]
    confidence: Literal["low", "medium", "high"] = "medium"
    author: str = "unknown"
    status: Literal["open", "confirmed", "rejected", "retracted"] = "open"


# table name -> row model (what adapters may yield)
RECORD_MODELS: dict[str, type[_Row]] = {
    "agents": Agent,
    "messages": Message,
    "actions": Action,
    "periods": Period,
    "artifacts": Artifact,
    "touches": Touch,
}

# DuckDB column types, in table order; used for DDL and for typed bulk loads.
COLUMNS: dict[str, dict[str, str]] = {
    "agents": {
        "agent_id": "TEXT",
        "source": "TEXT",
        "display_name": "TEXT",
        "aliases": "TEXT[]",
        "first_seen": "TIMESTAMP",
        "last_seen": "TIMESTAMP",
        "meta": "JSON",
    },
    "messages": {
        "evidence_id": "TEXT",
        "source": "TEXT",
        "channel": "TEXT",
        "author_id": "TEXT",
        "recipient_ids": "TEXT[]",
        "reply_to": "TEXT",
        "ts": "TIMESTAMP",
        "ts_quality": "TEXT",
        "msg_type": "TEXT",
        "content": "TEXT",
        "meta": "JSON",
    },
    "actions": {
        "evidence_id": "TEXT",
        "source": "TEXT",
        "agent_id": "TEXT",
        "run_id": "TEXT",
        "seq": "INTEGER",
        "ts": "TIMESTAMP",
        "ts_quality": "TEXT",
        "kind": "TEXT",
        "content": "TEXT",
        "meta": "JSON",
    },
    "periods": {
        "evidence_id": "TEXT",
        "source": "TEXT",
        "kind": "TEXT",
        "label": "TEXT",
        "start_ts": "TIMESTAMP",
        "end_ts": "TIMESTAMP",
        "meta": "JSON",
    },
    "artifacts": {
        "artifact_id": "TEXT",
        "source": "TEXT",
        "kind": "TEXT",
        "name": "TEXT",
        "meta": "JSON",
    },
    "touches": {
        "touch_id": "TEXT",
        "source": "TEXT",
        "record_id": "TEXT",
        "artifact_id": "TEXT",
        "op": "TEXT",
        "ts": "TIMESTAMP",
        "meta": "JSON",
    },
    "findings": {
        "finding_id": "TEXT",
        "created_at": "TIMESTAMP",
        "claim": "TEXT",
        "evidence_ids": "TEXT[]",
        "confidence": "TEXT",
        "author": "TEXT",
        "status": "TEXT",
    },
    "sources": {
        "source": "TEXT",
        "adapter": "TEXT",
        "path": "TEXT",
        "ingested_at": "TIMESTAMP",
        "counts": "JSON",
        "meta": "JSON",
    },
}

PRIMARY_KEYS = {
    "agents": "agent_id",
    "messages": "evidence_id",
    "actions": "evidence_id",
    "periods": "evidence_id",
    "artifacts": "artifact_id",
    "touches": "touch_id",
    "findings": "finding_id",
    "sources": "source",
}

SCHEMA_VERSION = 2  # 2: artifacts + touches tables, generic 'period' kind


def ddl() -> list[str]:
    """CREATE statements for every table (idempotent)."""
    out = []
    for table, cols in COLUMNS.items():
        pk = PRIMARY_KEYS[table]
        body = ", ".join(f"{c} {t}{' PRIMARY KEY' if c == pk else ''}" for c, t in cols.items())
        out.append(f"CREATE TABLE IF NOT EXISTS {table} ({body})")
    return out
