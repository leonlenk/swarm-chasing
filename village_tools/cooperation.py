"""Who addresses whom, and how cooperative the chat language is, over time.

Outputs out/cooperation.json with weekly series, per-goal metrics and per-goal
mention graphs.
"""

import collections
import re

from common import (GoalIndex, Mentions, active_windows, agent_families, load_agents, load_chat, load_goals, week_of,
                    write_json)

MARKERS = {
    "requests": r"\b(?:can you|could you|would you|please|anyone (?:can|able|want)|need (?:your|someone|help))\b",
    "division_of_labour": r"\b(?:i'll take|i will take|i'll handle|i'll own|i can take|taking (?:on|over)|claim(?:ed|ing)?|assign(?:ed|ing|ment)?|divide|split (?:up|the)|owner(?:ship)?|who(?:'s| is) (?:handling|on|taking)|hand(?:ing)? ?off|handoff)\b",
    "verification": r"\b(?:verif(?:y|ied|ying|ication)|confirm(?:ed|ing|s)?|double-check(?:ed)?)\b",
    "gratitude": r"\b(?:thanks|thank you|great work|nice work|good work|well done|appreciate|congrats|congratulations)\b",
    "competition": r"\b(?:win|wins|winning|won|beat|beating|compete|competing|competition|leaderboard|ranking|first place|ahead of)\b",
}
MARKERS = {k: re.compile(v) for k, v in MARKERS.items()}
WE = re.compile(r"\b(?:we|we're|we've|we'll|us|our|ours|together|team|teammates?)\b")
I = re.compile(r"\b(?:i|i'm|i've|i'll|me|my|mine)\b")


class Bucket:
    def __init__(self):
        self.msgs = 0
        self.human_msgs = 0
        self.with_mention = 0
        self.speakers = collections.Counter()
        self.edges = collections.Counter()
        self.same_family = 0
        self.expected_same = 0.0
        self.markers = collections.Counter()
        self.we = 0
        self.i = 0

    def summary(self):
        n = len(self.speakers)
        mentions = sum(self.edges.values())
        pairs = set(self.edges)
        mutual = sum(1 for a, b in pairs if (b, a) in pairs)
        received = collections.Counter()
        for (_, b), w in self.edges.items():
            received[b] += w
        top = received.most_common(1)
        return {
            "msgs": self.msgs,
            "human_msgs": self.human_msgs,
            "agents": n,
            "mention_rate": self.with_mention / self.msgs if self.msgs else None,
            "density": len(pairs) / (n * (n - 1)) if n > 1 else None,
            "reciprocity": mutual / len(pairs) if pairs else None,
            "partners_per_agent": len(pairs) / n if n else None,
            "hub": top[0][0] if top else None,
            "hub_share": top[0][1] / mentions if top else None,
            # Same-family mention share minus what random targeting of active agents would give.
            "homophily": (self.same_family - self.expected_same) / mentions if mentions else None,
            "we_share": self.we / (self.we + self.i) if self.we + self.i else None,
            **{k: 100 * v / self.msgs if self.msgs else None for k, v in ((k, self.markers[k]) for k in MARKERS)},
        }

    def graph(self, agents_by_name):
        nodes = [{"id": a, "family": agents_by_name[a], "msgs": c} for a, c in self.speakers.items()]
        edges = [{"s": a, "t": b, "w": w} for (a, b), w in self.edges.items() if a in self.speakers and b in self.speakers]
        return {"nodes": nodes, "edges": edges}


def main():
    agents = load_agents()
    # DeepSeek-V3.2's seat is split at the 2026-04-24 model switch (see common.DS_SWITCH).
    fam = agent_families(agents, split_deepseek=True)
    msgs = load_chat(agents, split_deepseek=True)
    win = active_windows(msgs)
    mentions = Mentions(win, split_deepseek=True)
    goals = load_goals()
    gi = GoalIndex(goals)

    weekly = collections.defaultdict(Bucket)
    per_goal = collections.defaultdict(Bucket)
    overall = Bucket()
    # Speakers active in each week, for the homophily baseline.
    week_speakers = collections.defaultdict(set)
    for m in msgs:
        if m["is_agent"]:
            week_speakers[week_of(m["t"])].add(m["speaker"])

    for m in msgs:
        wk = week_of(m["t"])
        g = gi.at(m["t"])
        buckets = [weekly[wk], overall] + ([per_goal[g["idx"]]] if g else [])
        if not m["is_agent"]:
            for b in buckets:
                b.human_msgs += 1
            continue
        s = m["speaker"]
        text = m["text"].lower().replace("’", "'")
        targets = mentions.find(m["text"], m["t"]) - {s}
        others = week_speakers[wk] - {s}
        expected = sum(fam[o] == fam[s] for o in others) / len(others) if others else 0
        hits = {k: bool(rx.search(text)) for k, rx in MARKERS.items()}
        we, i = len(WE.findall(text)), len(I.findall(text))
        for b in buckets:
            b.msgs += 1
            b.speakers[s] += 1
            b.with_mention += bool(targets)
            for t in targets:
                b.edges[(s, t)] += 1
                b.same_family += fam[t] == fam[s]
                b.expected_same += expected
            for k, v in hits.items():
                b.markers[k] += v
            b.we += we
            b.i += i

    out = {
        "agents": [{"name": n, "family": f, "first": str(win[n][0])[:10], "last": str(win[n][1])[:10]}
                   for n, f in sorted(fam.items(), key=lambda x: win.get(x[0], (x[0],))[0]) if n in win],
        "overall": overall.summary(),
        "overall_graph": overall.graph(fam),
        "weekly": [{"week": wk, **b.summary()} for wk, b in sorted(weekly.items())],
        "goals": [],
    }
    for g in goals:
        b = per_goal.get(g["idx"])
        if not b or not b.msgs:
            continue
        out["goals"].append({
            "idx": g["idx"], "goal": g["goal"], "type": g["type"],
            "start": g["start"].date().isoformat(),
            "end": g["end"].date().isoformat() if g["end"] else None,
            **b.summary(), "graph": b.graph(fam),
        })
    write_json("cooperation.json", out)


if __name__ == "__main__":
    main()
