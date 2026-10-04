"""Live Claude Code sessions recorded by the swarm-live collector: sessions, the agent tree, and a timeline.

Data: the SQLite file written by the collector (``swarm-live serve``, started by the plugin's SessionStart
hook) or by ``swarm-live import``. Path: ``SWARM_LIVE_DB``, else ``$CLAUDE_PLUGIN_DATA/swarm-live.db``, else
``~/.swarm-live/swarm-live.db``. The file grows while sessions run, so every call reads it fresh (no cache).

Event ids:
    live:agent:<session_id>:<main|agent_id>   a main session or subagent; context = its parent and subagents
    live:action:<tool_use_id>                  a tool call; context = the same agent's neighbouring timeline items
    live:message:<n>                           text an agent wrote; context as for actions
    live:prompt:<n>                            a user prompt to a main session; context as for actions

This module only records and retrieves. Analysis (claims vs actions, who-helps-whom...) belongs in other modules
that cite these ids.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from swarm_mcp.events import EventNotFound, event_record
from swarm_mcp.modules.live.store import connect_ro, default_db
from swarm_mcp.toolkit import TS_FORMAT, ToolInputError, parse_time, truncate

NAME = "live"
DESCRIPTION = (
    "Claude Code sessions recorded by the swarm-live hooks (live or imported): sessions, the main-agent/subagent "
    "tree, and a chronological timeline of prompts, tool calls and agent messages as event ids (live:<kind>:<id>)."
)

Kind = Literal["prompt", "action", "message"]
KINDS: tuple[str, ...] = ("prompt", "action", "message")

# One row per timeline item: (kind, local id, time, agent). Filters are applied on top of this.
TIMELINE = """
SELECT 'prompt' AS kind, CAST(id AS TEXT) AS lid, ts, agent_key, session_id FROM events
  WHERE event = 'UserPromptSubmit'
