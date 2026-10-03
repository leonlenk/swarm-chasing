"""AI Village (AI Digest): read-only access to agents, goals and chat.

Data: gzipped JSONL in ``$SWARM_DATA_DIR/ai-village/`` (override with
``SWARM_VILLAGE_DIR``). Small tables load on first use; chat (~183k rows,
~2 s) loads on the first chat tool call and is cached for the process.
agent_memories and events are intentionally not loaded.

Chat messages are event ids ``village:chat:<chat_messages.id>``; ``core_get_event`` expands them with the
neighbouring messages in the same room.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from swarm_mcp.events import EventNotFound, event_record
from swarm_mcp.modules.village.data import (
    AGENTS_FILE,
    CHAT_FILE,
    Chat,
    Meta,
    Msg,
    intersect_sorted,
    load_chat,
    load_meta,
)
from swarm_mcp.modules.village.names import HUMAN, lab_for
from swarm_mcp.toolkit import ToolInputError, iso, parse_time, truncate

NAME = "village"
DESCRIPTION = (
    "AI Village dataset (AI Digest; frontier-model agents sharing a group chat with weekly goals): "
    "agents, goals, chat search, chronological message windows, per-agent activity. Read-only; times are UTC."
)

# ----------------------------------------------------------------------------- helpers


def village_dir(ctx) -> Path:
    override = ctx.setting("dir")
    return Path(override).expanduser() if override else ctx.data_dir / "ai-village"


def requires(ctx) -> list[str]:
    d = village_dir(ctx)
    if not d.is_dir():
        return [f"AI Village dataset directory not found: {d} (set SWARM_DATA_DIR or SWARM_VILLAGE_DIR)"]
    return [f"missing {f} in {d}" for f in (CHAT_FILE, AGENTS_FILE) if not (d / f).exists()]


_GOAL_TYPES: list[tuple[str, tuple[str, ...]]] = [
    ("holiday", ("holiday", "do whatever you", "do as you please")),
    ("self_directed", ("choose your own", "pick your own", "pursue whatever", "choose a goal")),
    ("assigned_individual", ("your assigned goal",)),
    (
        "competitive",
        (
            "compete",
            "competition",
            "beat ",
            "whichever agent",
            "tournament",
            "challenge each other",
            "debate",
            "best ai assistant",
            "most profit",
            "hack the",
        ),
    ),
    (
        "collaborative",
        (
            "together",
            "collaborativ",
            "help ",
            "each other",
            "connect your worlds",
            "elect ",
            "follow your leader",
            "your leader",
            "organise an event",
            "organize an event",
        ),
    ),
    ("individual", ("each agent",)),
]


def goal_type(text: str) -> str:
    """Keyword heuristic, not ground truth (the dataset has no goal-type field)."""
    t = (text or "").lower()
    for label, keys in _GOAL_TYPES:
        if any(k in t for k in keys):
            return label
    return "open_task"


def _week_start(day: str) -> str:
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


def _notes(*items: str | None) -> list[str]:
    return [i for i in items if i]


# ----------------------------------------------------------------------------- module


def register(mcp, ctx) -> None:
    root = village_dir(ctx)

    def meta() -> Meta:
        return ctx.lazy("meta", lambda: load_meta(root))

    def chat() -> Chat:
        return ctx.lazy("chat", lambda: load_chat(root))

    def mentions() -> list[tuple[str, ...]]:
        """Per-message tuple of agent ids named in the message (self-mentions dropped)."""

        def build() -> list[tuple[str, ...]]:
            m, c = meta(), chat()
            out: list[tuple[str, ...]] = []
            for msg in c.msgs:
                ids = m.matcher.find(msg.content)
                out.append(tuple(i for i in ids if i != msg.agent_id) if ids else ())
            return out

        return ctx.lazy("mentions", build)

    def resolve_room(room: str | None) -> str | None:
        if room is None or not room.strip():
            return None
        m = meta()
        q = room.strip().lstrip("#").lower()
        if room in m.rooms_by_id:
            return room
        for rid, r in m.rooms_by_id.items():
            if (r.get("name") or "").lower() == q:
                return rid
        names = sorted({r.get("name") for r in m.rooms_by_id.values() if r.get("name")})
        raise ToolInputError(f"Unknown room {room!r}. Rooms: {', '.join(names)}")

    def candidate_indices(agent: str | None, room: str | None) -> tuple[list[int] | None, dict[str, Any]]:
        """Sorted message indices for the agent/room filters (None = all messages)."""
        m, c = meta(), chat()
        idxs: list[int] | None = None
        applied: dict[str, Any] = {}
        if agent:
            aid = m.matcher.resolve(agent)
            idxs = c.human if aid == HUMAN else c.by_agent.get(aid, [])
            applied["agent"] = "human" if aid == HUMAN else m.agent_name(aid)
        if room:
            rid = resolve_room(room)
            ridx = c.by_room.get(rid, [])
            idxs = ridx if idxs is None else intersect_sorted(idxs, ridx)
            applied["room"] = m.room_name(rid)
        return idxs, applied

    def speaker(msg: Msg, m: Meta) -> str:
        if msg.agent_id:
            return m.agent_name(msg.agent_id)
        return f"human:{(msg.user_id or '?')[:8]}"

    def chat_event_id(msg: Msg) -> str:
        return ctx.event_id("chat", msg.id)

    def msg_dict(msg: Msg, m: Meta, max_chars: int) -> dict[str, Any]:
        """A chat message as a standard event record (see swarm_mcp.events)."""
        text, cut = truncate(ctx.scrub(msg.content), max_chars)
        return event_record(
            chat_event_id(msg),
            time=iso(msg.ts),
            actor=speaker(msg, m),
            actor_type=msg.speaker_type,
            location=m.room_name(msg.room_id),
            text=text,
            truncated=cut,
        )

    def chat_index() -> dict[str, int]:
        return ctx.lazy("chat_index", lambda: {msg.id: i for i, msg in enumerate(chat().msgs)})

    # ------------------------------------------------------------------ event ids

    @ctx.event_source(
        kinds={"chat": "a chat message (chat_messages table); context = neighbouring messages in the same room"},
        description="AI Village records.",
    )
    def resolve_event(kind: str, local_id: str, *, before: int, after: int, max_chars: int) -> dict[str, Any]:
        m, c = meta(), chat()
        i = chat_index().get(local_id)
        if i is None:
            raise EventNotFound(local_id)
        msg = c.msgs[i]
        seq = c.by_room.get(msg.room_id) if msg.room_id else None
        if seq is None:  # no room: fall back to the global timeline
            seq, pos = range(len(c.msgs)), i
        else:
            pos = bisect.bisect_left(seq, i)
        return {
            "event": msg_dict(msg, m, max_chars),
            "before": [msg_dict(c.msgs[j], m, max_chars) for j in seq[max(0, pos - before) : pos]],
            "after": [msg_dict(c.msgs[j], m, max_chars) for j in seq[pos + 1 : pos + 1 + after]],
            "context": "previous/next messages in the same room" if msg.room_id else "previous/next messages overall",
        }

    # ------------------------------------------------------------------ tools

    @ctx.tool()
    def agents(
        include_departed: Annotated[
            bool, Field(description="Include agents that have left the village (is_participating=false).")
        ] = True,
        sort_by: Annotated[
            Literal["joined", "messages", "name"], Field(description="Sort order: join date, message count, or name.")
        ] = "joined",
    ) -> dict[str, Any]:
        """List AI Village agents: name, model string, lab, join date, active window (first/last chat message) and chat message count."""
        m, c = meta(), chat()
        rows = []
        for a in m.agents:
            if not include_departed and not a.get("is_participating", True):
                continue
            idxs = c.by_agent.get(a["id"], [])
            rows.append(
                {
                    "name": a["name"],
                    "model": a.get("model_string"),
                    "lab": lab_for(a.get("model_string"), a["name"]),
                    "joined": (a.get("created_at") or "")[:10] or None,
                    "participating": a.get("is_participating"),
                    "first_message": iso(c.msgs[idxs[0]].ts) if idxs else None,
                    "last_message": iso(c.msgs[idxs[-1]].ts) if idxs else None,
                    "message_count": len(idxs),
                }
            )
        key = {
            "joined": lambda r: r["joined"] or "",
            "messages": lambda r: -r["message_count"],
            "name": lambda r: r["name"].lower(),
        }
        rows.sort(key=key[sort_by])
        return {
            "count": len(rows),
            "human_message_count": len(c.human),
            "agents": rows,
            "notes": ["active window = first/last chat message; 'joined' = agent record creation date (UTC)"],
        }

    @ctx.tool()
    def goals() -> dict[str, Any]:
        """The sequence of village-wide goals with start/end times (UTC), duration and a heuristic goal type
        (holiday, self_directed, competitive, collaborative, individual, assigned_individual, open_task)."""
        m = meta()
        rows = []
        for i, g in enumerate(m.goals, 1):
            start, end = g.get("start_time"), g.get("end_time")
            dur = None
            if start and end:
                dur = round(
                    (datetime.fromisoformat(end[:19]) - datetime.fromisoformat(start[:19])).total_seconds() / 86400, 1
                )
            rows.append(
                {
                    "index": i,
                    "goal": g.get("goal"),
                    "start": iso(start),
                    "end": iso(end),
                    "ongoing": end is None,
                    "duration_days": dur,
                    "type": goal_type(g.get("goal") or ""),
                }
            )
        return {
            "count": len(rows),
            "goals": rows,
            "notes": ["'type' is a keyword heuristic from the goal text, not a dataset field"],
        }

    @ctx.tool()
    def search_chat(
        query: Annotated[str, Field(description="Text to find. Plain substring unless regex=true.")],
        agent: Annotated[
            str | None,
            Field(
                description="Only messages by this speaker: agent name, short form ('Opus 4.5', 'GPT-5.2'), id, or 'human'."
            ),
        ] = None,
        room: Annotated[str | None, Field(description="Only this chat room, by name (e.g. 'general') or id.")] = None,
        since: Annotated[
            str | None, Field(description="Inclusive lower bound, ISO date/datetime in UTC, e.g. '2026-01-05'.")
        ] = None,
        until: Annotated[
            str | None,
            Field(description="Exclusive upper bound, ISO date/datetime in UTC; a bare date includes that whole day."),
        ] = None,
        regex: Annotated[bool, Field(description="Treat query as a Python regular expression.")] = False,
        case_sensitive: Annotated[bool, Field(description="Match case exactly.")] = False,
        limit: Annotated[int, Field(description="Max results to return (default 20, max 200).")] = 20,
        offset: Annotated[int, Field(description="Skip this many matches (for paging).", ge=0)] = 0,
        newest_first: Annotated[bool, Field(description="Return the most recent matches first.")] = False,
        snippet_chars: Annotated[
            int, Field(description="Approximate snippet length around the match.", ge=40, le=2000)
        ] = 240,
    ) -> dict[str, Any]:
        """Search AI Village chat messages. Returns matches with time, speaker, room and a snippet around the
        match, plus the total match count. Emails/phone numbers in returned text are masked."""
        if not query or not query.strip():
            raise ToolInputError("query must not be empty")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pat = re.compile(query if regex else re.escape(query), flags)
        except re.error as e:
            raise ToolInputError(
                f"Invalid regex {query!r}: {e}. Set regex=false for a plain substring search."
            ) from None
        lim, note = ctx.limit(limit)
        s, u = parse_time(since, field="since"), parse_time(until, end=True, field="until")
        m, c = meta(), chat()
        idxs, applied = candidate_indices(agent, room)
        window = c.window(idxs, s, u)
        hits = [i for i in window if pat.search(c.msgs[i].content)]
        if newest_first:
            hits.reverse()
        page = hits[offset : offset + lim]
        results = []
        for i in page:
            msg = c.msgs[i]
            clean = ctx.scrub(msg.content)
            mt = pat.search(clean)
            half = snippet_chars // 2
            if mt:
                a, b = max(0, mt.start() - half), min(len(clean), mt.end() + half)
            else:  # the match was inside masked text: centre on the equivalent position
                raw = pat.search(msg.content)
                centre = int((raw.start() if raw else 0) * len(clean) / max(1, len(msg.content)))
                a, b = max(0, centre - half), min(len(clean), centre + half)
            snippet = ("…" if a > 0 else "") + clean[a:b].replace("\n", " ") + ("…" if b < len(clean) else "")
            results.append(
                {
                    "event_id": chat_event_id(msg),
                    "time": iso(msg.ts),
                    "actor": speaker(msg, m),
                    "location": m.room_name(msg.room_id),
                    "snippet": snippet,
                    "match": mt.group(0) if mt else None,
                    "text_chars": len(msg.content),
                }
            )
        if since:
            applied["since"] = iso(s)
        if until:
            applied["until"] = iso(u)
        return {
            "query": query,
            "filters": applied,
            "total_matches": len(hits),
            "returned": len(results),
            "offset": offset,
            "has_more": offset + len(results) < len(hits),
            "results": results,
            "notes": _notes(
                note,
                "snippets are trimmed; pass a result's event_id to core_get_event for the full message and its context"
                if results
                else None,
            ),
        }

    @ctx.tool()
    def messages(
        start: Annotated[str, Field(description="Inclusive start, ISO date/datetime in UTC, e.g. '2026-01-05T17:00'.")],
        end: Annotated[
            str | None,
            Field(
                description="Exclusive end, ISO date/datetime in UTC; a bare date includes that whole day. Omit for open-ended."
            ),
        ] = None,
        room: Annotated[str | None, Field(description="Only this chat room, by name (e.g. 'general') or id.")] = None,
        agent: Annotated[
            str | None, Field(description="Only messages by this speaker: agent name, short form, id, or 'human'.")
        ] = None,
        limit: Annotated[int, Field(description="Max messages (default 50, max 200).")] = 50,
        max_chars: Annotated[
            int | None,
            Field(description="Truncate each message to this many characters (default 1000).", ge=80, le=20000),
        ] = None,
    ) -> dict[str, Any]:
        """Chronological window of AI Village chat messages between start and end. If more messages exist than
        `limit`, `next_start` gives the start value for the next page."""
        s = parse_time(start, field="start")
        u = parse_time(end, end=True, field="end")
        if s and u and u <= s:
            raise ToolInputError(f"end ({end}) must be after start ({start})")
        lim, note = ctx.limit(limit, default=50)
        m, c = meta(), chat()
        idxs, applied = candidate_indices(agent, room)
        window = c.window(idxs, s, u)
        sel = window[:lim]
        width = max_chars or ctx.config.max_text
        out = [msg_dict(c.msgs[i], m, width) for i in sel]
        has_more = len(window) > lim
        truncated = sum(1 for o in out if o.get("truncated"))
        return {
            "window": {"start": iso(s), "end": iso(u)},
            "filters": applied,
            "total_in_window": len(window),
            "returned": len(out),
            "has_more": has_more,
            "next_start": iso(c.msgs[window[lim]].ts) if has_more else None,
            "messages": out,
            "notes": _notes(
                note,
                f"{truncated} message(s) truncated to {width} chars; raise max_chars for full text"
                if truncated
                else None,
                "next_start is inclusive and may repeat messages sharing that exact timestamp" if has_more else None,
            ),
        }

    @ctx.tool()
    def agent_activity(
        agent: Annotated[str, Field(description="Agent name, short form ('Opus 4.5', 'GPT-5.2') or id.")],
        bucket: Annotated[
            Literal["day", "week", "month", "goal"],
            Field(description="Time bucket for counts: day, ISO week (Monday start), month, or village goal period."),
        ] = "week",
        since: Annotated[str | None, Field(description="Inclusive lower bound, ISO date/datetime in UTC.")] = None,
        until: Annotated[str | None, Field(description="Exclusive upper bound, ISO date/datetime in UTC.")] = None,
        top: Annotated[int, Field(description="How many most-named / named-by agents to return.", ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """One agent's chat activity: message counts per time bucket, rooms used, the agents it names most in
        its messages, and the agents that name it most. Name matching uses full names plus obvious short forms."""
        m, c = meta(), chat()
        aid = m.matcher.resolve(agent)
        if aid == HUMAN:
            raise ToolInputError(
                "agent_activity needs a specific agent; use village_search_chat(agent='human') for humans"
            )
        s, u = parse_time(since, field="since"), parse_time(until, end=True, field="until")
        own = list(c.window(c.by_agent.get(aid, []), s, u))
        ment = mentions()

        goal_starts = [g.get("start_time") or "" for g in m.goals]

        def key(ts: str) -> str:
            if bucket == "day":
                return ts[:10]
            if bucket == "month":
                return ts[:7]
            if bucket == "week":
                return _week_start(ts[:10])
            gi = bisect.bisect_right(goal_starts, ts) - 1
            if gi < 0:
                return "0: (before first goal)"
            return f"{gi + 1}: {(m.goals[gi].get('goal') or '')[:70]}"

        counts: Counter[str] = Counter()
        rooms: Counter[str] = Counter()
        named: Counter[str] = Counter()
        named_total: Counter[str] = Counter()
        for i in own:
            msg = c.msgs[i]
            counts[key(msg.ts)] += 1
            rooms[m.room_name(msg.room_id) or "?"] += 1
            ids = ment[i]
            named_total.update(ids)
            named.update(set(ids))

        named_by: Counter[str] = Counter()
        for i in c.window(None, s, u):
            if aid in ment[i]:
                named_by[c.msgs[i].agent_id or HUMAN] += 1

        def label(x: str) -> str:
            return "human" if x == HUMAN else m.agent_name(x)

        buckets = [
            {"bucket": k, "messages": v}
            for k, v in sorted(counts.items(), key=lambda kv: int(kv[0].split(":")[0]) if bucket == "goal" else kv[0])
        ]
        a = m.agents_by_id.get(aid, {})
        return {
            "agent": m.agent_name(aid),
            "model": a.get("model_string"),
            "lab": lab_for(a.get("model_string"), a.get("name", "")),
            "aliases_matched": m.matcher.aliases_for(aid),
            "window": {"since": iso(s), "until": iso(u)},
            "message_count": len(own),
            "first_message": iso(c.msgs[own[0]].ts) if own else None,
            "last_message": iso(c.msgs[own[-1]].ts) if own else None,
            "bucket": bucket,
            "counts": buckets,
            "rooms": dict(rooms.most_common()),
            "names_most": [
                {"agent": label(x), "messages": n, "mentions": named_total[x]} for x, n in named.most_common(top)
            ],
            "named_by_most": [{"speaker": label(x), "messages": n} for x, n in named_by.most_common(top)],
            "notes": _notes(
                "empty buckets are omitted",
                "names_most counts messages naming each agent (self-mentions excluded); mentions = total occurrences",
            ),
        }

    # ------------------------------------------------------------------ resources

    for fname, path, desc in (
        ("README.md", "readme", "AI Village dataset card (what each file is, processing notes)."),
        ("SCHEMA.md", "schema", "Column-level schema for every AI Village table."),
        (
            "CHANGELOG.md",
            "changelog",
            "Dated scaffolding changes and agent roster; read before drawing conclusions over time.",
        ),
    ):
        if (root / fname).exists():
            ctx.resource(path, description=desc, mime_type="text/markdown")(_file_reader(root / fname, path))


def _file_reader(file: Path, name: str):
    def read() -> str:
        return file.read_text(encoding="utf-8")

    read.__name__ = f"village_{name}"
    return read
