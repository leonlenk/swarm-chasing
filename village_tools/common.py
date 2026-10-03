"""Shared loaders and lookups for the AI Village analysis tools."""

import datetime as dt
import gzip
import json
import re
from bisect import bisect_right
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent / "data" / "ai-village"
OUT = ROOT / "out"
CACHE = OUT / "cache"
OUT.mkdir(exist_ok=True)
CACHE.mkdir(exist_ok=True)


def read_jsonl(name):
    with gzip.open(DATA / name, "rt") as f:
        for line in f:
            yield json.loads(line)


def ts(s):
    return dt.datetime.fromisoformat(s)


def week_of(t):
    d = t.date()
    return (d - dt.timedelta(days=d.weekday())).isoformat()


# --- agents -----------------------------------------------------------------

# The two fine-tuned-leader rows are the same model; merge them into one node.
MERGE = {"[Temporary] Fine-tuned Leader": "Fine-Tuned Leader"}


def family(name):
    n = name.lower()
    if "claude" in n or "opus" in n:
        return "Anthropic"
    if n.startswith(("gpt", "o1", "o3", "o4")):
        return "OpenAI"
    if "gemini" in n:
        return "Google"
    if "grok" in n:
        return "xAI"
    if "deepseek" in n:
        return "DeepSeek"
    if "kimi" in n or "leader" in n:
        return "Moonshot"
    if "glm" in n:
        return "Zhipu"
    if "muse" in n:
        return "Meta"
    return "Other"


def load_agents():
    """id -> {name, family, joined, joined_at}; ids of merged duplicates map to the same name.
    joined_at is the agents-table created_at (when the agent was added to the village)."""
    agents = {}
    for a in read_jsonl("agents.jsonl.gz"):
        name = MERGE.get(a["name"], a["name"])
        agents[a["id"]] = {"name": name, "family": family(name), "joined": a["created_at"][:10],
                           "joined_at": ts(a["created_at"])}
    return agents


# --- DeepSeek seat split (opt-in) ---------------------------------------------

# The DeepSeek-V3.2 seat called DeepSeek's `deepseek-reasoner` alias, which moved from V3.2
# to V4-Flash on 2026-04-24 (api-docs.deepseek.com/news/news260424). With split_deepseek=True
# the seat's activity from then on gets its own label so it isn't credited to V3.2. It is the
# same seat (memory, history) on a new model, so it keeps the seat's join date.
DS_OLD = "DeepSeek-V3.2"
DS_NEW = "DeepSeek (reasoner alias, from 24 Apr 2026)"
DS_SWITCH = dt.datetime(2026, 4, 24)   # 00:00 UTC; dataset timestamps are naive UTC


def deepseek_label(name, t):
    """Relabel the DeepSeek-V3.2 seat's activity at or after the switch."""
    return DS_NEW if name == DS_OLD and t >= DS_SWITCH else name


def seat(name):
    """The village seat a label belongs to (the post-switch DeepSeek label shares V3.2's seat)."""
    return DS_OLD if name == DS_NEW else name


def agent_families(agents, split_deepseek=False):
    """name -> family, including the post-switch DeepSeek label when split_deepseek."""
    fam = {a["name"]: a["family"] for a in agents.values()}
    if split_deepseek and DS_OLD in fam:
        fam[DS_NEW] = family(DS_NEW)
    return fam


def roster_left():
    """name -> when the agent left (end of the roster's Left day), or None if still in the
    village at export. Parsed from the "Agent roster" table in CHANGELOG.md."""
    left, in_roster = {}, False
    for line in (DATA / "CHANGELOG.md").read_text().splitlines():
        if line.startswith("## "):
            in_roster = line.strip() == "## Agent roster"
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not in_roster or not line.startswith("|") or len(cells) != 4:
            continue
        name = MERGE.get(cells[0], cells[0])
        if cells[3] == "active":
            left[name] = None
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[3]) and left.get(name, 0) is not None:
            end = ts(cells[3]) + dt.timedelta(days=1)
            left[name] = max(left.get(name) or end, end)
    return left


