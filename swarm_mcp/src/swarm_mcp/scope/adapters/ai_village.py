"""AI Village (AI Digest) adapter.

Input: the gzipped-JSONL export directory (see its README/SCHEMA/CHANGELOG).

Mapping:
  agents.jsonl.gz         -> agents   (agent_id = village:agent:<uuid>; model string, lab,
                                       join date in meta; first/last seen = first/last chat
                                       message; aliases = obvious short forms, e.g. "Opus 4.5")
  chat_messages.jsonl.gz  -> messages (channel = chat room name; author = agent id or
                                       human:<user id>; recipient_ids = agents named in the
                                       text, self excluded; ts_quality 'exact'; msg_type NULL)
  village_goals.jsonl.gz  -> periods  (kind 'village_goal')
  events.jsonl.gz         -> actions  (optional, streamed with a byte prefilter):
                                       START_USING_COMPUTER.sessionGoal and
                                       CONSOLIDATE.nextSessionGoal -> kind 'session_goal';
                                       STOP_USING_COMPUTER.summary -> kind 'session_summary'
agent_memories (2.4 GB) and computer-use turns are not read.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from swarm_mcp.scope.names import NameMatcher, lab_for

SOURCE = "village"
AGENTS_FILE = "agents.jsonl.gz"
CHAT_FILE = "chat_messages.jsonl.gz"
ROOMS_FILE = "chat_rooms.jsonl.gz"
GOALS_FILE = "village_goals.jsonl.gz"
EVENTS_FILE = "events.jsonl.gz"
REQUIRED = (AGENTS_FILE, CHAT_FILE)

_EVENT_KINDS = {
    "START_USING_COMPUTER": ("session_goal", "sessionGoal"),
    "CONSOLIDATE": ("session_goal", "nextSessionGoal"),
    "STOP_USING_COMPUTER": ("session_summary", "summary"),
}
_EVENT_NEEDLES = tuple(f'"{k}"'.encode() for k in _EVENT_KINDS)


def read_jsonl_gz(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def parse_ts(value: str | None) -> datetime | None:
    """Dataset timestamps are naive UTC strings like '2025-12-29 18:49:21.291984'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def agent_eid(uuid: str) -> str:
    return f"{SOURCE}:agent:{uuid}"


