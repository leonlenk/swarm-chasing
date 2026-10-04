"""Lazy loaders and indexes for the AI Village gzipped-JSONL export."""

from __future__ import annotations

import bisect
import gzip
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from swarm_mcp.modules.village.names import NameMatcher

log = logging.getLogger("swarm_mcp.village")

CHAT_FILE = "chat_messages.jsonl.gz"
AGENTS_FILE = "agents.jsonl.gz"
GOALS_FILE = "village_goals.jsonl.gz"
ROOMS_FILE = "chat_rooms.jsonl.gz"


def read_jsonl_gz(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


class Msg:
    __slots__ = ("id", "ts", "speaker_type", "agent_id", "user_id", "room_id", "content")

    def __init__(self, row: dict[str, Any]):
        self.id: str = row.get("id") or ""
        self.ts: str = row.get("created_at") or ""
        self.speaker_type: str = row.get("speaker_type") or ("agent" if row.get("agent_speaker_id") else "user")
        self.agent_id: str | None = row.get("agent_speaker_id")
        self.user_id: str | None = row.get("user_speaker_id")
        self.room_id: str | None = row.get("room_id")
        self.content: str = row.get("content") or ""


@dataclass
class Meta:
    """Small tables: agents, rooms, goals (+ the name matcher)."""

    agents: list[dict[str, Any]]
    agents_by_id: dict[str, dict[str, Any]]
    rooms_by_id: dict[str, dict[str, Any]]
    goals: list[dict[str, Any]]
    matcher: NameMatcher

    def agent_name(self, aid: str | None) -> str:
        if not aid:
            return "?"
        a = self.agents_by_id.get(aid)
        return a["name"] if a else f"agent:{aid[:8]}"

    def room_name(self, rid: str | None) -> str | None:
        if not rid:
            return None
        r = self.rooms_by_id.get(rid)
        return r["name"] if r else f"room:{rid[:8]}"


def load_meta(root: Path) -> Meta:
    agents = list(read_jsonl_gz(root / AGENTS_FILE)) if (root / AGENTS_FILE).exists() else []
    rooms = list(read_jsonl_gz(root / ROOMS_FILE)) if (root / ROOMS_FILE).exists() else []
    goals = list(read_jsonl_gz(root / GOALS_FILE)) if (root / GOALS_FILE).exists() else []
    goals.sort(key=lambda g: g.get("start_time") or "")
    agents.sort(key=lambda a: a.get("created_at") or "")
    return Meta(
        agents=agents,
        agents_by_id={a["id"]: a for a in agents},
        rooms_by_id={r["id"]: r for r in rooms},
        goals=goals,
        matcher=NameMatcher((a["id"], a["name"]) for a in agents),
    )


@dataclass
class Chat:
    """All chat messages, sorted by time, with per-agent / per-room indexes."""

    msgs: list[Msg]
    ts: list[str]
    by_agent: dict[str, list[int]] = field(default_factory=dict)
    by_room: dict[str, list[int]] = field(default_factory=dict)
    human: list[int] = field(default_factory=list)

    def window(self, idxs: list[int] | None, since: str | None, until: str | None) -> list[int] | range:
        """Indices (into msgs) in [since, until), optionally restricted to ``idxs`` (sorted)."""
        if idxs is None:
            lo = bisect.bisect_left(self.ts, since) if since else 0
            hi = bisect.bisect_left(self.ts, until) if until else len(self.ts)
            return range(lo, hi)
        lo = bisect.bisect_left(idxs, bisect.bisect_left(self.ts, since)) if since else 0
        hi = bisect.bisect_left(idxs, bisect.bisect_left(self.ts, until)) if until else len(idxs)
        return idxs[lo:hi]


def load_chat(root: Path) -> Chat:
    msgs = [Msg(r) for r in read_jsonl_gz(root / CHAT_FILE)]
    msgs.sort(key=lambda m: (m.ts, m.id))
    by_agent: dict[str, list[int]] = defaultdict(list)
    by_room: dict[str, list[int]] = defaultdict(list)
    human: list[int] = []
    for i, m in enumerate(msgs):
        if m.agent_id:
            by_agent[m.agent_id].append(i)
        else:
            human.append(i)
        if m.room_id:
            by_room[m.room_id].append(i)
    log.info("village chat: %d messages", len(msgs))
    return Chat(msgs=msgs, ts=[m.ts for m in msgs], by_agent=dict(by_agent), by_room=dict(by_room), human=human)


def intersect_sorted(a: list[int], b: list[int]) -> list[int]:
    sb = set(b)
    return [x for x in a if x in sb]
