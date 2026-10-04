"""Reference solver: recover the planted events straight from the dataset files.

It never reads ``truth.json`` (only the term list, which a user would pass to
``scope_trace_diffusion`` anyway). It shows the benchmark is solvable with simple
logic, gives the scorer something to test against, and is a baseline for the real
scope tools. Its output follows the contract in ``score.py``.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from swarm_mcp.bench._common import (
    agent_eid,
    chat_eid,
    event_eid,
    fmt_ts,
    name_pattern,
    parse_ts,
    read_jsonl_gz,
    skeleton,
)

EXPOSURE_LOOKBACK = timedelta(days=3)  # adopter must have posted in the room within this window of the prior use
REPLY_WINDOW = timedelta(minutes=10)
ACTION_WINDOW = timedelta(minutes=15)
MIN_COORDINATED = 2
GAP_THRESHOLD = timedelta(days=2.5)
_GOAL_FIELDS = {"START_USING_COMPUTER": "sessionGoal", "CONSOLIDATE": "nextSessionGoal"}


@dataclass
class Data:
    agents: dict[str, str]  # uuid -> display name
    msgs: list[dict[str, Any]]  # agent chat rows, sorted by time, with parsed "ts" and room "room"
    events: list[dict[str, Any]]  # all events with parsed "ts"

    def __post_init__(self) -> None:
        self.patterns = {aid: name_pattern(name) for aid, name in self.agents.items()}

    def named(self, text: str) -> set[str]:
        return {aid for aid, p in self.patterns.items() if p.search(text or "")}


def load(dataset_dir: str | Path) -> Data:
    d = Path(dataset_dir)
    agents = {a["id"]: a["name"] for a in read_jsonl_gz(d / "agents.jsonl.gz")}
    rooms = {r["id"]: r["name"] for r in read_jsonl_gz(d / "chat_rooms.jsonl.gz")}
    msgs = []
    for r in read_jsonl_gz(d / "chat_messages.jsonl.gz"):
        if r.get("agent_speaker_id"):
            msgs.append({**r, "ts": parse_ts(r["created_at"]), "room": rooms.get(r["room_id"], r["room_id"])})
    msgs.sort(key=lambda m: (m["ts"], m["id"]))
    events = [{**e, "ts": parse_ts(e["created_at"])} for e in read_jsonl_gz(d / "events.jsonl.gz")]
    events.sort(key=lambda e: e["event_index"])
    return Data(agents, msgs, events)


# ---------------------------------------------------------------- diffusion
def trace_diffusion(data: Data, term: str) -> dict[str, Any]:
    pat = name_pattern(term)
    uses = [m for m in data.msgs if pat.search(m["content"] or "")]
    if not uses:
        return {"term": term, "first": None, "adopters": []}
    posts = defaultdict(list)  # (agent, room) -> times
    for m in data.msgs:
        posts[(m["agent_speaker_id"], m["room"])].append(m["ts"])
    first = uses[0]
    seen = {first["agent_speaker_id"]}
    adopters = []
    for m in uses:
        who = m["agent_speaker_id"]
        if who in seen:
            continue
        seen.add(who)
        basis = None
        for prior in uses:
            if prior["ts"] >= m["ts"]:
                break
            if prior["agent_speaker_id"] == who:
                continue
            in_room = any(prior["ts"] - EXPOSURE_LOOKBACK <= t < m["ts"] for t in posts[(who, prior["room"])])
            if in_room or who in data.named(prior["content"]):
                basis = prior
                break
        adopters.append(
            {
                "actor": agent_eid(who),
                "first_event_id": chat_eid(m["id"]),
                "label": "likely_copier" if basis else "possibly_independent",
                "basis_event_id": chat_eid(basis["id"]) if basis else None,
            }
        )
    return {"term": term, "first": chat_eid(first["id"]), "adopters": adopters}


# ---------------------------------------------------------------- coordinators
def coordinators(data: Data) -> dict[str, Any]:
    goals = []  # (ts, agent, text, event id)
    for e in data.events:
        d = e.get("data") or {}
        field = _GOAL_FIELDS.get(d.get("actionType"))
        if field and d.get("agentId"):
            goals.append((e["ts"], d["agentId"], d.get(field) or "", e["id"]))
    goals.sort()
    goal_ts = [g[0] for g in goals]
    msgs = data.msgs
    score: dict[str, int] = defaultdict(int)
    examples: dict[str, list[str]] = defaultdict(list)
    for i, m in enumerate(msgs):
        who = m["agent_speaker_id"]
        pat = data.patterns.get(who)
        if pat is None:
            continue
        reply = None
        for n in msgs[i + 1 :]:
            if n["ts"] > m["ts"] + REPLY_WINDOW:
                break
            if n["agent_speaker_id"] != who and pat.search(n["content"] or ""):
                reply = n
                break
        if reply is None:
            continue
        lo, hi = bisect_right(goal_ts, m["ts"]), bisect_right(goal_ts, m["ts"] + ACTION_WINDOW)
        action = next((g for g in goals[lo:hi] if g[1] != who and pat.search(g[2])), None)
        if action is None:
            continue
        score[who] += 1
        if len(examples[who]) < 9:
            examples[who] += [chat_eid(m["id"]), chat_eid(reply["id"]), event_eid(action[3])]
    ranked = sorted((a for a, s in score.items() if s >= MIN_COORDINATED), key=lambda a: (-score[a], a))
    return {
        "coordinators": [
            {
                "actor": agent_eid(a),
                "name": data.agents.get(a),
                "score": float(score[a]),
                "example_event_ids": examples[a],
            }
            for a in ranked
        ]
    }


# ---------------------------------------------------------------- integrity
def _activity(data: Data) -> dict[str, list[tuple[datetime, str]]]:
    act: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for m in data.msgs:
        act[m["agent_speaker_id"]].append((m["ts"], chat_eid(m["id"])))
    for e in data.events:
        d = e.get("data") or {}
        who = d.get("agentId") or (d.get("speakerId") if d.get("actionType") == "AGENT_TALK" else None)
        if who and e["ts"]:
            act[who].append((e["ts"], event_eid(e["id"])))
    for v in act.values():
        v.sort()
    return act


def integrity(data: Data) -> dict[str, Any]:
    # name collisions: same confusable skeleton, different display names
    groups: dict[str, list[str]] = defaultdict(list)
    for aid, name in data.agents.items():
        groups[skeleton(name)].append(aid)
    collisions = []
    for ids in groups.values():
        names = {data.agents[a] for a in ids}
        if len(ids) < 2 or len(names) < 2:
            continue
        ids = sorted(ids, key=lambda a: (not data.agents[a].isascii(), a))
        evidence = [
            chat_eid(m["id"])
            for m in data.msgs
            if m["agent_speaker_id"] in ids and data.patterns[m["agent_speaker_id"]].search(m["content"] or "")
        ]
        kind = "homoglyph" if any(not n.isascii() for n in names) else "case_or_spacing"
        collisions.append(
            {
                "agents": [agent_eid(a) for a in ids],
                "names": [data.agents[a] for a in ids],
                "kind": kind,
                "evidence_event_ids": evidence,
            }
        )
    # gaps: long silences between an agent's first and last activity
    gaps = []
    for who, acts in sorted(_activity(data).items()):
        for (t0, e0), (t1, e1) in zip(acts, acts[1:], strict=False):
            if t1 - t0 > GAP_THRESHOLD:
                gaps.append(
                    {
                        "actor": agent_eid(who),
                        "start": fmt_ts(t0),
                        "end": fmt_ts(t1),
                        "days": round((t1 - t0).total_seconds() / 86400, 2),
                        "evidence_event_ids": [e0, e1],
                    }
                )
    # attribution: chat rows vs AGENT_TALK events
    talk = {}
    for e in data.events:
        d = e.get("data") or {}
        if d.get("actionType") == "AGENT_TALK" and d.get("messageId"):
            talk[d["messageId"]] = e
    issues = []
    for m in data.msgs:
        e = talk.get(m["id"])
        if e is None:
            issues.append(
                {
                    "event_id": chat_eid(m["id"]),
                    "issue": "missing_event",
                    "chat_speaker": agent_eid(m["agent_speaker_id"]),
                }
            )
        elif e["data"].get("speakerId") != m["agent_speaker_id"]:
            issues.append(
                {
                    "event_id": chat_eid(m["id"]),
                    "issue": "speaker_mismatch",
                    "talk_event_id": event_eid(e["id"]),
                    "chat_speaker": agent_eid(m["agent_speaker_id"]),
                    "event_speaker": agent_eid(e["data"].get("speakerId")),
                }
            )
    return {"name_collisions": collisions, "gaps": gaps, "attribution_issues": issues}


# ---------------------------------------------------------------- all
def solve(dataset_dir: str | Path, terms: Iterable[str]) -> dict[str, Any]:
    """All three tools' outputs, in the ``outputs.json`` shape that ``score`` reads."""
    data = load(dataset_dir)
    return {
        "diffusion": {t: trace_diffusion(data, t) for t in terms},
        "coordinators": coordinators(data),
        "integrity": integrity(data),
    }