class AiVillageAdapter:
    name = "ai_village"
    source = SOURCE

    # ------------------------------------------------------------------ inspect
    def inspect(self, path: Path) -> dict[str, Any]:
        path = Path(path)
        if not path.is_dir():
            raise FileNotFoundError(f"AI Village directory not found: {path}")
        files = {}
        for f in sorted(path.iterdir()):
            if f.is_file():
                files[f.name] = f.stat().st_size
        out: dict[str, Any] = {
            "adapter": self.name,
            "path": str(path),
            "files": files,
            "missing_required": [f for f in REQUIRED if f not in files],
        }
        manifest = path / "manifest.json"
        if manifest.exists():
            m = json.loads(manifest.read_text())
            out["manifest_row_counts"] = m.get("rowCounts")
            out["exported_at"] = m.get("exportedAt")
        fields = {}
        for name in (AGENTS_FILE, CHAT_FILE, ROOMS_FILE, GOALS_FILE):
            if name in files:
                first = next(read_jsonl_gz(path / name), None)
                fields[name] = sorted(first) if first else []
        out["fields"] = fields
        return out

    # ------------------------------------------------------------------ load
    def load(self, path: Path, *, include_events: bool = True, **_: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        path = Path(path)
        for f in REQUIRED:
            if not (path / f).exists():
                raise FileNotFoundError(f"missing {f} in {path}")

        agents = list(read_jsonl_gz(path / AGENTS_FILE))
        rooms = {r["id"]: r for r in read_jsonl_gz(path / ROOMS_FILE)} if (path / ROOMS_FILE).exists() else {}
        matcher = NameMatcher((a["id"], a["name"]) for a in agents if a.get("id") and a.get("name"))

        # periods: village goals
        if (path / GOALS_FILE).exists():
            goals = sorted(read_jsonl_gz(path / GOALS_FILE), key=lambda g: g.get("start_time") or "")
            for i, g in enumerate(goals, 1):
                yield (
                    "periods",
                    {
                        "evidence_id": f"{SOURCE}:goal:{g['id']}",
                        "source": SOURCE,
                        "kind": "village_goal",
                        "label": g.get("goal") or "",
                        "start_ts": parse_ts(g.get("start_time")),
                        "end_ts": parse_ts(g.get("end_time")),
                        "meta": {"index": i, "native_id": g["id"]},
                    },
                )

        # messages (and first/last seen per agent)
        seen: dict[str, list[datetime]] = {}
        for r in read_jsonl_gz(path / CHAT_FILE):
            aid = r.get("agent_speaker_id")
            uid = r.get("user_speaker_id")
            author = agent_eid(aid) if aid else f"human:{uid or 'unknown'}"
            content = r.get("content") or ""
            ts = parse_ts(r.get("created_at"))
            recipients: list[str] = []
            for m in matcher.find(content):
                if m != aid:
                    e = agent_eid(m)
                    if e not in recipients:
                        recipients.append(e)
            room = rooms.get(r.get("room_id") or "")
            yield (
                "messages",
                {
                    "evidence_id": f"{SOURCE}:msg:{r['id']}",
                    "source": SOURCE,
                    "channel": (room or {}).get("name") or r.get("room_id"),
                    "author_id": author,
                    "recipient_ids": recipients,
                    "reply_to": None,
                    "ts": ts,
                    "ts_quality": "exact" if ts else "missing",
                    "msg_type": None,
                    "content": content,
                    "meta": {"room_id": r.get("room_id"), "speaker_type": r.get("speaker_type")},
                },
            )
            if aid and ts:
                s = seen.setdefault(aid, [ts, ts])
                if ts < s[0]:
                    s[0] = ts
                if ts > s[1]:
                    s[1] = ts

        # agents (after chat so first/last seen are known)
        known = set()
        for a in agents:
            known.add(a["id"])
            s = seen.get(a["id"])
            aliases = [x for x in matcher.aliases_for(a["id"]) if x != a["name"]]
            yield (
                "agents",
                {
                    "agent_id": agent_eid(a["id"]),
                    "source": SOURCE,
                    "display_name": a["name"],
                    "aliases": aliases,
                    "first_seen": s[0] if s else None,
                    "last_seen": s[1] if s else None,
                    "meta": {
                        "native_id": a["id"],
                        "model_string": a.get("model_string"),
                        "lab": lab_for(a.get("model_string"), a["name"]),
                        "joined": a.get("created_at"),
                        "is_participating": a.get("is_participating"),
                    },
                },
            )
        for aid, s in seen.items():  # speakers missing from agents.jsonl (should not happen)
            if aid not in known:
                yield (
                    "agents",
                    {
                        "agent_id": agent_eid(aid),
                        "source": SOURCE,
                        "display_name": f"agent:{aid[:8]}",
                        "aliases": [],
                        "first_seen": s[0],
                        "last_seen": s[1],
                        "meta": {"native_id": aid, "placeholder": True},
                    },
                )

        if include_events and (path / EVENTS_FILE).exists():
            yield from self._actions(path / EVENTS_FILE)

    def _actions(self, file: Path) -> Iterator[tuple[str, dict[str, Any]]]:
        """Stream events.jsonl.gz (~330 MB gz); only lines containing a wanted actionType are parsed."""
        with gzip.open(file, "rb") as f:
            for line in f:
                if not any(n in line for n in _EVENT_NEEDLES):
                    continue
                r = json.loads(line)
                d = r.get("data") or {}
                spec = _EVENT_KINDS.get(d.get("actionType"))
                agent = d.get("agentId")
                if not spec or not agent:
                    continue
                kind, field = spec
                ts = parse_ts(r.get("created_at"))
                yield (
                    "actions",
                    {
                        "evidence_id": f"{SOURCE}:event:{r['id']}",
                        "source": SOURCE,
                        "agent_id": agent_eid(agent),
                        "run_id": d.get("computerUseSessionId"),
                        "seq": r.get("event_index"),
                        "ts": ts,
                        "ts_quality": "exact" if ts else "missing",
                        "kind": kind,
                        "content": d.get(field) or "",
                        "meta": {"event_type": d.get("actionType"), "room_id": d.get("roomId")},
                    },
                )
