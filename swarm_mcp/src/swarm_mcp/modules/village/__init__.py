"""AI Village (AI Digest): village-specific views over the SwarmScope store.

Generic tools (search, records, message windows, agent profiles, timelines,
communication graphs) live in ``scope_*`` and work on any ingested source.
This module only adds what is specific to AI Village: the sequence of weekly
village goals (with a heuristic goal type) and per-goal activity, plus the
dataset's own docs as resources. Data comes from the store
(``swarm-mcp ingest ai_village data/ai-village``); the raw directory
(``$SWARM_DATA_DIR/ai-village`` or ``SWARM_VILLAGE_DIR``) is only used for
the README/SCHEMA/CHANGELOG resources.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from swarm_mcp.scope import db
from swarm_mcp.toolkit import ToolInputError, iso

NAME = "village"
DESCRIPTION = (
    "AI Village specifics (AI Digest; frontier-model agents sharing a group chat with weekly goals): "
    "the goal sequence with heuristic goal types and per-goal activity. Use scope_* for search/records/graphs."
)
SOURCE = "village"


def village_dir(ctx) -> Path:
    override = ctx.setting("dir")
    return Path(override).expanduser() if override else ctx.data_dir / "ai-village"


def requires(ctx) -> list[str]:
    path = ctx.store_path
    if not path.exists():
        return [f"SwarmScope store not found at {path}; run `swarm-mcp ingest ai_village data/ai-village`"]
    try:
        with ctx.store() as s:
            if not s.scalar("SELECT count(*) FROM sources WHERE source = ?", [SOURCE]):
                return [f"no AI Village data in {path}; run `swarm-mcp ingest ai_village data/ai-village`"]
    except Exception as e:  # noqa: BLE001 - e.g. a store from an older schema
        return [f"cannot read {path}: {type(e).__name__}: {e}"]
    return []


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


def _duration_days(start, end) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 86400, 1)


_GOALS_SQL = """
SELECT p.evidence_id, p.label, p.start_ts, p.end_ts, p.meta,
       (SELECT count(*) FROM messages m
         WHERE m.source = p.source AND m.ts >= p.start_ts AND (p.end_ts IS NULL OR m.ts < p.end_ts)) AS messages,
       (SELECT count(DISTINCT m.author_id) FROM messages m
         WHERE m.source = p.source AND m.author_id NOT LIKE 'human:%'
           AND m.ts >= p.start_ts AND (p.end_ts IS NULL OR m.ts < p.end_ts)) AS active_agents
FROM periods p
WHERE p.source = ? AND p.kind = 'village_goal'
ORDER BY p.start_ts
"""


def register(mcp, ctx) -> None:
    def goal_rows(s: db.Store) -> list[dict[str, Any]]:
        rows = s.all(_GOALS_SQL, [SOURCE])
        for i, r in enumerate(rows, 1):
            r["index"] = i
        return rows

    def goal_dict(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "index": r["index"],
            "evidence_id": r["evidence_id"],
            "goal": r["label"],
            "type": goal_type(r["label"]),
            "start": iso(str(r["start_ts"])) if r["start_ts"] else None,
            "end": iso(str(r["end_ts"])) if r["end_ts"] else None,
            "ongoing": r["end_ts"] is None,
            "duration_days": _duration_days(r["start_ts"], r["end_ts"]),
            "messages": r["messages"],
            "active_agents": r["active_agents"],
        }

    def find_goal(rows: list[dict[str, Any]], goal: str) -> dict[str, Any]:
        q = (goal or "").strip()
        if not q:
            raise ToolInputError("goal must not be empty")
        if q.isdigit():
            i = int(q)
            if 1 <= i <= len(rows):
                return rows[i - 1]
            raise ToolInputError(f"goal index {i} out of range 1..{len(rows)}; see village_goals")
        hits = [r for r in rows if r["evidence_id"] == q or q.lower() in (r["label"] or "").lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise ToolInputError(f"No village goal matches {goal!r}. Use village_goals to list them.")
        opts = "; ".join(f"{r['index']}: {(r['label'] or '')[:60]}" for r in hits[:10])
        raise ToolInputError(f"goal {goal!r} matches {len(hits)} goals; pass the index. Candidates: {opts}")

    @ctx.tool()
    def goals(
        agent: Annotated[
            str | None,
            Field(description="Also count this agent's messages per goal (name, alias like 'Opus 4.5', or agent_id)."),
        ] = None,
    ) -> dict[str, Any]:
        """The sequence of AI Village goals: index, evidence_id, goal text, start/end (UTC), duration, a heuristic
        goal type (holiday, self_directed, competitive, collaborative, individual, assigned_individual,
        open_task), and chat volume (messages, active agents) during each goal. Use start/end as since/until
        for scope_timeline / scope_comm_graph / scope_search."""
        with ctx.store() as s:
            rows = goal_rows(s)
            per_agent: dict[str, int] = {}
            label = None
            if agent:
                a = s.resolve_agent(agent, SOURCE)
                label = a["display_name"]
                for r in rows:
                    per_agent[r["evidence_id"]] = s.scalar(
                        "SELECT count(*) FROM messages WHERE author_id = ? AND ts >= ? AND (? IS NULL OR ts < ?)",
                        [a["agent_id"], r["start_ts"], r["end_ts"], r["end_ts"]],
                    )
        out = []
        for r in rows:
            d = goal_dict(r)
            if agent:
                d["agent_messages"] = per_agent.get(r["evidence_id"], 0)
            out.append(d)
        return {
            "count": len(out),
            "agent": label,
            "goals": out,
            "notes": ["'type' is a keyword heuristic from the goal text, not a dataset field"],
        }

    @ctx.tool()
    def goal(
        goal: Annotated[
            str, Field(description="Goal index (from village_goals), its evidence_id, or a substring of its text.")
        ],
        top: Annotated[int, Field(description="How many top speakers/channels to return.", ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """Activity during one AI Village goal period: window, type, message volume, top speakers, channels,
        the busiest day, and session-goal/summary counts. Cite the goal by its evidence_id."""
        with ctx.store() as s:
            r = find_goal(goal_rows(s), goal)
            window = "source = ? AND ts >= ? AND (? IS NULL OR ts < ?)"
            params = [SOURCE, r["start_ts"], r["end_ts"], r["end_ts"]]
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
        d = goal_dict(r)
        meta = json.loads(r["meta"]) if isinstance(r["meta"], str) else (r["meta"] or {})
        d.update(
            {
                "goal_index_in_dataset": meta.get("index"),
                "human_messages": humans,
                "top_speakers": [
                    {"author": db.label_for(x["author_id"], names), "author_id": x["author_id"], "messages": x["n"]}
                    for x in speakers
                ],
                "channels": [{"channel": x["channel"], "messages": x["n"]} for x in channels],
                "busiest_day": {"day": str(peak["day"]), "messages": peak["n"]} if peak else None,
                "actions": {x["kind"]: x["n"] for x in actions},
                "notes": [
                    f"for centrality call scope_comm_graph(since={d['start']!r}, until={d['end']!r}); "
                    "for the activity curve call scope_timeline with the same window",
                    "'type' is a keyword heuristic from the goal text, not a dataset field",
                ],
            }
        )
        return d

    # ------------------------------------------------------------------ resources

    root = village_dir(ctx)
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
