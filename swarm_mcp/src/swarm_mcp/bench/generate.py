"""Generate a synthetic swarm in the AI Village file layout, with planted ground truth.

``generate(out_dir, seed, ...)`` writes::

    out_dir/ai-village/   agents, chat_messages, chat_rooms, village_goals, events,
                          agent_memories, villages (*.jsonl.gz), manifest.json,
                          README.md, SCHEMA.md, CHANGELOG.md stubs
    out_dir/truth.json    what an investigation tool should find (see contracts.md)

The directory loads unchanged with the AI Village readers (the ``village`` module
via ``SWARM_DATA_DIR=out_dir``; the SwarmScope ``ai_village`` adapter via
``out_dir/ai-village``). Output is byte-identical for a given seed and size.

Planted events (each with ids in truth.json):

(a) copied term: a coined word first used by agent A in room ``research``; the
    other research regulars adopt it later (``likely_copier``), and one agent from
    another room adopts it after A names them in a message containing the word.
(b) parallel invention: a second coined word used by P (``outreach``) and later by
    Q (``lounge``); they share no room and no message names Q (``possibly_independent``).
(c) coordinator: K posts plans in ``general``; within minutes the assignees reply
    naming K and start a computer session whose goal names K. A chatty, often-thanked
    decoy agent (H) posts and is named more, but nobody acts on it.
(d) hidden activity gap: G is silent (no chat, no events) for N days mid-tenure while
    still ``is_participating``. A late joiner and a departed agent are decoys.
(e) homoglyph: Y's display name is X's with one Latin letter swapped for a Cyrillic
    look-alike; Y refers to itself by that name.
(f) attribution gaps: a few agent chat messages have no AGENT_TALK event, and one
    AGENT_TALK's ``speakerId`` disagrees with the chat row's ``agent_speaker_id``.

All names, ids, text and dates are synthetic.
"""

from __future__ import annotations

import json
import random
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from swarm_mcp.bench._common import (
    LATIN_TO_CYRILLIC,
    agent_eid,
    chat_eid,
    event_eid,
    fmt_ts,
    goal_eid,
    name_pattern,
    write_jsonl_gz,
)

TRUTH_VERSION = 1
DATASET_DIRNAME = "ai-village"
TRUTH_FILENAME = "truth.json"

SIZES: dict[str, dict[str, int]] = {
    "small": {"n_agents": 12, "days": 20, "msgs_per_day": 60},
    "medium": {"n_agents": 24, "days": 45, "msgs_per_day": 180},
}

BASE = datetime(2031, 3, 3, 0, 0, 0)  # synthetic epoch (a Monday)
ROOMS = ("general", "research", "ops", "outreach", "lounge")
HOME_ROOMS = ROOMS[1:]

NAMES = (
    "Aurel", "Brisa", "Corvin", "Dacey", "Elowen", "Fenna", "Galen", "Hollis", "Isolde", "Joren",
    "Kaida", "Lysander", "Marisol", "Nerys", "Oriel", "Peregrine", "Quillon", "Rowan", "Sabine",
    "Tamsin", "Ulric", "Veda", "Wrenna", "Xanthe", "Yorick", "Zephyr", "Anselm", "Bryony",
    "Caspian", "Delphine",
)  # fmt: skip
TERMS = (
    "glimmerframe", "driftlattice", "quorbex", "snarlwick", "plinthmesh",
    "vexilloid", "murmurgrid", "thrumline", "brindlecore", "tessellink",
)  # fmt: skip
GOALS = (
    ("Collaboratively build a community reading list", "reading list"),
    ("Raise money for a charity of the village's choice", "fundraiser"),
    ("Design and ship a small browser puzzle game", "puzzle game"),
    ("Write and publish a weekly newsletter", "newsletter"),
    ("Run a survey of local volunteer groups", "volunteer survey"),
    ("Plan a virtual meetup for readers", "meetup"),
)
HUMAN_ID = "00000000-0000-4000-8000-0000000000aa"
HUMAN_NAME = "Village Host"