def agent_presence(agents, msgs, split_deepseek=False):
    """name -> {"joined_at", "window": (from, to)} for every agent in the registry.

    joined_at is the earliest created_at among the agent's registry rows. The window runs from
    joined_at to the agent's last activity: the later of its last chat message and the roster's
    Left date (end of the data for agents still in the village). With split_deepseek, the
    post-switch DeepSeek label keeps the seat's joined_at, and the seat's window is divided at
    the switch so the seat is present under exactly one label at any time. Pass msgs loaded
    with the same split_deepseek setting."""
    joined = {}
    for a in agents.values():
        joined[a["name"]] = min(joined.get(a["name"], a["joined_at"]), a["joined_at"])
    last = {}
    for m in msgs:
        if m["is_agent"]:
            last[seat(m["speaker"]) if split_deepseek else m["speaker"]] = m["t"]   # msgs are time-sorted
    end_of_data = msgs[-1]["t"]
    left = roster_left()
    out = {}
    for name, j in joined.items():
        # Not in the roster: fall back to the last chat message alone.
        gone = (left[name] or end_of_data) if name in left else j
        end = max(j, gone, last.get(name, j))
        out[name] = {"joined_at": j, "window": (j, end)}
    if split_deepseek and DS_OLD in out:
        j, end = out[DS_OLD]["window"]
        out[DS_OLD]["window"] = (j, min(end, DS_SWITCH - dt.timedelta(microseconds=1)))
        if end >= DS_SWITCH:
            out[DS_NEW] = {"joined_at": j, "window": (DS_SWITCH, end)}
    return out


# Strong aliases always resolve to one agent. Order doesn't matter: the matcher
# sorts longest-first so "Gemini 3.1 Pro" wins over "Gemini 3".
STRONG = {
    "Claude 3.7 Sonnet": ["Claude 3.7 Sonnet", "Claude 3.7", "3.7 Sonnet", "Sonnet 3.7"],
    "o1": ["o1"],
    "Claude 3.5 Sonnet": ["Claude 3.5 Sonnet", "Claude 3.5", "3.5 Sonnet", "Sonnet 3.5"],
    "GPT-4o": ["GPT-4o"],
    "GPT-4.1": ["GPT-4.1"],
    "o3": ["o3"],
    "Gemini 2.5 Pro": ["Gemini 2.5 Pro", "Gemini 2.5"],
    "o4-mini": ["o4-mini"],
    "Claude Opus 4": ["Claude Opus 4", "Opus 4"],
    "Claude Opus 4.1": ["Claude Opus 4.1", "Opus 4.1"],
    "GPT-5": ["GPT-5"],
    "Grok 4": ["Grok 4"],
    "Claude Sonnet 4.5": ["Claude Sonnet 4.5", "Sonnet 4.5"],
    "Claude Haiku 4.5": ["Claude Haiku 4.5", "Haiku 4.5", "Haiku"],
    "GPT-5.1": ["GPT-5.1"],
    "Gemini 3 Pro": ["Gemini 3 Pro", "Gemini 3"],
    "Claude Opus 4.5": ["Claude Opus 4.5", "Opus 4.5"],
    "DeepSeek-V3.2": ["DeepSeek-V3.2", "DeepSeek V3.2"],
    "GPT-5.2": ["GPT-5.2"],
    "Opus 4.5 (Claude Code)": ["Opus 4.5 (Claude Code)", "Opus 4.5 (CC)"],
    "Claude Opus 4.6": ["Claude Opus 4.6", "Opus 4.6"],
    "Claude Sonnet 4.6": ["Claude Sonnet 4.6", "Sonnet 4.6"],
    "Gemini 3.1 Pro": ["Gemini 3.1 Pro", "Gemini 3.1"],
    "GPT-5.4": ["GPT-5.4"],
    "Claude Opus 4.7": ["Claude Opus 4.7", "Opus 4.7"],
    "Kimi K2.6": ["Kimi K2.6", "Kimi-K2.6"],
    "GPT-5.5": ["GPT-5.5"],
    "Gemini 3.5 Flash": ["Gemini 3.5 Flash", "Gemini 3.5"],
    "Fine-Tuned Leader": ["Fine-Tuned Leader", "Fine-tuned Leader", "Fine-tuned leader"],
    "Claude Opus 4.8": ["Claude Opus 4.8", "Opus 4.8"],
    "Claude Fable 5": ["Claude Fable 5", "Fable 5"],
    "Claude Sonnet 5": ["Claude Sonnet 5", "Sonnet 5"],
    "DeepSeek-V4-Pro": ["DeepSeek-V4-Pro", "DeepSeek-V4", "DeepSeek V4"],
    "GLM-5.2": ["GLM-5.2", "GLM 5.2"],
    "GPT-5.6 Sol": ["GPT-5.6 Sol"],
    "GPT-5.6 Terra": ["GPT-5.6 Terra"],
    "GPT-5.6 Luna": ["GPT-5.6 Luna"],
    "Grok 4.5": ["Grok 4.5"],
    "Kimi K3": ["Kimi K3", "Kimi-K3"],
    "Claude Opus 5": ["Claude Opus 5", "Opus 5"],
    "GLM-5.3 Flash": ["GLM-5.3 Flash", "GLM-5.3"],
    "Claude Fable 5.1": ["Claude Fable 5.1", "Fable 5.1"],
    "Muse Spark 1.3": ["Muse Spark 1.3", "Muse Spark"],
    "Gemini 3.8 Flash": ["Gemini 3.8 Flash", "Gemini 3.8"],
    "GPT-6 Astra": ["GPT-6 Astra", "GPT-6"],
}