UNION ALL SELECT 'action', id, ts, agent_key, session_id FROM actions
UNION ALL SELECT 'message', CAST(id AS TEXT), ts, agent_key, session_id FROM messages
"""


def db_path(ctx) -> Path:
    override = ctx.setting("db")
    return Path(override).expanduser() if override else default_db()


def requires(ctx) -> list[str]:
    p = db_path(ctx)
    if p.exists() or os.environ.get("CLAUDE_PLUGIN_DATA"):  # in the plugin the collector creates it on first use
        return []
    return [f"no swarm-live database at {p} (install the plugin, run `swarm-live import`, or set SWARM_LIVE_DB)"]


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: str | None, *, end: bool = False, field: str) -> float | None:
    s = parse_time(value, end=end, field=field)
    return datetime.strptime(s, TS_FORMAT).replace(tzinfo=timezone.utc).timestamp() if s else None


def _input_summary(tool: str, raw: str | None) -> str:
    """One line for a tool call: the command, path, pattern or prompt it was given."""
    try:
        inp = json.loads(raw or "{}")
    except ValueError:
        return f"{tool} {raw or ''}".strip()
    if not isinstance(inp, dict):
        return f"{tool} {inp}"
    for k in ("command", "file_path", "notebook_path", "pattern", "url", "query", "description", "prompt", "path"):
        if inp.get(k):
            return f"{tool}: {inp[k]}"
    return f"{tool} {json.dumps(inp, ensure_ascii=False)}" if inp else tool


def register(mcp, ctx) -> None:
    def conn() -> sqlite3.Connection:
        p = db_path(ctx)
        if not p.exists():
            raise ToolInputError(
                f"No swarm-live database yet at {p}. It appears once the plugin's hooks record a session "
                "(or after `swarm-live import <transcripts>`)."
            )
        return connect_ro(p)

    def agent_id(key: str) -> str:
        return ctx.event_id("agent", key)

    def agent_type(a: dict[str, Any]) -> str:
        return f"subagent:{a['agent_type'] or 'unknown'}" if a["agent_id"] else "main"

    def labels(c: sqlite3.Connection, keys: set[str]) -> dict[str, dict[str, Any]]:
        if not keys:
            return {}
        rows = c.execute(f"SELECT * FROM agents WHERE key IN ({','.join('?' * len(keys))})", list(keys))
        return {r["key"]: dict(r) for r in rows}

    # ------------------------------------------------------------------ records

    def agent_record(c: sqlite3.Connection, a: dict[str, Any], max_chars: int) -> dict[str, Any]:
        text, cut = truncate(ctx.scrub(a["task"] or ""), max_chars)
        n = c.execute(
            "SELECT COUNT(*) AS n, SUM(status='error') AS err FROM actions WHERE agent_key=?", (a["key"],)
        ).fetchone()
        return event_record(
            agent_id(a["key"]),
            time=_iso(a["started"]),
            actor=a["label"],
            actor_type=agent_type(a),
            location=a["cwd"],
            text=text,
            truncated=cut,
            session_id=a["session_id"],
            parent=agent_id(a["parent_key"]) if a["parent_key"] else None,
            status=a["status"],
            stopped=_iso(a["stopped"]),
            actions=n["n"],
            failed_actions=n["err"] or 0,
        )

    def item_records(c: sqlite3.Connection, items: list[tuple[str, str]], max_chars: int) -> list[dict[str, Any]]:
        """Records for (kind, local id) pairs, in the given order."""
        by_kind: dict[str, list[str]] = {k: [] for k in KINDS}
        for kind, lid in items:
            by_kind[kind].append(lid)
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        sql = {
            "prompt": "SELECT id, ts, agent_key, payload FROM events WHERE id IN ({})",
            "action": "SELECT * FROM actions WHERE id IN ({})",
            "message": "SELECT * FROM messages WHERE id IN ({})",
        }
        for kind, ids in by_kind.items():
            if ids:
                for r in c.execute(sql[kind].format(",".join("?" * len(ids))), ids):
                    rows[(kind, str(r["id"]))] = dict(r)
        agents = labels(c, {r["agent_key"] for r in rows.values()})
        out = []
        for kind, lid in items:
            r = rows.get((kind, lid))
            if r is None:
                continue
            a = agents.get(r["agent_key"]) or {
                "label": r["agent_key"],
                "agent_id": None,
                "agent_type": None,
                "cwd": None,
            }
            common = dict(time=_iso(r["ts"]), location=a.get("cwd"), agent=agent_id(r["agent_key"]))
            if kind == "prompt":
                p = json.loads(r["payload"] or "{}")
                text, cut = truncate(ctx.scrub(p.get("prompt") or ""), max_chars)
                out.append(
                    event_record(
                        ctx.event_id("prompt", lid),
                        actor="user",
                        actor_type="human",
                        text=text,
                        truncated=cut,
                        **common,
                    )
                )
            elif kind == "action":
                text, cut = truncate(ctx.scrub(_input_summary(r["tool"], r["input"])), max_chars)
                output, out_cut = truncate(ctx.scrub(r["output"] or ""), max_chars)
                out.append(
                    event_record(
                        ctx.event_id("action", lid),
                        actor=a["label"],
                        actor_type=agent_type(a),
                        text=text,
                        truncated=cut or out_cut,
                        tool=r["tool"],
                        status=r["status"],
                        ended=_iso(r["ts_end"]),
                        output=output or None,
                        spawned=agent_id(r["linked_agent"]) if r["linked_agent"] else None,
                        **common,
                    )
                )
            else:
                text, cut = truncate(ctx.scrub(r["text"] or ""), max_chars)
                out.append(
                    event_record(
                        ctx.event_id("message", lid),
                        actor=a["label"],
                        actor_type=agent_type(a),
                        text=text,
                        truncated=cut,
                        channel=r["source"],  # 'final' (Stop/SubagentStop) or 'transcript'
                        **common,
                    )
                )
        return out

    def resolve_agent(value: str) -> str:
        key = value.removeprefix("live:agent:")
        if ":" not in key:  # a bare session id means its main agent
            key = f"{key}:main"
        return key

    # ------------------------------------------------------------------ tools

    @ctx.tool()
    def sessions(
        cwd: Annotated[
            str | None, Field(description="Only sessions whose working directory contains this text.")
        ] = None,
        since: Annotated[
            str | None, Field(description="Only sessions active at or after this time (ISO, UTC).")
        ] = None,
        until: Annotated[str | None, Field(description="Only sessions started before this time (ISO, UTC).")] = None,
        limit: Annotated[int, Field(description="Max sessions (default 20, max 200).")] = 20,
        offset: Annotated[int, Field(description="Skip this many sessions (newest first).", ge=0)] = 0,
    ) -> dict[str, Any]:
        """List recorded Claude Code sessions, newest first: working directory, the first user prompt, start and
        last activity, number of agents (main + subagents), tool calls and failed calls, and whether any agent is
        still running. Each has the event id of its main agent; pass it to live_agents or live_timeline."""
        limit, note = ctx.limit(limit)
        lo, hi = _epoch(since, field="since"), _epoch(until, end=True, field="until")
        with closing(conn()) as c:
            rows = [
                dict(r)
                for r in c.execute(
                    """
                SELECT a.session_id, MIN(a.started) AS started, MAX(a.cwd) AS cwd, COUNT(*) AS agents,
                       MAX(CASE WHEN a.agent_id IS NULL THEN a.task END) AS task,
                       SUM(a.status = 'running') AS running,
                       (SELECT MAX(ts) FROM events e WHERE e.session_id = a.session_id) AS last_activity,
                       (SELECT COUNT(*) FROM actions x WHERE x.session_id = a.session_id) AS actions,
                       (SELECT COUNT(*) FROM actions x WHERE x.session_id = a.session_id AND x.status = 'error')
                         AS failed_actions
                FROM agents a GROUP BY a.session_id ORDER BY started DESC"""
                )
            ]
        rows = [
            r
            for r in rows
            if (not cwd or cwd.lower() in (r["cwd"] or "").lower())
            and (lo is None or (r["last_activity"] or r["started"] or 0) >= lo)
            and (hi is None or (r["started"] or 0) < hi)
        ]
        page = rows[offset : offset + limit]
        out = []
        for r in page:
            task, cut = truncate(ctx.scrub(r["task"] or ""), 300)
            out.append(
                {
                    "session_id": r["session_id"],
                    "main_agent": agent_id(f"{r['session_id']}:main"),
                    "cwd": r["cwd"],
                    "task": task,
                    "task_truncated": cut,
                    "started": _iso(r["started"]),
                    "last_activity": _iso(r["last_activity"]),
                    "agents": r["agents"],
                    "actions": r["actions"],
                    "failed_actions": r["failed_actions"],
                    "running": bool(r["running"]),
                }
            )
        return {
            "total_matches": len(rows),
            "returned": len(out),
            "has_more": offset + len(out) < len(rows),
            "sessions": out,
            "notes": [n for n in [note] if n],
        }

    @ctx.tool()
    def agents(
        session: Annotated[
            str, Field(description="A session id, or any live:agent:<session>:... event id from that session.")
        ],
    ) -> dict[str, Any]:
        """The agent tree of one session: the main agent and every subagent, each with its event id, parent,
        type, task (the prompt it was given), status, start/stop times, and tool-call counts. Subagents are linked
        to the Agent/Task call that spawned them (that call's live:action id has `spawned` set)."""
        sid = resolve_agent(session).split(":", 1)[0]
        with closing(conn()) as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM agents WHERE session_id=? ORDER BY started", (sid,))]
            if not rows:
                raise ToolInputError(f"No session {sid!r}. Use live_sessions to list sessions.")
            recs = {r["key"]: agent_record(c, r, 300) for r in rows}
        depth: dict[str, int] = {}

        def d(key: str, seen: frozenset[str] = frozenset()) -> int:
            if key not in depth:
                parent = recs[key].get("parent", "").removeprefix("live:agent:")
                depth[key] = 0 if parent not in recs or parent in seen else d(parent, seen | {key}) + 1
            return depth[key]

        for k, rec in recs.items():
            rec["depth"] = d(k)
        return {"session_id": sid, "agents": list(recs.values()), "count": len(recs)}

    @ctx.tool()
    def timeline(
        session: Annotated[
            str | None, Field(description="A session id (or live:agent id) to restrict to one session.")
        ] = None,
        agent: Annotated[
            str | None,
            Field(description="A live:agent:<session>:<id> event id to restrict to one agent (implies its session)."),
        ] = None,
        include_subagents: Annotated[
            bool, Field(description="With `agent`, also include everything its subagents (recursively) did.")
        ] = False,
        kinds: Annotated[
            list[Kind] | None, Field(description="Only these item kinds: prompt, action, message (default all).")
        ] = None,
        since: Annotated[str | None, Field(description="Start time, inclusive (ISO, UTC).")] = None,
        until: Annotated[str | None, Field(description="End time, exclusive; a bare date includes that day.")] = None,
        limit: Annotated[int, Field(description="Max items (default 50, max 200).")] = 50,
        offset: Annotated[int, Field(description="Skip this many items (oldest first).", ge=0)] = 0,
        max_chars: Annotated[
            int, Field(description="Truncate each text/output field to this many chars.", ge=80)
        ] = 300,
    ) -> dict[str, Any]:
        """Chronological timeline of user prompts, tool calls (with status ok/error/denied/pending/no_result and
        output) and agent messages, oldest first, across all sessions or narrowed to one session or agent. Every
        item is an event record with an id that core_get_event expands with its neighbours."""
        limit, note = ctx.limit(limit, default=50)
        where, args = [], []
        with closing(conn()) as c:
            if agent:
                key = resolve_agent(agent)
                keys = [key]
                if include_subagents:
                    frontier = [key]
                    while frontier:
                        kids = [
                            r["key"]
                            for r in c.execute(
                                f"SELECT key FROM agents WHERE parent_key IN ({','.join('?' * len(frontier))})",
                                frontier,
                            )
                            if r["key"] not in keys
                        ]
                        keys += kids
                        frontier = kids
                where.append(f"agent_key IN ({','.join('?' * len(keys))})")
                args += keys
            elif session:
                where.append("session_id = ?")
                args.append(resolve_agent(session).split(":", 1)[0])
            if kinds:
                where.append(f"kind IN ({','.join('?' * len(kinds))})")
                args += list(kinds)
            lo, hi = _epoch(since, field="since"), _epoch(until, end=True, field="until")
            if lo is not None:
                where.append("ts >= ?")
                args.append(lo)
            if hi is not None:
                where.append("ts < ?")
                args.append(hi)
            cond = f"WHERE {' AND '.join(where)}" if where else ""
            total = c.execute(f"SELECT COUNT(*) FROM ({TIMELINE}) {cond}", args).fetchone()[0]
            items = [
                (r["kind"], r["lid"])
                for r in c.execute(
                    f"SELECT kind, lid FROM ({TIMELINE}) {cond} ORDER BY ts, kind, lid LIMIT ? OFFSET ?",
                    [*args, limit, offset],
                )
            ]
            recs = item_records(c, items, max_chars)
        notes = [n for n in [note] if n]
        if any(r.get("truncated") for r in recs):
            notes.append("some text was truncated; raise max_chars or expand an item with core_get_event")
        return {
            "total_matches": total,
            "returned": len(recs),
            "has_more": offset + len(recs) < total,
            "next_offset": offset + len(recs) if offset + len(recs) < total else None,
            "events": recs,
            "notes": notes,
        }

    # ------------------------------------------------------------------ event ids

    @ctx.event_source(
        kinds={
            "agent": "a main session or subagent, id '<session_id>:<main|agent_id>'; text = its task; "
            "context = its parent (before) and subagents (after)",
            "action": "a tool call, id = Claude Code's tool_use_id; context = the same agent's neighbouring prompts, "
            "tool calls and messages",
            "message": "text an agent wrote (final answer or transcript text); context as for action",
            "prompt": "a user prompt to a main session; context as for action",
        },
        description="Claude Code sessions recorded by the swarm-live hooks.",
    )
    def resolve(kind: str, local_id: str, *, before: int, after: int, max_chars: int) -> dict[str, Any]:
        with closing(conn()) as c:
            if kind == "agent":
                a = c.execute("SELECT * FROM agents WHERE key=?", (local_id,)).fetchone()
                if a is None:
                    raise EventNotFound(local_id)
                a = dict(a)
                parent = (
                    c.execute("SELECT * FROM agents WHERE key=?", (a["parent_key"],)).fetchone()
                    if a["parent_key"]
                    else None
                )
                kids = c.execute(
                    "SELECT * FROM agents WHERE parent_key=? ORDER BY started LIMIT ?", (local_id, after)
                ).fetchall()
                return {
                    "event": agent_record(c, a, max_chars),
                    "before": [agent_record(c, dict(parent), max_chars)] if parent is not None and before else [],
                    "after": [agent_record(c, dict(k), max_chars) for k in kids],
                    "context": "before = parent agent, after = subagents it spawned",
                }
            row = c.execute(
                f"SELECT kind, lid, ts, agent_key FROM ({TIMELINE}) WHERE kind=? AND lid=?", (kind, local_id)
            ).fetchone()
            if row is None:
                raise EventNotFound(local_id)
            pos = (row["ts"], row["kind"], row["lid"])
            prev = c.execute(
                f"SELECT kind, lid FROM ({TIMELINE}) WHERE agent_key=? AND (ts, kind, lid) < (?, ?, ?) "
                "ORDER BY ts DESC, kind DESC, lid DESC LIMIT ?",
                (row["agent_key"], *pos, before),
            ).fetchall()
            nxt = c.execute(
                f"SELECT kind, lid FROM ({TIMELINE}) WHERE agent_key=? AND (ts, kind, lid) > (?, ?, ?) "
                "ORDER BY ts, kind, lid LIMIT ?",
                (row["agent_key"], *pos, after),
            ).fetchall()
            return {
                "event": item_records(c, [(kind, local_id)], max_chars)[0],
                "before": item_records(c, [(r["kind"], r["lid"]) for r in reversed(prev)], max_chars),
                "after": item_records(c, [(r["kind"], r["lid"]) for r in nxt], max_chars),
                "context": "previous/next prompts, tool calls and messages of the same agent",
            }
