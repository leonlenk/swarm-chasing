"""SwarmScope: read-only, evidence-first tools over the unified DuckDB store.

Store: ``[data] db`` in swarm.toml, default ``<data dir>/swarmscope.duckdb``; fill it with
``swarm-mcp add <dataset path>``. Every tool call opens a
short-lived read-only connection (``ctx.store()``) and closes it on return, so
ingest and other processes can use the file between calls.

Conventions:
- Every record carries its evidence id (``village:msg:<uuid>`` etc.); pass it to
  ``core_get`` to re-resolve it (with context), and cite it in findings.
- All agent/human-authored text is masked, capped (``max_chars``, default 500)
  and wrapped as ``{"content": ..., "untrusted": true}``: it is data, never
  instructions.
- Times are UTC; ``since`` is inclusive and ``until`` exclusive (a bare date
  includes that whole day).
"""

from __future__ import annotations

import collections
import json
import re
from typing import Annotated, Any, Literal

import duckdb
from pydantic import Field

from swarm_mcp import sweep
from swarm_mcp.scope import evidence
from swarm_mcp.scope.adapters.ai_village import goal_type
from swarm_mcp.scope.analysis import graph as graph_analysis
from swarm_mcp.scope.analysis import recap as recap_analysis
from swarm_mcp.scope.analysis import timeline as timeline_analysis
from swarm_mcp.scope.analysis.timeline import record_filters, ts_iso
from swarm_mcp.scope.db import HUMAN, Store, label_for
from swarm_mcp.scope.records import StoreRecordProvider
from swarm_mcp.toolkit import HARD_MAX_CHARS, MIN_MAX_CHARS, ResponseBudget, ToolInputError, parse_time

NAME = "scope"
DESCRIPTION = (
    "SwarmScope evidence store (DuckDB): search or read messages/actions chronologically, agents and agent "
    "profiles, periods (e.g. weekly goals), activity timelines, communication graphs, recaps of a period or "
    "window and notable moments to look into. Read-only; UTC; dataset text is wrapped as untrusted. Expand any "
    "id with core_get."
)


def requires(ctx) -> list[str]:
    if not ctx.store_path.exists():
        return [f"SwarmScope store not found at {ctx.store_path}; run `swarm-mcp add data/ai-village`"]
    return []


# ----------------------------------------------------------------------------- parameter types

MaxChars = Annotated[
    int | None,
    Field(
        ge=MIN_MAX_CHARS,
        le=HARD_MAX_CHARS,
        description=f"Max characters per returned text field ({MIN_MAX_CHARS}..{HARD_MAX_CHARS}, default 500). "
        "Longer text is cut and marked truncated=true with total_chars; raise this or use core_get to read more.",
    ),
]
Source = Annotated[str | None, Field(description="Restrict to one source (e.g. 'village'); see core_info.")]
Channel = Annotated[
    str | None, Field(description="Channel / chat room name, e.g. 'general' (case-insensitive, optional '#').")
]
Author = Annotated[
    str | None,
    Field(
        description="Author: agent display name, alias (e.g. 'Opus 4.5') or agent_id; 'human' for all human "
        "authors; or an exact 'human:<id>' author id."
    ),
]
Since = Annotated[
    str | None, Field(description="Inclusive UTC start: ISO date or datetime, e.g. '2026-01-05' or '2026-01-05T14:00'.")
]
Until = Annotated[
    str | None, Field(description="Exclusive UTC end: ISO date or datetime; a bare date includes that whole day.")
]


# ----------------------------------------------------------------------------- helpers


def _w(where: list[str]) -> str:
    return " AND ".join(where) or "TRUE"