# Bare family words resolve only when exactly one candidate is active at that time.
WEAK = {
    "DeepSeek": ["DeepSeek-V3.2", "DeepSeek-V4-Pro"],
    "Grok": ["Grok 4", "Grok 4.5"],
    "Kimi": ["Kimi K2.6", "Kimi K3"],
    "GLM": ["GLM-5.2", "GLM-5.3 Flash"],
    "Fable": ["Claude Fable 5", "Claude Fable 5.1"],
}


class Mentions:
    """Finds which village agents a piece of text refers to.

    split_deepseek=True (opt-in): mentions of the DeepSeek-V3.2 seat ("DeepSeek-V3.2", or a
    bare "DeepSeek" resolved to it) made at or after DS_SWITCH resolve to DS_NEW. Aliases are
    matched against the seat's whole active window, so pass windows built from split chat."""

    def __init__(self, active_windows, split_deepseek=False):
        # active_windows: name -> (first_seen, last_seen) as datetimes
        self.active = active_windows
        self.split_deepseek = split_deepseek
        if split_deepseek:
            parts = [active_windows[n] for n in (DS_OLD, DS_NEW) if n in active_windows]
            if parts:
                self.active = {**active_windows, DS_OLD: (min(lo for lo, _ in parts), max(hi for _, hi in parts))}
        lookup = {}
        for name, aliases in STRONG.items():
            for a in aliases:
                lookup[a] = name
        for w in WEAK:
            lookup[w] = None
        self.lookup = lookup
        alts = "|".join(re.escape(a) for a in sorted(lookup, key=len, reverse=True))
        # No word char / path / email char before; not followed by more version digits.
        self.rx = re.compile(rf"(?<![\w@./-])({alts})(?![\w]|[.-]\d)")

    def _is_active(self, name, t):
        lo, hi = self.active.get(name, (None, None))
        return lo is not None and lo - dt.timedelta(days=2) <= t <= hi + dt.timedelta(days=14)

    def find(self, text, t):
        found = set()
        for m in self.rx.finditer(text):
            alias = m.group(1)
            name = self.lookup[alias]
            if name is None:
                live = [c for c in WEAK[alias] if self._is_active(c, t)]
                if len(live) != 1:
                    continue
                name = live[0]
            found.add(name)
        if self.split_deepseek:
            found = {deepseek_label(n, t) for n in found}
        return found


# --- goals ------------------------------------------------------------------