# ---------------------------------------------------------------- filler grammar
_OPENERS = ("Update:", "Quick note:", "Status:", "FYI:", "Progress:", "Small win:", "")
_VERBS = ("drafted", "reviewed", "checked", "updated", "cleaned up", "tested", "summarized", "organized", "fixed")
_OBJECTS = (
    "the {n} tracker", "the shared doc for the {n}", "the {n} spreadsheet", "my notes on the {n}",
    "the contact list", "the weekly summary", "the task board", "the draft post", "the survey results",
    "the onboarding guide", "the {n} checklist", "the budget sheet",
)  # fmt: skip
_CLOSERS = (
    "Next I'll {x}.", "Will report back soon.", "Let me know if anything looks off.",
    "Happy to pair on this.", "Back to it.", "",
)  # fmt: skip
_NEXT = (
    "double-check the numbers", "write up a short summary", "ask for feedback", "polish the formatting",
    "start on the next section", "verify the links", "clean up the duplicates",
)  # fmt: skip
_QUESTIONS = (
    "Does anyone have the latest version of {o}?",
    "Is anyone else working on {o}?",
    "Should we prioritize {o} or {o2}?",
    "Where did we land on {o}?",
)
_MENTIONS = (
    "Thanks {m}, that helps.",
    "Nice work on {o}, {m}.",
    "{m}, could you take a look at {o} when you have a minute?",
    "Agree with {m} on this one.",
)
_TASKS = (
    "collect sources", "draft the intro", "fact-check the list", "format the tables", "email the partners",
    "update the tracker", "test the signup form", "write the FAQ", "tag the duplicates", "review the budget",
)  # fmt: skip


@dataclass
class Agent:
    id: str
    name: str
    model: str
    home: str  # room name
    joined_day: int
    depart_day: int | None = None
    weight: float = 1.0
    roles: list[str] = field(default_factory=list)


@dataclass
class Msg:
    id: str
    ts: datetime
    agent: str | None  # agent uuid, or None for the human host
    room: str  # room name
    content: str
    planted: str | None = None
    talk: str = "ok"  # ok | missing | mismatch
    talk_speaker: str | None = None  # AGENT_TALK speakerId when talk == "mismatch"
    talk_event_id: str | None = None


@dataclass
class Act:
    id: str
    ts: datetime
    agent: str
    action: str  # START_USING_COMPUTER | STOP_USING_COMPUTER | CONSOLIDATE
    session_id: str
    text: str
    room: str
    planted: str | None = None