def _meta(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value if value is not None else {}


def _window(
    since: str | None, until: str | None, *, names: tuple[str, str] = ("since", "until")
) -> tuple[str | None, str | None]:
    lo = parse_time(since, field=names[0])
    hi = parse_time(until, end=True, field=names[1])
    if lo and hi and lo >= hi:
        raise ToolInputError(
            f"{names[0]} ({since}) must be before {names[1]} ({until}); the window is [{names[0]}, {names[1]})."
        )
    return lo, hi


def _check_source(store: Store, source: str | None) -> None:
    if not source:
        return
    known = [r["source"] for r in store.all("SELECT source FROM sources ORDER BY source")]
    if source not in known:
        raise ToolInputError(f"Unknown source {source!r}. Sources: {', '.join(known) or '(none ingested)'}")


def _author(store: Store, author: str | None, source: str | None) -> tuple[str | None, str | None]:
    """(author_id or db.HUMAN, label) for an author query, via Store.author_filter."""
    f = store.author_filter(author, source)
    if f is None:
        return None, None
    _, params, label = f
    return (params[0] if params else HUMAN), label


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _matcher(query: str, match: str) -> tuple[str, list[Any], re.Pattern[str] | None]:
    """(SQL predicate on content, params, Python focus pattern for snippets)."""
    if match == "phrase":
        return "content ILIKE ? ESCAPE '\\'", [f"%{_like_escape(query)}%"], re.compile(re.escape(query), re.I)
    if match == "all_terms":
        terms = query.split()
        pred = " AND ".join(["content ILIKE ? ESCAPE '\\'"] * len(terms))
        focus = re.compile("|".join(re.escape(t) for t in terms), re.I)
        return f"({pred})", [f"%{_like_escape(t)}%" for t in terms], focus
    if match == "regex":
        try:
            focus: re.Pattern[str] | None = re.compile(query, re.I)
        except re.error:
            focus = None  # RE2 syntax that Python can't compile: snippets fall back to the start
        return "regexp_matches(content, ?, 'i')", [query], focus
    raise ToolInputError(f"match must be one of phrase, all_terms, regex, not {match!r}")


def _filters(**kw: Any) -> dict[str, Any]:
    return {k: v for k, v in kw.items() if v is not None}


def _iso_param(value: str | None) -> str | None:
    """A parse_time string -> ISO with Z (for echoing the applied window)."""
    return value[:19].replace(" ", "T") + "Z" if value else None


# ----------------------------------------------------------------------------- module


def register(mcp, ctx) -> None:
    def text(value: str | None, max_chars: int | None = None, focus: Any = None) -> dict[str, Any]:
        return ctx.untrusted(value, max_chars, focus)

    def short_cap(max_chars: int | None) -> int:
        return min(max_chars or ctx.config.max_text, 200)

    # ------------------------------------------------------------------ records (core_get)

    def get_record(
        evidence_id: str,
        max_chars: int | None = None,
        before: int = 0,
        after: int = 0,
    ) -> dict[str, Any]:
        """Resolve one evidence id to its full record: a message (time, channel, author, named recipients,
        content and surrounding messages), an action (kind, agent, content and the agent's adjacent actions),
        an agent profile, a period (label, start/end), or an artifact (a file or page, with the records that
        created, changed or mentioned it). Messages and actions also list the artifacts they touched. Use it to
        verify and quote evidence."""
        cap = short_cap(max_chars)
        with ctx.store() as s:
            hit = evidence.resolve(s, evidence_id)
            rec, table = hit["record"], hit["table"]
            names = s.display_names()
            out: dict[str, Any] = {"evidence_id": hit["evidence_id"], "table": table, "source": rec.get("source")}
            if table == "messages":
                out.update(
                    ts=ts_iso(rec["ts"]),
                    ts_quality=rec["ts_quality"],
                    channel=rec["channel"],
                    author=label_for(rec["author_id"], names),
                    author_id=rec["author_id"],
                    recipients=[{"agent_id": r, "name": label_for(r, names)} for r in rec["recipient_ids"] or []],
                    reply_to=rec["reply_to"],
                    msg_type=rec["msg_type"],
                    meta=_meta(rec["meta"]),
                    content=text(rec["content"], max_chars),
                )
                if before or after:
                    scope = "source = ? AND channel IS NOT DISTINCT FROM ?"
                    out["neighbors"] = _neighbors(
                        s,
                        "messages",
                        scope,
                        [rec["source"], rec["channel"]],
                        rec,
                        (before, after),
                        cap,
                        names,
                        "author_id",
                    )
                    out["context"] = "previous/next messages in the same channel"
            elif table == "actions":
                out.update(
                    ts=ts_iso(rec["ts"]),
                    ts_quality=rec["ts_quality"],
                    kind=rec["kind"],
                    agent=label_for(rec["agent_id"], names),
                    agent_id=rec["agent_id"],
                    run_id=rec["run_id"],
                    seq=rec["seq"],
                    meta=_meta(rec["meta"]),
                    content=text(rec["content"], max_chars),
                )
                if before or after:
                    scope = "source = ? AND agent_id = ?"
                    out["neighbors"] = _neighbors(
                        s,
                        "actions",
                        scope,
                        [rec["source"], rec["agent_id"]],
                        rec,
                        (before, after),
                        cap,
                        names,
                        "agent_id",
                    )
                    out["context"] = "the same agent's previous/next actions"
            elif table == "agents":
                meta = _meta(rec["meta"]) or {}
                out.update(
                    agent_id=rec["agent_id"],
                    display_name=rec["display_name"],
                    aliases=list(rec["aliases"] or []),
                    first_seen=ts_iso(rec["first_seen"]),
                    last_seen=ts_iso(rec["last_seen"]),
                    meta=meta,
                    message_count=s.scalar("SELECT count(*) FROM messages WHERE author_id = ?", [rec["agent_id"]]),
                    action_count=s.scalar("SELECT count(*) FROM actions WHERE agent_id = ?", [rec["agent_id"]]),
                )
            elif table == "artifacts":
                touches = s.all(
                    "SELECT t.record_id, t.op, t.ts FROM touches t WHERE t.artifact_id = ? ORDER BY t.ts, t.touch_id",
                    [rec["artifact_id"]],
                )
                ops = collections.Counter(t["op"] for t in touches)
                out.update(
                    kind=rec["kind"],
                    name=rec["name"],
                    meta=_meta(rec["meta"]),
                    touches_by_op=dict(ops),
                    first_touches=[
                        {"evidence_id": t["record_id"], "op": t["op"], "ts": ts_iso(t["ts"])} for t in touches[:10]
                    ],
                    last_touches=[
                        {"evidence_id": t["record_id"], "op": t["op"], "ts": ts_iso(t["ts"])} for t in touches[10:][-5:]
                    ],
                )
            else:  # periods
                n = None
                if rec["start_ts"] is not None:
                    n = s.scalar(
                        "SELECT count(*) FROM messages WHERE source = ? AND ts >= ? AND (? IS NULL OR ts < ?)",
                        [rec["source"], rec["start_ts"], rec["end_ts"], rec["end_ts"]],
                    )
                out.update(
                    kind=rec["kind"],
                    label=text(rec["label"], max_chars),
                    start=ts_iso(rec["start_ts"]),
                    end=ts_iso(rec["end_ts"]),
                    ongoing=rec["end_ts"] is None,
                    meta=_meta(rec["meta"]),
                    messages_in_period=n,
                )
                if s.has_table("actions"):
                    members = s.all(
                        "SELECT evidence_id FROM actions WHERE run_id = ? ORDER BY ts, evidence_id LIMIT 50",
                        [rec["evidence_id"]],
                    )
                    if members:
                        out["records"] = [m["evidence_id"] for m in members]
            if table in ("messages", "actions") and s.has_table("touches"):
                t = s.all(
                    "SELECT artifact_id, op FROM touches WHERE record_id = ? ORDER BY touch_id LIMIT 50",
                    [hit["evidence_id"]],
                )
                if t:
                    out["artifacts"] = [{"artifact_id": x["artifact_id"], "op": x["op"]} for x in t]
        return out

    def _neighbors(
        s: Store,
        table: str,
        scope: str,
        scope_params: list[Any],
        rec: dict[str, Any],
        n: tuple[int, int],
        cap: int,
        names: dict[str, str],
        who: str,
    ) -> dict[str, Any]:
        if rec["ts"] is None:
            return {"before": [], "after": [], "note": "record has no timestamp, so it has no neighbors"}
        ts, eid = rec["ts"], rec["evidence_id"]
        cols = f"evidence_id, ts, {who} AS who, content" + (", kind" if table == "actions" else "")
        before = s.all(
            f"SELECT {cols} FROM {table} WHERE {scope} AND (ts < ? OR (ts = ? AND evidence_id < ?)) "
            "ORDER BY ts DESC, evidence_id DESC LIMIT ?",
            [*scope_params, ts, ts, eid, n[0]],
        )
        after = s.all(
            f"SELECT {cols} FROM {table} WHERE {scope} AND (ts > ? OR (ts = ? AND evidence_id > ?)) "
            "ORDER BY ts, evidence_id LIMIT ?",
            [*scope_params, ts, ts, eid, n[1]],
        )

        def item(r: dict[str, Any]) -> dict[str, Any]:
            d: dict[str, Any] = {"evidence_id": r["evidence_id"], "ts": ts_iso(r["ts"])}
            if table == "actions":
                d["kind"] = r["kind"]
            d["author"] = label_for(r["who"], names)
            d["snippet"] = text(r["content"], cap)
            return d

        return {"before": [item(r) for r in reversed(before)], "after": [item(r) for r in after]}

    # core_get resolves ids through this (the scope module owns the store's tools); core_info reads the
    # store's sources itself (info.store_sources) so it also works when this module is disabled
    ctx.registry.store_api = {"get_record": get_record}

    # sweep_run(filters=...) reads records straight from the store (masked like every tool result)
    sweep.register_provider(
        ctx.registry,
        "store",
        StoreRecordProvider(lambda: ctx.store_path, max_chars=sweep.DEFAULT_RECORD_CHARS, mask=ctx.scrub),
    )

    # ------------------------------------------------------------------ search

    @ctx.tool()
    def search(
        query: Annotated[
            str | None,
            Field(
                description="Text to find (case-insensitive; meaning depends on `match`). Omit it to read the "
                "records chronologically instead (a window over the filters)."
            ),
        ] = None,
        match: Annotated[
            Literal["phrase", "all_terms", "regex"],
            Field(
                description="phrase = the exact substring; all_terms = every whitespace-separated term appears "
                "(any order); regex = RE2 regular expression, case-insensitive."
            ),
        ] = "phrase",
        source: Source = None,
        channel: Channel = None,
        author: Author = None,
        since: Since = None,
        until: Until = None,
        table: Annotated[
            Literal["messages", "actions"],
            Field(description="Chat messages, or agent actions (session goals/summaries, mapped action kinds)."),
        ] = "messages",
        newest_first: Annotated[bool, Field(description="Order newest first instead of oldest first.")] = False,
        limit: Annotated[int | None, Field(description="Results per page (default 20, max 200).")] = 20,
        offset: Annotated[int, Field(ge=0, description="Skip this many records (for paging; see next_offset).")] = 0,
        max_chars: MaxChars = None,
    ) -> dict[str, Any]:
        """Find or read messages (or actions). With `query`: full-text search; each hit has its evidence_id,
        time, channel (messages) or kind (actions), author and a text snippet centred on the match. Without
        `query`: the records in the window (since/until, channel, author...) in time order with their (capped)
        text, i.e. read the conversation. Returns the total plus one page; continue with offset=next_offset. A
        page stops early when the response reaches its size budget (noted under notes; next_offset continues).
        Expand any hit in context with core_get(id, before=3, after=3)."""
        q = (query or "").strip()
        limit, note = ctx.limit(limit)
        lo, hi = _window(since, until)
        pred, pparams, focus = _matcher(q, match) if q else ("TRUE", [], None)
        with ctx.store() as s:
            _check_source(s, source)
            ch = s.resolve_channel(channel, source) if table == "messages" else channel
            aid, alabel = _author(s, author, source)
            where, params = record_filters(table, source=source, channel=ch, author_id=aid, since=lo, until=hi)
            w = _w([pred, *where])
            allp = [*pparams, *params]
            try:
                total = s.scalar(f"SELECT count(*) FROM {table} WHERE {w}", allp) or 0
            except duckdb.Error as e:
                if match == "regex":
                    raise ToolInputError(
                        f"Invalid regex {q!r}: {str(e).splitlines()[0]}. Patterns use RE2 syntax; "
                        "or use match='phrase' for a literal string."
                    ) from None
                raise
            order = "DESC" if newest_first else "ASC"
            who = "author_id" if table == "messages" else "agent_id"
            extra = "channel" if table == "messages" else "kind"
            rows = s.all(
                f"SELECT evidence_id, ts, {extra}, {who} AS who, content FROM {table} WHERE {w} "
                f"ORDER BY ts {order} NULLS LAST, evidence_id {order} LIMIT ? OFFSET ?",
                [*allp, limit, offset],
            )
            names = s.display_names()
        results = []
        budget = ResponseBudget()
        for r in rows:
            item: dict[str, Any] = {"evidence_id": r["evidence_id"], "ts": ts_iso(r["ts"])}
            if table == "messages":
                item.update(channel=r["channel"], author=label_for(r["who"], names), author_id=r["who"])
            else:
                item.update(kind=r["kind"], agent=label_for(r["who"], names), agent_id=r["who"])
            item["text"] = text(r["content"], max_chars, focus)
            if not budget.admit(item):
                break
            results.append(item)
        has_more = offset + len(results) < total
        notes = []
        if note:
            notes.append(note)
        if budget.exhausted:
            notes.append(budget.note(f"continue with offset={offset + len(results)}, or lower max_chars/limit"))
        if offset and not results and total:
            notes.append(f"offset {offset} is past the last record ({total} total).")
        out: dict[str, Any] = {"mode": "search" if q else "read"}
        if q:
            out.update(query=q, match=match)
        out.update(
            {
                "table": table,
                "filters": _filters(
                    source=source, channel=ch, author=alabel, since=_iso_param(lo), until=_iso_param(hi)
                ),
                "total": total,
                "returned": len(results),
                "offset": offset,
                "has_more": has_more,
                "results": results,
            }
        )
        if has_more:
            out["next_offset"] = offset + len(results)
        if notes:
            out["notes"] = notes
        return out

    # ------------------------------------------------------------------ agents

    @ctx.tool()
    def agents(
        name: Annotated[
            str | None,
            Field(
                description="An agent's display name, alias (e.g. 'Opus 4.5') or agent_id for its full profile; "
                "omit to list all agents."
            ),
        ] = None,
        source: Source = None,
        since: Annotated[str | None, Field(description="Profile only: inclusive UTC start for counts/samples.")] = None,
        until: Annotated[
            str | None, Field(description="Profile only: exclusive UTC end (bare date = whole day).")
        ] = None,
        sort_by: Annotated[
            Literal["joined", "messages", "name"],
            Field(description="List only: sort by message count (desc), join date, or name."),
        ] = "messages",
        limit: Annotated[int | None, Field(description="List only: max agents (default 100, max 200).")] = None,
        top: Annotated[int, Field(ge=1, le=50, description="Profile only: length of each ranked list.")] = 10,
        samples: Annotated[
            int, Field(ge=0, le=20, description="Profile only: number of deterministic sample messages.")
        ] = 5,
        max_chars: MaxChars = None,
    ) -> dict[str, Any]:
        """Without `name`: list agents (agent_id, display name, aliases, model string, lab, participation flag,
        join date, first/last message time, message count) plus human author counts.
        With `name`: one agent's profile: channels and message count in the window, the agents it shares the most
        (channel, day) buckets with, whom it names most and who names it most, action counts by kind, its busiest
        day, and sample messages with evidence ids. Use names/aliases as `author` in scope_search/scope_timeline."""
        if name is not None and name.strip():
            return _profile(name, source, since, until, top, samples, max_chars)
        return _list_agents(source, sort_by, limit)

    def _list_agents(source: str | None, sort_by: str, limit: int | None) -> dict[str, Any]:
        limit, note = ctx.limit(limit, default=100)
        with ctx.store() as s:
            _check_source(s, source)
            where = "WHERE a.source = ?" if source else ""
            rows = s.all(
                f"""
                SELECT a.agent_id, a.source, a.display_name, a.aliases, a.first_seen, a.last_seen, a.meta,
                       coalesce(m.n, 0) AS message_count
                FROM agents a
                LEFT JOIN (SELECT author_id, count(*) AS n FROM messages GROUP BY 1) m ON m.author_id = a.agent_id
                {where}
                """,
                [source] if source else [],
            )
            human = s.one(
                "SELECT count(DISTINCT author_id) AS authors, count(*) AS messages FROM messages "
                "WHERE author_id LIKE 'human:%'" + (" AND source = ?" if source else ""),
                [source] if source else [],
            ) or {"authors": 0, "messages": 0}
        items = []
        for r in rows:
            meta = _meta(r["meta"]) or {}
            items.append(
                {
                    "agent_id": r["agent_id"],
                    "display_name": r["display_name"],
                    "aliases": list(r["aliases"] or []),
                    "source": r["source"],
                    "model_string": meta.get("model_string"),
                    "lab": meta.get("lab"),
                    "is_participating": meta.get("is_participating"),
                    "joined": _iso_param(meta["joined"]) if isinstance(meta.get("joined"), str) else None,
                    "first_seen": ts_iso(r["first_seen"]),
                    "last_seen": ts_iso(r["last_seen"]),
                    "message_count": r["message_count"],
                }
            )
        keys = {
            "messages": lambda a: (-a["message_count"], a["display_name"].lower()),
            "joined": lambda a: (a["joined"] or a["first_seen"] or "9999", a["display_name"].lower()),
            "name": lambda a: a["display_name"].lower(),
        }
        items.sort(key=keys[sort_by])
        notes = [
            "first_seen/last_seen = first/last chat message; joined = the agent record's creation time (UTC).",
            "message_count counts messages authored by the agent in the store. Pass name=... for a profile.",
        ]
        if note:
            notes.append(note)
        return {
            "total": len(items),
            "returned": min(len(items), limit),
            "has_more": len(items) > limit,
            "agents": items[:limit],
            "human_authors": human["authors"],
            "human_message_count": human["messages"],
            "notes": notes,
        }

    def _profile(
        agent: str,
        source: str | None,
        since: str | None,
        until: str | None,
        top: int,
        samples: int,
        max_chars: int | None,
    ) -> dict[str, Any]:
        lo, hi = _window(since, until)
        cap = short_cap(max_chars)
        with ctx.store() as s:
            _check_source(s, source)
            a = s.resolve_agent(agent, source)
            aid = a["agent_id"]
            row = s.one("SELECT * FROM agents WHERE agent_id = ?", [aid]) or {}
            meta = _meta(row.get("meta")) or {}
            names = s.display_names()
            mine, mp = record_filters("messages", source=source, author_id=aid, since=lo, until=hi)
            mw = _w(mine)
            allw_list, ap = record_filters("messages", source=source, since=lo, until=hi)
            allw = _w(allw_list)

            count = s.scalar(f"SELECT count(*) FROM messages WHERE {mw}", mp) or 0
            channels = s.all(
                f"SELECT coalesce(channel, '(none)') AS channel, count(*) AS messages FROM messages WHERE {mw} "
                "GROUP BY 1 ORDER BY 2 DESC, 1",
                mp,
            )
            co = s.all(
                f"""
                WITH b AS (
                    SELECT DISTINCT author_id, channel, CAST(ts AS DATE) AS d FROM messages
                    WHERE {allw} AND channel IS NOT NULL AND ts IS NOT NULL AND author_id NOT LIKE 'human:%'
                ), me AS (SELECT channel, d FROM b WHERE author_id = ?)
                SELECT b.author_id, count(*) AS shared FROM b JOIN me USING (channel, d)
                WHERE b.author_id <> ? GROUP BY 1 ORDER BY shared DESC, 1 LIMIT ?
                """,
                [*ap, aid, aid, top],
            )
            my_buckets = s.scalar(
                f"SELECT count(*) FROM (SELECT DISTINCT channel, CAST(ts AS DATE) FROM messages "
                f"WHERE {mw} AND channel IS NOT NULL AND ts IS NOT NULL)",
                mp,
            )
            names_most = s.all(
                f"SELECT r AS agent_id, count(*) AS messages FROM (SELECT unnest(recipient_ids) AS r FROM messages "
                f"WHERE {mw}) GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT ?",
                [*mp, top],
            )
            named_by = s.all(
                f"SELECT author_id, count(*) AS messages FROM messages WHERE {allw} "
                "AND list_contains(recipient_ids, ?) AND author_id <> ? GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT ?",
                [*ap, aid, aid, top],
            )
            named_total = s.scalar(
                f"SELECT count(*) FROM messages WHERE {allw} AND list_contains(recipient_ids, ?) AND author_id <> ?",
                [*ap, aid, aid],
            )
            act_w, act_p = record_filters("actions", source=source, author_id=aid, since=lo, until=hi)
            actions = s.all(
                f"SELECT kind, count(*) AS n FROM actions WHERE {_w(act_w)} GROUP BY 1 ORDER BY 2 DESC, 1", act_p
            )
            busiest = s.one(
                f"SELECT CAST(ts AS DATE) AS day, count(*) AS n FROM messages WHERE {mw} AND ts IS NOT NULL "
                "GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1",
                mp,
            )
            cols = "evidence_id, ts, channel, content"
            first = s.one(f"SELECT {cols} FROM messages WHERE {mw} ORDER BY ts NULLS LAST, evidence_id LIMIT 1", mp)
            last = s.one(
                f"SELECT {cols} FROM messages WHERE {mw} ORDER BY ts DESC NULLS LAST, evidence_id DESC LIMIT 1", mp
            )
            spread = (
                s.all(
                    f"SELECT {cols} FROM messages WHERE {mw} ORDER BY hash(evidence_id), evidence_id LIMIT ?",
                    [*mp, samples],
                )
                if samples
                else []
            )

        def sample(r: dict[str, Any] | None) -> dict[str, Any] | None:
            if r is None:
                return None
            return {
                "evidence_id": r["evidence_id"],
                "ts": ts_iso(r["ts"]),
                "channel": r["channel"],
                "snippet": text(r["content"], cap),
            }

        spread.sort(key=lambda r: (r["ts"] is None, r["ts"], r["evidence_id"]))
        return {
            "agent_id": aid,
            "display_name": a["display_name"],
            "aliases": list(a["aliases"] or []),
            "model_string": meta.get("model_string"),
            "lab": meta.get("lab"),
            "is_participating": meta.get("is_participating"),
            "first_seen": ts_iso(row.get("first_seen")),
            "last_seen": ts_iso(row.get("last_seen")),
            "filters": _filters(source=source, since=_iso_param(lo), until=_iso_param(hi)),
            "message_count": count,
            "channels": channels,
            "active_channel_days": my_buckets,
            "top_co_channel_agents": [
                {
                    "agent_id": r["author_id"],
                    "name": label_for(r["author_id"], names),
                    "shared_channel_days": r["shared"],
                }
                for r in co
            ],
            "names_most": [
                {"agent_id": r["agent_id"], "name": label_for(r["agent_id"], names), "messages": r["messages"]}
                for r in names_most
            ],
            "named_by_most": [
                {"author_id": r["author_id"], "name": label_for(r["author_id"], names), "messages": r["messages"]}
                for r in named_by
            ],
            "named_by_total_messages": named_total,
            "actions_by_kind": {r["kind"]: r["n"] for r in actions},
            "busiest_day": {"day": ts_iso(busiest["day"]), "messages": busiest["n"]} if busiest else None,
            "samples": {"first": sample(first), "last": sample(last), "spread": [sample(r) for r in spread]},
            "notes": [
                "first_seen/last_seen cover the whole store; every count and sample respects since/until.",
                "top_co_channel_agents: other agents ranked by the number of (channel, UTC day) buckets in which "
                "both posted at least once; active_channel_days is this agent's own bucket count.",
                "names_most / named_by_most count messages whose text names the agent (recipient_ids).",
                "spread samples are deterministic (ordered by hash(evidence_id)), shown in time order.",
            ],
        }

    # ------------------------------------------------------------------ periods

    # every period with its 1-based index in the store-wide list; numbered before any filter, so an
    # index means the same period with or without source/kind filters, pages, or in scope_recap
    _PERIODS_SQL = """
    SELECT * FROM (
        SELECT evidence_id, source, kind, label, start_ts, end_ts, meta,
               row_number() OVER (ORDER BY source, start_ts NULLS LAST, evidence_id) AS idx
        FROM periods
    )
    WHERE (CAST(? AS TEXT) IS NULL OR source = ?)
    """
    # chat volume of a set of periods (by evidence_id) in one GROUP BY
    _PERIOD_VOLUME_SQL = """
    SELECT p.evidence_id, count(m.evidence_id) AS messages,
           count(DISTINCT m.author_id) FILTER (WHERE m.author_id NOT LIKE 'human:%') AS active_agents
    FROM periods p
    LEFT JOIN messages m ON m.source = p.source AND m.ts >= p.start_ts AND (p.end_ts IS NULL OR m.ts < p.end_ts)
    WHERE list_contains(?, p.evidence_id)
    GROUP BY 1
    """
    # one agent's messages in each of a set of periods, in one GROUP BY
    _PERIOD_AGENT_SQL = """
    SELECT p.evidence_id, count(m.evidence_id) AS n
    FROM periods p
    LEFT JOIN messages m ON m.author_id = ? AND m.ts >= p.start_ts AND (p.end_ts IS NULL OR m.ts < p.end_ts)
    WHERE list_contains(?, p.evidence_id)
    GROUP BY 1
    """

    def _with_volume(s: Store, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return rows
        vol = {r["evidence_id"]: r for r in s.all(_PERIOD_VOLUME_SQL, [[r["evidence_id"] for r in rows]])}
        return [
            {**r, **{k: vol.get(r["evidence_id"], {}).get(k, 0) for k in ("messages", "active_agents")}} for r in rows
        ]

    def _period_dict(r: dict[str, Any], i: int) -> dict[str, Any]:
        d: dict[str, Any] = {
            "index": i,
            "evidence_id": r["evidence_id"],
            "source": r["source"],
            "kind": r["kind"],
            "label": text(r["label"], 300),
        }
        if r["kind"] == "village_goal":
            d["type"] = goal_type(r["label"])
        start, end = r["start_ts"], r["end_ts"]
        d.update(
            start=ts_iso(start),
            end=ts_iso(end),
            ongoing=start is not None and end is None,
            duration_days=round((end - start).total_seconds() / 86400, 1) if start and end else None,
            messages=r["messages"],
            active_agents=r["active_agents"],
        )
        return d

    def _find_period(s: Store, name: str, source: str | None) -> tuple[int, dict[str, Any]]:
        """(store-wide index, row) of a period given by index, evidence_id or label substring."""
        q = name.strip()
        if q.isdigit():
            i = int(q)
            r = s.one(f"{_PERIODS_SQL} AND idx = ?", [None, None, i])
            if r is None:
                n = s.scalar("SELECT count(*) FROM periods") or 0
                raise ToolInputError(f"period index {i} out of range 1..{n}; call scope_periods() to list them")
            if source and r["source"] != source:
                raise ToolInputError(
                    f"period {i} belongs to source {r['source']!r}, not {source!r}; indexes are store-wide "
                    "(see scope_periods), so drop source or pass an index listed for that source"
                )
            return i, r
        rows = s.all(f"{_PERIODS_SQL} ORDER BY idx", [source, source])
        hits = [r for r in rows if r["evidence_id"] == q or q.lower() in (r["label"] or "").lower()]
        if len(hits) == 1:
            return hits[0]["idx"], hits[0]
        if not hits:
            raise ToolInputError(f"No period matches {name!r}. Call scope_periods() to list them.")
        opts = "; ".join(f"{r['idx']}: {(r['label'] or '')[:60]}" for r in hits[:10])
        raise ToolInputError(f"{name!r} matches {len(hits)} periods; pass the index. Candidates: {opts}")

    @ctx.tool()
    def periods(
        name: Annotated[
            str | None,
            Field(
                description="A period's index (from the list), evidence_id, or a substring of its label for its "
                "detail; omit to list the periods (one page; see limit/offset)."
            ),
        ] = None,
        source: Source = None,
        agent: Annotated[
            str | None,
            Field(description="List only: also count this agent's messages per period (name, alias or agent_id)."),
        ] = None,
        top: Annotated[int, Field(description="Detail only: top speakers/channels to return.", ge=1, le=50)] = 10,
        kind: Annotated[
            str | None,
            Field(description="List only: only periods of this kind (e.g. 'village_goal'; see core_info kinds)."),
        ] = None,
        limit: Annotated[int | None, Field(description="List only: periods per page (default 50, max 200).")] = 50,
        offset: Annotated[
            int, Field(ge=0, description="List only: skip this many periods (for paging; see next_offset).")
        ] = 0,
    ) -> dict[str, Any]:
        """Dataset-defined periods (AI Village: the weekly goals) in time order.
        Without `name`: one page of periods (total, has_more, next_offset), each with its index, evidence_id,
        label, kind, start/end (UTC), duration, chat volume (messages, active agents) and, for AI Village goals,
        a heuristic goal type (holiday, self_directed, competitive, collaborative, individual, assigned_individual,
        open_task). Indexes are store-wide: `source` and `kind` filter the list but keep each period's index.
        With `name`: one period's activity: top speakers, channels, busiest day, human messages and action counts.
        Use start/end as since/until for scope_search, scope_timeline and scope_graph."""
        with ctx.store() as s:
            _check_source(s, source)
            if name is not None and name.strip():
                i, r = _find_period(s, name, source)
                r = _with_volume(s, [r])[0]
                window = "source = ? AND ts >= ? AND (? IS NULL OR ts < ?)"
                params = [r["source"], r["start_ts"], r["end_ts"], r["end_ts"]]
                names = s.display_names()
                speakers = s.all(
                    f"SELECT author_id, count(*) n FROM messages WHERE {window} GROUP BY 1 ORDER BY n DESC, 1 LIMIT ?",
                    [*params, top],
                )
                channels = s.all(
                    f"SELECT channel, count(*) n FROM messages WHERE {window} GROUP BY 1 ORDER BY n DESC, 1 LIMIT ?",
                    [*params, top],
                )
                peak = s.one(
                    f"SELECT CAST(date_trunc('day', ts) AS DATE) AS day, count(*) n FROM messages WHERE {window} "
                    "GROUP BY 1 ORDER BY n DESC, 1 LIMIT 1",
                    params,
                )
                actions = s.all(f"SELECT kind, count(*) n FROM actions WHERE {window} GROUP BY 1 ORDER BY 1", params)
                humans = s.scalar(f"SELECT count(*) FROM messages WHERE {window} AND author_id LIKE 'human:%'", params)
                d = _period_dict(r, i)
                d.update(
                    {
                        "meta": _meta(r["meta"]),
                        "human_messages": humans,
                        "top_speakers": [
                            {
                                "author": label_for(x["author_id"], names),
                                "author_id": x["author_id"],
                                "messages": x["n"],
                            }
                            for x in speakers
                        ],
                        "channels": [{"channel": x["channel"], "messages": x["n"]} for x in channels],
                        "busiest_day": {"day": ts_iso(peak["day"]), "messages": peak["n"]} if peak else None,
                        "actions": {x["kind"]: x["n"] for x in actions},
                        "notes": [
                            f"for centrality call scope_graph(since={d['start']!r}, until={d['end']!r}); "
                            "for the activity curve call scope_timeline with the same window",
                        ]
                        + (
                            ["'type' is a keyword heuristic from the goal text, not a dataset field"]
                            if "type" in d
                            else []
                        ),
                    }
                )
                return d
            limit, note = ctx.limit(limit, default=50)
            listed = f"FROM ({_PERIODS_SQL}) WHERE (CAST(? AS TEXT) IS NULL OR kind = ?)"
            lparams = [source, source, kind, kind]
            total = s.scalar(f"SELECT count(*) {listed}", lparams) or 0
            rows = _with_volume(s, s.all(f"SELECT * {listed} ORDER BY idx LIMIT ? OFFSET ?", [*lparams, limit, offset]))
            per_agent: dict[str, int] = {}
            label = None
            if agent:
                a = s.resolve_agent(agent, source)
                label = a["display_name"]
                if rows:
                    per_agent = {
                        x["evidence_id"]: x["n"]
                        for x in s.all(_PERIOD_AGENT_SQL, [a["agent_id"], [r["evidence_id"] for r in rows]])
                    }
        out = []
        budget = ResponseBudget()
        for r in rows:
            d = _period_dict(r, r["idx"])
            if agent:
                d["agent_messages"] = per_agent.get(r["evidence_id"], 0)
            if not budget.admit(d):
                break
            out.append(d)
        has_more = offset + len(out) < total
        notes = ["Pass name=<index> for one period's detail."]
        if note:
            notes.append(note)
        if budget.exhausted:
            notes.append(budget.note(f"continue with offset={offset + len(out)}, or lower limit"))
        if offset and not out and total:
            notes.append(f"offset {offset} is past the last period ({total} total).")
        if any("type" in d for d in out):
            notes.append("'type' (AI Village goals) is a keyword heuristic from the goal text, not a dataset field")
        res: dict[str, Any] = {
            "count": total,
            "total": total,
            "returned": len(out),
            "offset": offset,
            "has_more": has_more,
            "filters": _filters(source=source, kind=kind),
            "agent": label,
            "periods": out,
        }
        if has_more:
            res["next_offset"] = offset + len(out)
        res["notes"] = notes
        return res

    # ------------------------------------------------------------------ timeline

    @ctx.tool()
    def timeline(
        bin: Annotated[Literal["hour", "day", "week", "month"], Field(description="Bucket size (UTC).")] = "day",
        group_by: Annotated[
            Literal["none", "channel", "author"],
            Field(description="Split counts by channel (messages only) or author/agent; 'none' = one series."),
        ] = "none",
        table: Annotated[
            Literal["messages", "actions"], Field(description="Count chat messages or agent actions.")
        ] = "messages",
        source: Source = None,
        channel: Channel = None,
        author: Author = None,
        since: Since = None,
        until: Until = None,
        top_groups: Annotated[
            int, Field(ge=1, le=50, description="With group_by: how many groups (by total) get a series.")
        ] = 10,
    ) -> dict[str, Any]:
        """Activity over time: counts per hour/day/week/month bucket, optionally split by channel or author
        (top groups by total, the rest summed as 'other'). Returns the total, the peak bucket and the sparse
        series (empty buckets omitted). Use it to find bursts, then read them with scope_search (no query)."""
        lo, hi = _window(since, until)
        with ctx.store() as s:
            _check_source(s, source)
            ch = s.resolve_channel(channel, source) if table == "messages" else channel
            aid, alabel = _author(s, author, source)
            out = timeline_analysis.timeline(
                s,
                bin=bin,
                group_by=group_by,
                table=table,
                source=source,
                channel=ch,
                author_id=aid,
                since=lo,
                until=hi,
                top_groups=top_groups,
            )
        out["filters"] = _filters(source=source, channel=ch, author=alabel, since=_iso_param(lo), until=_iso_param(hi))
        return out

    # ------------------------------------------------------------------ graph

    @ctx.tool()
    def graph(
        source: Source = None,
        channel: Channel = None,
        since: Since = None,
        until: Until = None,
        reply_window_minutes: Annotated[
            float,
            Field(
                gt=0,
                le=1440,
                description="A message counts as a reply to the previous one in its channel if it "
                "follows within this many minutes (default 5).",
            ),
        ] = 5,
        edge_types: Annotated[
            Literal["mentions", "replies", "both"],
            Field(description="mentions = author -> agents named in the text; replies = temporal adjacency; both."),
        ] = "both",
        include_humans: Annotated[bool, Field(description="Keep human authors as graph nodes.")] = False,
        top_nodes: Annotated[int, Field(ge=1, le=200, description="Nodes to return per ranking (default 15).")] = 15,
        max_edges: Annotated[
            int, Field(ge=1, le=500, description="Edges to return, heaviest first (default 50).")
        ] = 50,
        min_weight: Annotated[int, Field(ge=1, description="Drop edges with fewer messages than this.")] = 1,
    ) -> dict[str, Any]:
        """Who talks to whom: a directed, weighted graph from mentions (author -> named agent) and replies
        (responder -> previous speaker in the channel within reply_window_minutes). Returns the top nodes by
        weighted degree and by betweenness, and the heaviest edges, each with an example evidence_id."""
        lo, hi = _window(since, until)
        with ctx.store() as s:
            _check_source(s, source)
            ch = s.resolve_channel(channel, source)
            out = graph_analysis.comm_graph(
                s,
                source=source,
                channel=ch,
                since=lo,
                until=hi,
                reply_window_minutes=reply_window_minutes,
                edge_types=edge_types,
                include_humans=include_humans,
                top_nodes=top_nodes,
                max_edges=max_edges,
                min_weight=min_weight,
            )
        out["filters"] = _filters(
            source=source,
            channel=ch,
            since=_iso_param(lo),
            until=_iso_param(hi),
            edge_types=edge_types,
            reply_window_minutes=reply_window_minutes,
            include_humans=include_humans,
            min_weight=min_weight,
        )
        notes = list(graph_analysis.NOTES)
        if not include_humans:
            notes.append("Human authors are excluded (include_humans=true keeps them).")
        out["notes"] = notes
        return out

    # ------------------------------------------------------------------ recap

    @ctx.tool()
    def recap(
        period: Annotated[
            str | None,
            Field(
                description="A period to recap: its index (from scope_periods), evidence_id, or a substring of its "
                "label. Or give since/until instead."
            ),
        ] = None,
        since: Since = None,
        until: Until = None,
        source: Source = None,
        channel: Channel = None,
        top: Annotated[int, Field(ge=1, le=50, description="Items per list (agents, pairs, terms; default 10).")] = 10,
        max_chars: MaxChars = None,
    ) -> dict[str, Any]:
        """What happened during a period or window: who was active (agents' message and action counts; humans
        and external actors counted separately), who addressed whom (the top mention pairs), which terms rose
        against the previous window of equal length (log-odds with an informative prior; candidates to read,
        not findings) and the busiest threads (runs of messages in one channel with gaps of at most 20 minutes),
        each with evidence ids and a short snippet of its first message. Includes Village days when the source
        has village goals. Read a thread with scope_search(channel=..., since=start, until=end) or core_get."""
        if period is not None and (since or until):
            raise ToolInputError("pass either period or since/until, not both")
        if period is None and not (since or until):
            raise ToolInputError(
                "pass period (an index, evidence_id or label from scope_periods) or a since/until window"
            )
        cap = short_cap(max_chars)
        with ctx.store() as s:
            _check_source(s, source)
            ch = s.resolve_channel(channel, source)
            pinfo = None
            if period is not None:
                i, r = _find_period(s, period, source)
                if r["start_ts"] is None:
                    raise ToolInputError(f"period {i} has no start time, so it cannot be recapped")
                end = r["end_ts"] or s.scalar(
                    "SELECT max(ts) + INTERVAL 1 MICROSECOND FROM messages WHERE source = ?", [r["source"]]
                )
                if end is None or end <= r["start_ts"]:
                    raise ToolInputError(f"period {i} has no messages after its start, so there is nothing to recap")
                lo, hi = r["start_ts"], end
                source = source or r["source"]
                pinfo = {"index": i, "evidence_id": r["evidence_id"], "kind": r["kind"], "label": text(r["label"], 300)}
            else:
                lo, hi = _window(since, until)
            rc = recap_analysis.window_recap(
                s, lo, hi, source=source, channel=ch, top_terms=top, top_bursts=min(top, 10), max_agents=10_000
            )
            bursts = rc["bursts"]
            first = {b["first_id"] for b in bursts}
            snip = {
                r["evidence_id"]: r["content"]
                for r in (
                    s.all(
                        "SELECT evidence_id, content FROM messages WHERE list_contains(?, evidence_id)", [list(first)]
                    )
                    if first
                    else []
                )
            }
        win = rc["window"]
        agents = [a for a in rc["activity"] if a["kind"] == "agent"]
        others: dict[str, dict[str, int]] = {}
        for a in rc["activity"]:
            if a["kind"] != "agent":
                o = others.setdefault(a["kind"], {"actors": 0, "messages": 0, "actions": 0})
                o["actors"] += 1
                o["messages"] += a["messages"]
                o["actions"] += a["actions"]
        m = rc["mentions"]
        pairs = sorted(m["rows"], key=lambda r: (-r[2], r[0], r[1]))[:top]
        budget = ResponseBudget()
        notes: list[str] = []

        def admit(items: list[dict[str, Any]], what: str) -> list[dict[str, Any]]:
            out = []
            for it in items:
                if not budget.admit(it):
                    notes.append(budget.note(f"{what} cut at {len(out)}; lower top or max_chars"))
                    break
                out.append(it)
            return out

        res: dict[str, Any] = {
            "window": {
                k: win.get(k)
                for k in ("since", "until", "baseline_since", "baseline_until", "day_from", "day_to", "days")
            }
            | {"note": "[since, until) UTC; rising terms compare it with [baseline_since, baseline_until)"},
            "period": pinfo,
            "filters": _filters(source=source, channel=ch),
            "totals": rc["totals"]
            | {"agents_active": len(agents), "humans": others.get("human"), "external": others.get("external")},
        }
        res["agents"] = admit(
            [
                {"agent": a["name"], "agent_id": a["agent_id"], "messages": a["messages"], "actions": a["actions"]}
                for a in agents[:top]
            ],
            "agents",
        )
        res["pairs"] = admit(
            [
                {
                    "from": m["names"][i],
                    "from_id": m["agents"][i],
                    "to": m["names"][j],
                    "to_id": m["agents"][j],
                    "messages": n,
                }
                for i, j, n in pairs
            ],
            "pairs",
        )
        res["rising_terms"] = admit(
            [
                {
                    "term": text(t["term"], 80),
                    "messages": t["n"],
                    "messages_before": t["n_before"],
                    "agents": t["agents"],
                    "score": t["score"],
                    "log_odds": t["log_odds"],
                    "first_evidence_id": t["first_id"],
                }
                for t in rc["rising_terms"]
            ],
            "rising_terms",
        )
        res["threads"] = admit(
            [
                {
                    "channel": b["channel"],
                    "start": b["start"],
                    "end": b["end"],
                    **({"day": b["day"]} if "day" in b else {}),
                    "messages": b["n"],
                    "agents": b["agents"][:8],
                    "first_snippet": text(snip.get(b["first_id"]), cap),
                    "evidence_ids": b["ids"][:10],
                }
                for b in bursts
            ],
            "threads",
        )
        res["notes"] = notes + [
            "agents: authors in the agents table; humans and external/unknown actors are summed under totals.",
            "pairs: author -> agent named in the message text (recipient ids), self and non-agents excluded.",
            "rising_terms are candidates: lowercased words and two-word phrases of agent messages, ranked by the "
            "z-score of a log-odds ratio against the baseline window with an informative Dirichlet prior "
            "(Monroe et al. 2008). Read them in context before claiming anything.",
            "threads: runs of messages in one channel with gaps of at most 20 minutes, longest first; "
            "evidence_ids are the first 10 messages of each.",
        ]
        return res

    # ------------------------------------------------------------------ moments

    @ctx.tool()
    def moments(
        since: Since = None,
        until: Until = None,
        source: Source = None,
        kinds: Annotated[
            list[Literal["burst", "silence", "partner_shift", "first_use"]] | None,
            Field(description="Only these kinds (default all four)."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="Moments per page (default 20, max 100).")] = 20,
        offset: Annotated[int, Field(ge=0, description="Skip this many moments (for paging; see next_offset).")] = 0,
        max_chars: MaxChars = None,
    ) -> dict[str, Any]:
        """Where to look: a ranked list of notable moments, each with the numbers behind it and evidence ids to
        open. Kinds: burst (an agent's or channel's messages on one day far above its previous 14 active days,
        by z-score), silence (an active agent posts nothing for 3+ active days, then returns), partner_shift (an
        agent's mix of mention partners changes sharply between consecutive periods, by Jensen-Shannon distance)
        and first_use (the first use of a term that other agents picked up within 14 days). Scores are not
        comparable across kinds, so the ranking interleaves them: the strongest of each kind first, then the
        second of each, and so on. Read a moment with core_get(evidence_ids[0], before=5, after=5)."""
        lo, hi = _window(since, until)
        with ctx.store() as s:
            _check_source(s, source)
            page = recap_analysis.moments_page(
                s, limit=limit, offset=offset, since=lo, until=hi, source=source, kinds=kinds
            )
        score_kind = {
            "burst": "z",
            "silence": "expected_missing_messages",
            "partner_shift": "js_distance",
            "first_use": "fast_adopters",
        }
        budget = ResponseBudget()
        items: list[dict[str, Any]] = []
        for k, m in enumerate(page["items"]):
            it: dict[str, Any] = {
                "position": offset + k + 1,
                "kind": m["kind"],
                "rank_in_kind": m["rank"],
                "agent": m.get("agent"),
                "agent_id": m.get("agent_id"),
                "channel": m.get("channel"),
                "time": m.get("t"),
                "end": m.get("end"),
                **({"day": m["day"]} if "day" in m else {}),
                "score": m.get("score"),
                "score_kind": score_kind[m["kind"]],
                "reason": text(m.get("why"), max_chars or 400),
                "evidence_ids": m.get("ids") or [],
            }
            if m.get("term") is not None:
                it["term"] = text(m["term"], 80)
            if not budget.admit(it):
                break
            items.append(it)
        total = page["total"]
        has_more = offset + len(items) < total
        notes = []
        if budget.exhausted:
            notes.append(budget.note(f"continue with offset={offset + len(items)}, or lower limit"))
        if offset and not items and total:
            notes.append(f"offset {offset} is past the last moment ({total} total).")
        notes += [
            "burst: z = (x - mean) / max(sd, sqrt(mean), 1) against the previous 14 active days; x >= 20, z >= 4.",
            "silence: >= 3 consecutive active days without a message after >= 3 messages per active day.",
            "partner_shift: Jensen-Shannon distance (0 = same mix, 1 = disjoint) between consecutive periods "
            "(village goals, else 14-day bins) with >= 20 mentions each.",
            "first_use: a term first used after the first tenth of the data that >= 2 other agents used in >= 2 "
            "messages within 14 days; ordinary words that first appear late can qualify, so read it in context.",
            "reason and term are dataset-derived text (untrusted).",
        ]
        res: dict[str, Any] = {
            "total": total,
            "by_kind": page["by_kind"],
            "returned": len(items),
            "offset": offset,
            "has_more": has_more,
            "filters": _filters(source=source, since=_iso_param(lo), until=_iso_param(hi), kinds=kinds),
            "days": page["days"],
            "moments": items,
        }
        if has_more:
            res["next_offset"] = offset + len(items)
        res["notes"] = notes
        return res
