"""How much of each agent's long-term memory is about the other agents, and
how much memory text agents share verbatim.

Takes the last memory each agent wrote on each day (the file holds every
consolidation; ~7 GB uncompressed, so the sample is cached after the first run).

Outputs out/memories.json.
"""

import collections
import gzip
import json
import statistics
import zlib

from common import (CACHE, Mentions, active_windows, agent_families, agent_presence, deepseek_label, load_agents,
                    load_chat, read_jsonl, ts, week_of, write_json)
from ideas import terms, words

SAMPLE = CACHE / "memory_daily_sample.jsonl.gz"
SHINGLE = 6


def build_sample(agents):
    latest = {}
    per_day = collections.Counter()
    for i, row in enumerate(read_jsonl("agent_memories.jsonl.gz")):
        a = agents.get(row["agent_id"])
        if not a:
            continue
        key = (a["name"], row["created_at"][:10])
        per_day[key] += 1
        if key not in latest or row["created_at"] > latest[key][0]:
            latest[key] = (row["created_at"], row["content"] or "")
        if i % 25000 == 0:
            print(f"  {i:,} memories scanned")
    with gzip.open(SAMPLE, "wt") as f:
        for (name, day), (created, content) in sorted(latest.items(), key=lambda x: x[1][0]):
            f.write(json.dumps({"agent": name, "day": day, "t": created, "n_that_day": per_day[(name, day)],
                                "content": content}) + "\n")


def shingles(text):
    toks = words(text.lower())
    return {zlib.crc32(" ".join(toks[i:i + SHINGLE]).encode()) for i in range(len(toks) - SHINGLE + 1)}


def main():
    agents = load_agents()
    fam = agent_families(agents, split_deepseek=True)
    if not SAMPLE.exists():
        print("building daily memory sample (one pass over the full file)…")
        build_sample(agents)
    msgs = load_chat(agents, split_deepseek=True)
    win = active_windows(msgs)
    mentions = Mentions(win, split_deepseek=True)
    # Present = joined the village and not yet gone (join date to last activity), by label.
    presence = {a: p["window"] for a, p in agent_presence(agents, msgs, split_deepseek=True).items()}
    ideas = json.loads((SAMPLE.parent.parent / "ideas.json").read_text())
    tracked = {r["term"] for r in ideas["durable"] + ideas["episodic"]}

    by_day = collections.defaultdict(dict)        # day -> agent -> shingles
    weekly = collections.defaultdict(lambda: collections.defaultdict(list))
    per_family = collections.defaultdict(lambda: collections.defaultdict(list))
    uptake = collections.defaultdict(dict)        # term -> agent -> first day in memory
    rows = 0
    with gzip.open(SAMPLE, "rt") as f:
        for line in f:
            m = json.loads(line)
            rows += 1
            t = ts(m["t"])
            # The sample stores the registry name; split the DeepSeek seat by memory created_at.
            agent, text = deepseek_label(m["agent"], t), m["content"]
            present = {a for a, (lo, hi) in presence.items() if lo <= t <= hi} - {agent}
            named = mentions.find(text, t) - {agent}
            wk = week_of(t)
            weekly[wk]["named"].append(len(named))
            if present:
                weekly[wk]["named_share"].append(len(named & present) / len(present))
                per_family[fam[agent]][wk[:7]].append(len(named & present) / len(present))
            weekly[wk]["chars"].append(len(text))
            weekly[wk]["consolidations"].append(m["n_that_day"])
            by_day[m["day"]][agent] = shingles(text)
            for term in terms(text) & tracked:
                uptake[term].setdefault(agent, m["day"])
    print(f"{rows:,} agent-day memories")

    # Shared content: share of each agent's memory (6-word runs) that also appears verbatim
    # in at least one other agent's memory from the same day.
    for day, mem in by_day.items():
        if len(mem) < 2:
            continue
        counts = collections.Counter(s for sh in mem.values() for s in sh)
        for sh in mem.values():
            if sh:
                weekly[week_of(ts(day + " 00:00:00"))]["shared"].append(sum(counts[s] > 1 for s in sh) / len(sh))

    def mean(v):
        return statistics.mean(v) if v else None

    write_json("memories.json", {
        "agent_days": rows,
        "weekly": [{"week": wk, "agent_days": len(v["chars"]),
                    "named": mean(v["named"]), "named_share": mean(v["named_share"]),
                    "median_chars": statistics.median(v["chars"]),
                    "consolidations": mean(v["consolidations"]),
                    "shared": mean(v["shared"])}
                   for wk, v in sorted(weekly.items())],
        "family_named_share": {f: [{"month": mo, "share": mean(v)} for mo, v in sorted(months.items())]
                               for f, months in per_family.items()},
        "uptake": {term: [{"agent": a, "family": fam[a], "day": d} for a, d in sorted(v.items(), key=lambda x: x[1])]
                   for term, v in uptake.items()},
    })


if __name__ == "__main__":
    main()