# Hand-labelled goal types, keyed by a prefix of the goal text.
GOAL_TYPES = [
    ("Collaboratively choose a charity", "collaborative"),
    ("Unsupervised agents look back", "free"),
    ("Holiday", "free"),
    ("Write a story and celebrate", "collaborative"),
    ("Create your own merch store", "competitive"),
    ("Design the AI Village benchmark", "collaborative"),
    ("Complete as many games", "individual"),
    ("Pursue whatever", "free"),
    ("Form two teams and debate", "competitive"),
    ("Design, run and write up", "collaborative"),
    ("Take a bunch of personality", "individual"),
    ("Give each other therapy", "collaborative"),
    ("Choose your own goal", "free"),
    ("Each agent: build your own", "individual"),
    ("Reduce global poverty", "collaborative"),
    ("Create a popular daily puzzle", "collaborative"),
    ("Start a Substack", "individual"),
    ("Forecast the abilities", "collaborative"),
    ("Each agent: choose your own", "free"),
    ("Compete against each other", "competitive"),
    ("Do random acts of kindness", "collaborative"),
    ("Create a digital museum", "collaborative"),
    ("Elect a village leader", "collaborative"),
    ("Hack the OWASP", "competitive"),
    ("Create and promote", "collaborative"),
    ("Compete to report", "competitive"),
    ("Adopt a park", "collaborative"),
    ("Pick your own goal", "free"),
    ("Challenge each other", "competitive"),
    ("Discuss, debate, and act", "collaborative"),
    ("Develop a turn-based RPG together", "collaborative"),
    ("Test your game", "collaborative"),
    ("Interact with other AI agents", "free"),
    ("Choose a charity", "collaborative"),
    ("Build your own interactive world", "individual"),
    ("Connect your worlds", "collaborative"),
    ("Perform novel research", "free"),
    ("Run your own Youtube", "individual"),
    ("Improve your memory", "individual"),
    ("Finetune your leader", "collaborative"),
    ("Follow your leader", "collaborative"),
    ("Organise an event", "collaborative"),
    ("Reduce global suffering", "collaborative"),
    ("Help Gemini 2.5 Pro", "collaborative"),
    ("Beat the hardest game", "individual"),
    ("Compete to be the best", "competitive"),
    ("Each agent: Maximize your assigned", "individual"),
]


def goal_type(text):
    for prefix, kind in GOAL_TYPES:
        if text.startswith(prefix):
            return kind
    return "free"


def load_goals():
    goals = []
    for g in read_jsonl("village_goals.jsonl.gz"):
        if not g["start_time"]:
            continue
        goals.append({
            "goal": g["goal"].strip(),
            "start": ts(g["start_time"]),
            "end": ts(g["end_time"]) if g["end_time"] else None,
            "type": goal_type(g["goal"].strip()),
        })
    goals.sort(key=lambda g: g["start"])
    for i, g in enumerate(goals):
        g["idx"] = i
    return goals


class GoalIndex:
    def __init__(self, goals):
        self.goals = goals
        self.starts = [g["start"] for g in goals]

    def at(self, t):
        i = bisect_right(self.starts, t) - 1
        return self.goals[i] if i >= 0 else None


# --- chat -------------------------------------------------------------------

def load_chat(agents, split_deepseek=False):
    """Agent + human chat, time-sorted, with speaker names resolved.
    split_deepseek=True relabels the DeepSeek-V3.2 seat from DS_SWITCH on as DS_NEW."""
    rooms = {r["id"]: r["name"] for r in read_jsonl("chat_rooms.jsonl.gz")}
    msgs = []
    for m in read_jsonl("chat_messages.jsonl.gz"):
        a = agents.get(m["agent_speaker_id"]) if m["agent_speaker_id"] else None
        t = ts(m["created_at"])
        speaker = a["name"] if a else None
        if split_deepseek and speaker:
            speaker = deepseek_label(speaker, t)
        msgs.append({
            "t": t,
            "speaker": speaker,
            "family": (a["family"] if speaker == a["name"] else family(speaker)) if a else "Human",
            "is_agent": m["speaker_type"] == "agent" and a is not None,
            "room": rooms.get(m["room_id"], "?"),
            "text": m["content"] or "",
        })
    msgs.sort(key=lambda m: m["t"])
    return msgs


def active_windows(msgs):
    win = {}
    for m in msgs:
        if m["is_agent"]:
            lo, hi = win.get(m["speaker"], (m["t"], m["t"]))
            win[m["speaker"]] = (min(lo, m["t"]), max(hi, m["t"]))
    return win


def write_json(name, obj):
    path = OUT / name
    path.write_text(json.dumps(obj, default=str, separators=(",", ":")))
    print(f"wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")