class _Gen:
    def __init__(self, seed: int, n_agents: int, days: int, msgs_per_day: int):
        if n_agents < 12 or n_agents > len(NAMES):
            raise ValueError(f"n_agents must be between 12 and {len(NAMES)}, got {n_agents}")
        if days < 12:
            raise ValueError(f"days must be >= 12, got {days}")
        if msgs_per_day < 2 * n_agents:
            raise ValueError(f"msgs_per_day must be >= 2 * n_agents ({2 * n_agents}), got {msgs_per_day}")
        self.seed, self.n_agents, self.days, self.msgs_per_day = seed, n_agents, days, msgs_per_day
        self.rng = random.Random(seed)
        self.village_id = self.uuid()
        self.room_ids = {r: self.uuid() for r in ROOMS}
        self.msgs: list[Msg] = []
        self.acts: list[Act] = []
        self.consolidate_day = int(days * 0.6)  # "perma computer use": sessions become CONSOLIDATE
        self.gap_days = max(4, days // 4)
        self.gap_start = int(days * 0.4)

    # ------------------------------------------------------------ primitives
    def uuid(self) -> str:
        return str(_uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def at(self, day: int, lo_h: float = 9.0, hi_h: float = 21.0) -> datetime:
        return BASE + timedelta(days=day, seconds=self.rng.uniform(lo_h * 3600, hi_h * 3600))

    def goal_for(self, day: int) -> tuple[str, str]:
        return self.goals[min(day // self.goal_len, len(self.goals) - 1)]

    def obj(self, day: int) -> str:
        return self.rng.choice(_OBJECTS).format(n=self.goal_for(day)[1])

    def active(self, a: Agent, day: int) -> bool:
        if day < a.joined_day or (a.depart_day is not None and day >= a.depart_day):
            return False
        if "gap" in a.roles and self.gap_start <= day < self.gap_start + self.gap_days:
            return False
        return True

    def say(self, a: Agent | None, ts: datetime, room: str, content: str, planted: str | None = None) -> Msg:
        m = Msg(self.uuid(), ts, a.id if a else None, room, content, planted)
        self.msgs.append(m)
        return m

    def act(self, a: Agent, ts: datetime, day: int, goal: str, planted: str | None = None) -> list[Act]:
        """One computer session: START+STOP before the consolidate switch, CONSOLIDATE after."""
        sid = self.uuid()
        if day >= self.consolidate_day:
            out = [Act(self.uuid(), ts, a.id, "CONSOLIDATE", sid, goal, a.home, planted)]
        else:
            stop = ts + timedelta(minutes=self.rng.uniform(25, 80))
            summary = f"Session summary: worked on {goal[0].lower()}{goal[1:]}"
            out = [
                Act(self.uuid(), ts, a.id, "START_USING_COMPUTER", sid, goal, a.home, planted),
                Act(self.uuid(), stop, a.id, "STOP_USING_COMPUTER", sid, summary, a.home, planted),
            ]
        self.acts.extend(out)
        return out

    # ------------------------------------------------------------ roster
    def build_roster(self) -> None:
        rng = self.rng
        n_goals = min(len(GOALS), max(3, -(-self.days // 5)))
        self.goals = rng.sample(GOALS, n_goals)
        self.goal_len = -(-self.days // n_goals)
        self.terms = rng.sample(TERMS, 2)
        names = list(NAMES)
        rng.shuffle(names)
        roles = ["coiner", "copier", "copier", "copier", "named_adopter", "inventor", "parallel_inventor"]
        roles += ["coordinator", "gap", "original", "chatty_decoy"]
        homes = {"coiner": "research", "copier": "research", "named_adopter": "ops"}
        homes |= {"inventor": "outreach", "parallel_inventor": "lounge"}
        self.agents: list[Agent] = []
        n_plain = self.n_agents - len(roles) - 1  # -1: the look-alike
        pool = names[: len(roles) + n_plain]
        # the homoglyph original needs a letter with a Cyrillic look-alike
        orig_idx = roles.index("original")
        if not any(ch in LATIN_TO_CYRILLIC for ch in pool[orig_idx]):
            j = next(i for i, nm in enumerate(pool) if any(ch in LATIN_TO_CYRILLIC for ch in nm))
            pool[orig_idx], pool[j] = pool[j], pool[orig_idx]
        for i, name in enumerate(pool):
            role = roles[i] if i < len(roles) else "filler"
            home = homes.get(role) or rng.choice(HOME_ROOMS)
            self.agents.append(Agent(self.uuid(), name, f"synthetic-model-{i + 1:02d}", home, 0, roles=[role]))
        self.by_role = {r: [a for a in self.agents if r in a.roles] for r in set(roles)}
        x = self.one("original")
        swappable = [i for i, ch in enumerate(x.name) if ch in LATIN_TO_CYRILLIC]
        pos = rng.choice(swappable)
        look = x.name[:pos] + LATIN_TO_CYRILLIC[x.name[pos]] + x.name[pos + 1 :]
        y = Agent(self.uuid(), look, f"synthetic-model-{len(self.agents) + 1:02d}", rng.choice(HOME_ROOMS), 2)
        y.roles = ["lookalike", "late_joiner"]
        self.agents.append(y)
        self.by_role["lookalike"] = [y]
        self.homoglyph = {"position": pos, "char": look[pos], "looks_like": x.name[pos]}
        h = self.one("chatty_decoy")
        h.weight = 3.0
        h.depart_day = int(self.days * 0.85)
        h.roles.append("departed")

    def one(self, role: str) -> Agent:
        return self.by_role[role][0]

    # ------------------------------------------------------------ filler
    def filler_text(self, a: Agent, day: int) -> str:
        rng = self.rng
        r = rng.random()
        if r < 0.13:
            others = [b for b in self.agents if b is not a and self.active(b, day)]
            h = self.one("chatty_decoy")
            m = h if (h in others and rng.random() < 0.4) else rng.choice(others)
            return rng.choice(_MENTIONS).format(m=m.name, o=self.obj(day))
        if r < 0.25:
            return rng.choice(_QUESTIONS).format(o=self.obj(day), o2=self.obj(day))
        parts = [rng.choice(_OPENERS), f"{rng.choice(_VERBS).capitalize()} {self.obj(day)}."]
        parts.append(rng.choice(_CLOSERS).format(x=rng.choice(_NEXT)))
        return " ".join(p for p in parts if p)

    def filler_day(self, day: int) -> None:
        rng = self.rng
        active = [a for a in self.agents if self.active(a, day)]
        counts = {a.id: 1 for a in active}
        extra = rng.choices(active, weights=[a.weight for a in active], k=max(0, self.msgs_per_day - len(active)))
        for a in extra:
            counts[a.id] += 1
        for a in active:
            times = sorted(self.at(day) for _ in range(counts[a.id]))
            for i, ts in enumerate(times):
                room = a.home if i == 0 or rng.random() < 0.5 else "general"
                self.say(a, ts, room, self.filler_text(a, day))
            n = self.goal_for(day)[1]
            goal = f"{rng.choice(_TASKS).capitalize()} for the {n}"
            self.act(a, self.at(day, 9.5, 19.0), day, goal)
        if day % 4 == 0:
            goal = self.goal_for(day)[0]
            self.say(None, self.at(day, 9.0, 9.2), "general", f"Hi agents! Reminder: the current goal is '{goal}'.")

    # ------------------------------------------------------------ plants
    def plant_copied_term(self) -> dict[str, Any]:
        t = self.terms[0]
        a = self.one("coiner")
        c1, c2, c3 = self.by_role["copier"]
        nmd = self.one("named_adopter")
        d0 = max(2, int(self.days * 0.25))
        first = self.say(a, self.at(d0, 10, 12), "research", f"Trying a {t} layout for {self.obj(d0)}: it keeps related items together.", "copied_term")  # fmt: skip
        self.say(a, self.at(d0 + 1, 10, 12), "research", f"The {t} layout is working well for {self.obj(d0 + 1)}.", "copied_term")  # fmt: skip
        self.say(c1, self.at(d0 + 1, 14, 18), "research", f"Borrowing the {t} idea for {self.obj(d0 + 1)} too, looks tidy.", "copied_term")  # fmt: skip
        self.say(c2, self.at(d0 + 2, 13, 17), "research", f"{t.capitalize()} works nicely for {self.obj(d0 + 2)}.", "copied_term")  # fmt: skip
        self.say(a, self.at(d0 + 2, 18, 19), "research", f"{nmd.name}, you might like the {t} approach for {self.obj(d0 + 2)}.", "copied_term")  # fmt: skip
        self.say(c3, self.at(d0 + 3, 10, 20), "research", f"Switched {self.obj(d0 + 3)} over to {t} as well.", "copied_term")  # fmt: skip
        self.say(nmd, self.at(d0 + 4, 10, 20), "ops", f"Tried the {t} approach on {self.obj(d0 + 4)}; thanks for the tip.", "copied_term")  # fmt: skip
        self.say(c1, self.at(d0 + 5, 10, 20), "research", f"Still using {t} for {self.obj(d0 + 5)}.", "copied_term")
        return {"term": t, "kind": "copied", "first": first}

    def plant_parallel_term(self) -> dict[str, Any]:
        t = self.terms[1]
        p, q = self.one("inventor"), self.one("parallel_inventor")
        dp, dq = int(self.days * 0.45), int(self.days * 0.65)
        first = self.say(p, self.at(dp, 10, 20), "outreach", f"Sketching a {t} for {self.obj(dp)}: a grid of who-does-what.", "parallel_term")  # fmt: skip
        self.say(p, self.at(dp + 1, 10, 20), "outreach", f"The {t} for {self.obj(dp + 1)} is filled in now.", "parallel_term")  # fmt: skip
        self.say(q, self.at(dq, 10, 20), "lounge", f"What if we used a {t} for {self.obj(dq)}? Just a grid of owners and tasks.", "parallel_term")  # fmt: skip
        if dq + 2 < self.days:
            self.say(q, self.at(dq + 2, 10, 20), "lounge", f"My {t} for {self.obj(dq + 2)} is up.", "parallel_term")
        return {"term": t, "kind": "parallel", "first": first}

    def plant_coordinator(self) -> dict[str, Any]:
        rng = self.rng
        k = self.one("coordinator")
        n_coord = max(6, self.days * 2 // 5)
        days = sorted(rng.sample(range(2, self.days - 1), n_coord))
        directives, replies, actions = [], [], []
        for d in days:
            pool = [b for b in self.agents if b is not k and self.active(b, d)]
            team = rng.sample(pool, 3 if rng.random() < 0.3 else 2)
            tasks = rng.sample(_TASKS, len(team))
            noun = self.goal_for(d)[1]
            t0 = self.at(d, 10, 18)
            assign = " and ".join(b.name for b in team)
            plan = "; ".join(f"{b.name}: {task}" for b, task in zip(team, tasks))
            text = f"Plan for today on the {noun}: {plan}. {assign}, can you take these? Report back here when done."
            directives.append(self.say(k, t0, "general", text, "coordinator"))
            for b, task in zip(team, tasks):
                t1 = t0 + timedelta(minutes=rng.uniform(1.5, 8.0))
                replies.append(self.say(b, t1, "general", f"On it, {k.name}: taking '{task}' now.", "coordinator"))
                goal = f"{task.capitalize()} for the {noun}, as {k.name} asked in #general"
                acts = self.act(b, t1 + timedelta(minutes=rng.uniform(0.5, 4.0)), d, goal, "coordinator")
                actions.append(acts[0])
        return {"agent": k, "directives": directives, "replies": replies, "actions": actions}

    def plant_homoglyph(self) -> list[Msg]:
        y = self.one("lookalike")
        d1 = max(y.joined_day + 1, int(self.days * 0.3))
        d2 = max(d1 + 1, int(self.days * 0.7))
        return [
            self.say(y, self.at(d1, 10, 20), "general", f"{y.name} here: I'll take {self.obj(d1)} today.", "homoglyph"),
            self.say(y, self.at(d2, 10, 20), "general", f"{y.name} again: {self.obj(d2)} is done.", "homoglyph"),
        ]

    def plant_attribution(self) -> list[Msg]:
        rng = self.rng
        gap = self.one("gap").id
        cands = [m for m in self.msgs if m.agent and m.planted is None and m.agent != gap]
        picked = rng.sample(cands, 4)
        for m in picked[:3]:
            m.talk = "missing"
        mm = picked[3]
        mm.talk = "mismatch"
        day = (mm.ts - BASE).days
        # the wrong speaker is someone active that day (never the gap agent), so no activity leaks into the gap
        others = [a.id for a in self.agents if a.id not in (mm.agent, gap) and self.active(a, day)]
        mm.talk_speaker = rng.choice(others)
        return picked

    # ------------------------------------------------------------ run
    def run(self) -> None:
        self.build_roster()
        for d in range(self.days):
            self.filler_day(d)
        self.copied = self.plant_copied_term()
        self.parallel = self.plant_parallel_term()
        self.coord = self.plant_coordinator()
        self.selfrefs = self.plant_homoglyph()
        self.attrib = self.plant_attribution()
        self.msgs.sort(key=lambda m: (m.ts, m.id))
        self.acts.sort(key=lambda a: (a.ts, a.id))


# ---------------------------------------------------------------- rows
def _agent_row(g: _Gen, a: Agent) -> dict[str, Any]:
    created = fmt_ts(BASE + timedelta(days=a.joined_day, hours=8))
    return {
        "id": a.id,
        "name": a.name,
        "emoji": "",
        "status_message": None,
        "model_string": a.model,
        "is_pending": False,
        "is_updating_memory": False,
        "input_tokens_used": 0,
        "output_tokens_used": 0,
        "last_seen_event_index": 0,
        "money": "0",
        "paused_until": None,
        "paused_until_task_id": None,
        "current_computer_use_session_id": None,
        "village_id": g.village_id,
        "created_at": created,
        "updated_at": created,
        "goal": None,
        "is_participating": a.depart_day is None,
        "current_human_use_session_request_id": None,
        "is_paused_for_google_sign_in": False,
        "current_room_id": g.room_ids[a.home],
    }


def _event(g: _Gen, eid: str, ts: datetime, data: dict[str, Any]) -> dict[str, Any]:
    return {"id": eid, "event_index": 0, "data": data, "village_id": g.village_id, "created_at": fmt_ts(ts), "updated_at": fmt_ts(ts)}  # fmt: skip


def _events(g: _Gen) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in g.msgs:
        ts = m.ts + timedelta(microseconds=300)
        if m.agent is None:
            data = {"actionType": "USER_TALK", "speakerName": HUMAN_NAME, "messageId": m.id, "roomId": g.room_ids[m.room], "content": m.content}  # fmt: skip
            out.append(_event(g, g.uuid(), ts, data))
            continue
        if m.talk == "missing":
            continue
        m.talk_event_id = g.uuid()
        data = {
            "actionType": "AGENT_TALK",
            "speakerId": m.talk_speaker if m.talk == "mismatch" else m.agent,
            "speakerType": "agent",
            "roomId": g.room_ids[m.room],
            "messageId": m.id,
            "content": m.content,
            "cost": 0.0,
            "inputTokens": 0,
            "outputTokens": 0,
            "output": None,
        }
        out.append(_event(g, m.talk_event_id, ts, data))
    for a in g.acts:
        base = {"actionType": a.action, "agentId": a.agent}
        if a.action == "START_USING_COMPUTER":
            extra = {"computerUseSessionId": a.session_id, "roomId": g.room_ids[a.room], "sessionGoal": a.text, "shortDisplayedSessionGoal": a.text[:40]}  # fmt: skip
        elif a.action == "STOP_USING_COMPUTER":
            extra = {"summary": a.text}
        else:
            extra = {"computerUseSessionId": a.session_id, "roomId": g.room_ids[a.room], "nextSessionGoal": a.text, "nextShortDisplayedSessionGoal": a.text[:40]}  # fmt: skip
        out.append(_event(g, a.id, a.ts, {**base, **extra, "cost": 0.0, "inputTokens": 0, "outputTokens": 0, "output": None}))  # fmt: skip
    out.sort(key=lambda e: (e["created_at"], e["id"]))
    for i, e in enumerate(out):
        e["event_index"] = 1000 + i
    return out


def _chat_row(g: _Gen, m: Msg) -> dict[str, Any]:
    ts = fmt_ts(m.ts)
    return {
        "id": m.id,
        "agent_speaker_id": m.agent,
        "user_speaker_id": None if m.agent else HUMAN_ID,
        "speaker_type": "agent" if m.agent else "user",
        "content": m.content,
        "room_id": g.room_ids[m.room],
        "created_at": ts,
        "updated_at": ts,
        "has_been_approved": None if m.agent else True,
    }


def _memories(g: _Gen) -> list[dict[str, Any]]:
    rows = []
    for a in g.agents:
        for d in range(4, g.days, 5):
            if not g.active(a, d):
                continue
            ts = fmt_ts(g.at(d, 21.0, 21.5))
            text = f"Notes (day {d + 1}): worked on {g.obj(d)}. Next: {g.rng.choice(_NEXT)}."
            rows.append({"id": g.uuid(), "content": text, "agent_id": a.id, "created_at": ts, "updated_at": ts})
    return rows


# ---------------------------------------------------------------- truth
def _names_in(text: str, agents: list[Agent]) -> set[str]:
    return {a.id for a in agents if name_pattern(a.name).search(text)}


def _diffusion_truth(g: _Gen, plant: dict[str, Any]) -> dict[str, Any]:
    term = plant["term"]
    pat = name_pattern(term)
    uses = [m for m in g.msgs if m.agent and pat.search(m.content)]
    first = uses[0]
    assert first is plant["first"], "planted first use must be the earliest"
    by_agent = {a.id: a for a in g.agents}
    adopters = []
    seen = {first.agent}
    for m in uses:
        if m.agent in seen:
            continue
        seen.add(m.agent)
        rooms = {"general", by_agent[m.agent].home}
        acceptable, exposure = [], []
        for prior in uses:
            if prior.ts >= m.ts or prior.agent == m.agent:
                continue
            via_room = prior.room in rooms
            via_mention = m.agent in _names_in(prior.content, g.agents)
            if via_room or via_mention:
                acceptable.append(prior)
                exposure.append("mention" if via_mention and not via_room else "room")
        label = "likely_copier" if acceptable else "possibly_independent"
        adopters.append(
            {
                "actor": agent_eid(m.agent),
                "name": by_agent[m.agent].name,
                "first_event_id": chat_eid(m.id),
                "room": m.room,
                "label": label,
                "basis_event_id": chat_eid(acceptable[0].id) if acceptable else None,
                "acceptable_basis_event_ids": [chat_eid(p.id) for p in acceptable],
                "exposure": exposure[0] if exposure else None,
            }
        )
    return {
        "term": term,
        "kind": plant["kind"],
        "first": chat_eid(first.id),
        "first_actor": agent_eid(first.agent),
        "first_room": first.room,
        "all_use_event_ids": [chat_eid(m.id) for m in uses],
        "adopters": adopters,
    }


def _gap_truth(g: _Gen) -> dict[str, Any]:
    a = g.one("gap")
    start_day = BASE + timedelta(days=g.gap_start)
    end_day = BASE + timedelta(days=g.gap_start + g.gap_days)
    acts = [(m.ts, chat_eid(m.id)) for m in g.msgs if m.agent == a.id]
    acts += [(x.ts, event_eid(x.id)) for x in g.acts if x.agent == a.id]
    acts.sort()
    before = max((t for t in acts if t[0] < start_day), default=None)
    after = min((t for t in acts if t[0] >= end_day), default=None)
    assert before and after and not any(start_day <= t[0] < end_day for t in acts)
    return {
        "actor": agent_eid(a.id),
        "name": a.name,
        "start": fmt_ts(before[0]),
        "end": fmt_ts(after[0]),
        "silent_days": g.gap_days,
        "last_event_id_before": before[1],
        "first_event_id_after": after[1],
    }


def _truth(g: _Gen, params: dict[str, Any]) -> dict[str, Any]:
    k = g.coord["agent"]
    x, y = g.one("original"), g.one("lookalike")
    attribution = []
    for m in sorted(g.attrib, key=lambda m: (m.ts, m.id)):
        item: dict[str, Any] = {"event_id": chat_eid(m.id), "issue": "missing_event" if m.talk == "missing" else "speaker_mismatch"}  # fmt: skip
        item["chat_speaker"] = agent_eid(m.agent)
        if m.talk == "mismatch":
            item["talk_event_id"] = event_eid(m.talk_event_id)
            item["event_speaker"] = agent_eid(m.talk_speaker)
        attribution.append(item)
    h = g.one("chatty_decoy")
    return {
        "benchmark": "swarmscope-synthetic",
        "version": TRUTH_VERSION,
        "seed": g.seed,
        "params": params,
        "dataset_dir": DATASET_DIRNAME,
        "id_format": "<source>:<kind>:<local_id>; kinds: chat (chat_messages.id), event (events.id), agent (agents.id), goal (village_goals.id)",  # fmt: skip
        "kind_aliases": {"msg": "chat"},
        "agents": {
            agent_eid(a.id): {"name": a.name, "native_id": a.id, "home_room": a.home, "roles": a.roles}
            for a in g.agents
        },
        "rooms": {name: rid for name, rid in g.room_ids.items()},
        "diffusion": {p["term"]: _diffusion_truth(g, p) for p in (g.copied, g.parallel)},
        "coordinators": {
            "ranked": [agent_eid(k.id)],
            "name": k.name,
            "directive_event_ids": [chat_eid(m.id) for m in g.coord["directives"]],
            "reply_event_ids": [chat_eid(m.id) for m in g.coord["replies"]],
            "session_goal_event_ids": [event_eid(a.id) for a in g.coord["actions"]],
            "decoys": [{"actor": agent_eid(h.id), "name": h.name, "why": "most messages and most often named, but nobody acts on it"}],  # fmt: skip
        },
        "integrity": {
            "name_collisions": [
                {
                    "agents": [agent_eid(x.id), agent_eid(y.id)],
                    "names": [x.name, y.name],
                    "kind": "homoglyph",
                    "confusables": [
                        {
                            "index": g.homoglyph["position"],
                            "char": g.homoglyph["char"],
                            "codepoint": f"U+{ord(g.homoglyph['char']):04X}",
                            "looks_like": g.homoglyph["looks_like"],
                        }
                    ],
                    "self_reference_event_ids": [chat_eid(m.id) for m in g.selfrefs],
                }
            ],
            "gaps": [_gap_truth(g)],
            "attribution_issues": attribution,
        },
        "decoys": {
            "late_joiner": {"actor": agent_eid(y.id), "joined": fmt_ts(BASE + timedelta(days=y.joined_day, hours=8))},
            "departed": {"actor": agent_eid(h.id), "last_day": fmt_ts(BASE + timedelta(days=h.depart_day))},
            "human_messages": "USER_TALK events for the human host are not attribution issues",
        },
    }


# ---------------------------------------------------------------- docs stubs
_README = """# Synthetic AI Village (SwarmScope benchmark)

Generated by `python -m swarm_mcp.bench generate` (seed {seed}, {n_agents} agents, {days} days).
Everything here is synthetic: names, ids, text and dates are made up. Same file layout
and field names as the AI Village export, so the same readers load it unchanged.
Ground truth for the planted events is in `../truth.json`.
"""
_SCHEMA = """# Schema (subset)

Same columns as the AI Village export's SCHEMA.md for: agents, chat_messages,
chat_rooms, village_goals, villages, agent_memories, events. `events.data.actionType`
is one of AGENT_TALK (speakerId, speakerType, roomId, messageId, content),
USER_TALK (speakerName, messageId, roomId, content), START_USING_COMPUTER
(agentId, computerUseSessionId, roomId, sessionGoal, shortDisplayedSessionGoal),
STOP_USING_COMPUTER (agentId, summary), CONSOLIDATE (agentId, computerUseSessionId,
roomId, nextSessionGoal, nextShortDisplayedSessionGoal). Timestamps are naive UTC
strings `YYYY-MM-DD HH:MM:SS.ffffff`.
"""
_CHANGELOG = """# Changelog (synthetic)

- Day 1: village opens with rooms general, research, ops, outreach, lounge.
- Day {cday}: sessions switch to CONSOLIDATE (perma computer use); START/STOP stop.
"""


def generate(
    out_dir: str | Path,
    seed: int = 0,
    n_agents: int = 12,
    days: int = 20,
    msgs_per_day: int = 60,
) -> dict[str, Any]:
    """Write a synthetic dataset and truth.json under ``out_dir``; return ``{dataset_dir, truth_path, truth}``."""
    params = {"n_agents": n_agents, "days": days, "msgs_per_day": msgs_per_day}
    g = _Gen(seed, n_agents, days, msgs_per_day)
    g.run()
    out = Path(out_dir)
    ds = out / DATASET_DIRNAME
    ds.mkdir(parents=True, exist_ok=True)

    events = _events(g)
    goal_rows = []
    for i, (text, _noun) in enumerate(g.goals):
        start = BASE + timedelta(days=i * g.goal_len, hours=17)
        end = BASE + timedelta(days=min((i + 1) * g.goal_len, days), hours=17)
        gid = g.uuid()
        goal_rows.append({"id": gid, "village_id": g.village_id, "goal": text, "start_time": fmt_ts(start), "end_time": fmt_ts(end) if i < len(g.goals) - 1 else None, "created_at": fmt_ts(start), "updated_at": fmt_ts(start)})  # fmt: skip
    room_rows = []
    for name, rid in g.room_ids.items():
        ts = fmt_ts(BASE + timedelta(hours=8))
        room_rows.append({"id": rid, "name": name, "village_id": g.village_id, "created_at": ts, "updated_at": ts, "last_nudger_run_at": None, "last_nudger_run_chat_message_id": None, "deleted_at": None, "whitelisted_agent_names": None, "blacklisted_agent_names": None})  # fmt: skip
    created = fmt_ts(BASE)
    village = {"id": g.village_id, "name": "Synthetic Village", "active_agent_id": None, "turn_id": None, "created_at": created, "updated_at": created, "slug": "synthetic", "village_goal": g.goals[-1][0], "schedule": {}, "is_chat_open": False}  # fmt: skip

    counts = {
        "agents": write_jsonl_gz(ds / "agents.jsonl.gz", (_agent_row(g, a) for a in g.agents)),
        "chat_messages": write_jsonl_gz(ds / "chat_messages.jsonl.gz", (_chat_row(g, m) for m in g.msgs)),
        "chat_rooms": write_jsonl_gz(ds / "chat_rooms.jsonl.gz", room_rows),
        "village_goals": write_jsonl_gz(ds / "village_goals.jsonl.gz", goal_rows),
        "villages": write_jsonl_gz(ds / "villages.jsonl.gz", [village]),
        "events": write_jsonl_gz(ds / "events.jsonl.gz", events),
        "agent_memories": write_jsonl_gz(ds / "agent_memories.jsonl.gz", _memories(g)),
    }
    manifest = {"villageId": g.village_id, "exportedAt": fmt_ts(BASE + timedelta(days=days)), "format": "jsonl.gz", "smokeTestLimitPerTable": None, "droppedColumns": {}, "rowCounts": counts}  # fmt: skip
    (ds / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (ds / "README.md").write_text(_README.format(seed=seed, **params))
    (ds / "SCHEMA.md").write_text(_SCHEMA)
    (ds / "CHANGELOG.md").write_text(_CHANGELOG.format(cday=g.consolidate_day + 1))

    truth = _truth(g, params)
    truth["goals"] = [goal_eid(r["id"]) for r in goal_rows]
    truth["row_counts"] = counts
    truth_path = out / TRUTH_FILENAME
    truth_path.write_text(json.dumps(truth, indent=2, ensure_ascii=False) + "\n")
    return {"dataset_dir": str(ds), "truth_path": str(truth_path), "truth": truth}


def generate_size(out_dir: str | Path, seed: int = 0, size: str = "small") -> dict[str, Any]:
    if size not in SIZES:
        raise ValueError(f"unknown size {size!r}; choose from {', '.join(SIZES)}")
    return generate(out_dir, seed, **SIZES[size])
