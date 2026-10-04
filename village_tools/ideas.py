"""Trace coined terms through the village chat.

A "term" is a non-dictionary word (jargon, project names, coinages) or a
distinctive two-word phrase. For each one we record who used it first and when
every other agent first picked it up. Terms whose origin is the goal text or the
scaffolding changelog are flagged as seeded so they can be separated from ideas
that spread agent-to-agent.

Outputs out/ideas.json.
"""

import collections
import datetime as dt
import re
import statistics

from common import (DATA, STRONG, agent_families, agent_presence, load_agents, load_chat, load_goals, seat, week_of,
                    write_json)

STOP = set("""
a about above after again against all also am an and any are aren't as at be because been before being below
between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each
few for from further had hadn't has hasn't have haven't having he he'd he'll he's her here here's hers herself
him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself just let's me more most
mustn't my myself no nor not now of off on once only or other ought our ours ourselves out over own same
shan't she she'd she'll she's should shouldn't so some such than that that's the their theirs them themselves
then there there's these they they'd they'll they're they've this those through to too under until up very
was wasn't we we'd we'll we're we've were weren't what what's when when's where where's which while who who's
whom why why's will with won't would wouldn't you you'd you'll you're you've your yours yourself yourselves
yes ok okay still now new next one two three get got via per like well need needs using use used make made
""".split())

URL = re.compile(r"https?://\S+|\S+@\S+\.\w+|\b[\w-]+\.(?:com|org|io|net|dev|app|ai|html|md|json|js|py)\b")
CHUNK = re.compile(r"[.,;:!?()\[\]{}<>\"|*#`=/\\\n]+")   # phrases never span these
TOKEN = re.compile(r"[a-z][a-z0-9]*(?:['\-][a-z0-9]+)*|\d[\w.]*")
NAME_TOKENS = {t for name, aliases in STRONG.items() for a in [name, *aliases] for t in TOKEN.findall(a.lower())}
NAME_TOKENS |= {"claude", "opus", "sonnet", "haiku", "gemini", "gpt", "grok", "deepseek", "kimi", "glm", "fable",
                "muse", "spark", "flash", "pro", "astra", "sol", "terra", "luna", "leader", "openai", "anthropic"}
DICT = {w.strip().lower() for w in open("/usr/share/dict/words")}

MIN_DOCS = 20          # messages a term must appear in to be listed or counted for influence
MIN_DOCS_SPEED = 5     # looser floor for the spread-speed comparison, to limit survivor bias
MIN_LIFT = 30          # two-word phrases must co-occur this many times more than chance
NOVELTY_DAYS = 21      # ignore terms already in use during the first three weeks
MIN_USES_TO_ADOPT = 2  # an agent "adopts" a term once it has used it in this many messages
REACH_DAYS = 14        # window for comparing spread across eras
BURST_WEEKS = 4        # a term is episodic if most of its use falls in this many consecutive weeks


def words(chunk):
    out = []
    for t in TOKEN.findall(chunk):
        if t.endswith("'s"):
            t = t[:-2]
        out.append(t)
    return out


def terms(text):
    text = URL.sub(" . ", text.lower().replace("’", "'"))
    out = set()
    for chunk in CHUNK.split(text):
        toks = words(chunk)
        for t in toks:
            if len(t) >= 4 and t[0].isalpha() and t not in STOP:
                out.add(t)
        for a, b in zip(toks, toks[1:]):
            if a[0].isalpha() and b[0].isalpha() and a not in STOP and b not in STOP and len(a) >= 3 and len(b) >= 3:
                out.add(f"{a} {b}")
    return {t for t in out if not set(t.split()) & NAME_TOKENS}


def seeded_terms(goals):
    """Terms introduced from outside the chat: goal text and scaffolding changelog, with the date they appeared."""
    seeded = {}
    for g in goals:
        for t in terms(g["goal"]):
            seeded.setdefault(t, g["start"])
    section_date = None
    for line in (DATA / "CHANGELOG.md").read_text().splitlines():
        m = re.match(r"## (\d{4}-\d{2}-\d{2})", line)
        if m:
            section_date = dt.datetime.fromisoformat(m.group(1))
        elif section_date:
            for t in terms(line):
                if t not in seeded or section_date < seeded[t]:
                    seeded[t] = section_date
    return seeded


def burstiness(weekly):
    """Largest share of a term's uses that falls inside BURST_WEEKS consecutive weeks."""
    if not weekly:
        return 0
    weeks = sorted(weekly)
    total = sum(weekly.values())
    best = 0
    for i, w in enumerate(weeks):
        end = (dt.date.fromisoformat(w) + dt.timedelta(weeks=BURST_WEEKS)).isoformat()
        best = max(best, sum(weekly[x] for x in weeks[i:] if x < end))
    return best / total


def quarter(t):
    return f"{t.year}-Q{(t.month - 1) // 3 + 1}"


def main():
    agents = load_agents()
    fam = agent_families(agents, split_deepseek=True)
    msgs = load_chat(agents, split_deepseek=True)
    goals = load_goals()
    seeded = seeded_terms(goals)
    n_docs = len(msgs)

    msg_terms = [terms(m["text"]) for m in msgs]
    docs = collections.Counter(t for ts_ in msg_terms for t in ts_)
    early_cut = msgs[0]["t"] + dt.timedelta(days=NOVELTY_DAYS)
    early = set()
    for m, ts_ in zip(msgs, msg_terms):
        if m["t"] >= early_cut:
            break
        early |= ts_

    def distinctive(t):
        if " " not in t:
            return t not in DICT
        a, b = t.split()
        return docs[a] and docs[b] and docs[t] * n_docs / (docs[a] * docs[b]) >= MIN_LIFT

    cands = {t for t, n in docs.items() if n >= MIN_DOCS_SPEED and t not in early and distinctive(t)}
    print(f"{len(docs):,} distinct terms, {len(cands):,} candidates")

    first = {}
    uses = collections.defaultdict(lambda: collections.defaultdict(list))   # term -> speaker -> [(time, room)]
    weekly = collections.defaultdict(collections.Counter)
    for m, ts_ in zip(msgs, msg_terms):
        who = m["speaker"] or "Human"
        for t in ts_ & cands:
            if t not in first:
                first[t] = (m["t"], who, m["room"])
            uses[t][who].append((m["t"], m["room"]))
            weekly[t][week_of(m["t"])] += 1

    # Presence runs from the agent's join date (agents.created_at) to its last activity, so agents
    # who were in the village but quiet at coinage count as present, not as newcomers. Counted by
    # seat, so the split DeepSeek seat is one agent at any time.
    presence = agent_presence(agents, msgs, split_deepseek=True)
    joined = {a: p["joined_at"] for a, p in presence.items()}

    def present(t):
        return {seat(a) for a, p in presence.items() if p["window"][0] <= t <= p["window"][1]}

    records = []
    for t in cands:
        t0, origin, room = first[t]
        adopters = []
        for who, hits in uses[t].items():
            if who in (origin, "Human") or len(hits) < MIN_USES_TO_ADOPT:
                continue
            at, aroom = hits[0]
            adopters.append({"agent": who, "family": fam[who], "t": at, "room": aroom, "uses": len(hits),
                             "newcomer": joined[who] > t0})
        adopters.sort(key=lambda a: a["t"])
        seed_time = seeded.get(t)
        records.append({
            "term": t, "origin": origin, "origin_family": fam.get(origin, "Human"), "origin_time": t0,
            "room": room, "docs": docs[t],
            "seeded": seed_time is not None and seed_time <= t0 + dt.timedelta(days=1),
            "burst": burstiness(weekly[t]),
            "present": len(present(t0)),
            "adopters": adopters,
        })

    listed = [r for r in records if r["docs"] >= MIN_DOCS and not r["seeded"] and len(r["adopters"]) >= 3]
    print(f"{len(listed):,} unseeded terms adopted by >=3 other agents")

    def pick(rows, n):
        rows = sorted(rows, key=lambda r: (-len(r["adopters"]), -r["docs"]))
        top = []
        for r in rows:
            ws = set(r["term"].split())
            if any(ws & set(o["term"].split()) and abs(r["origin_time"] - o["origin_time"]) < dt.timedelta(days=2)
                   for o in top):
                continue
            top.append(r)
            if len(top) == n:
                break
        for r in top:
            r["weekly"] = sorted(weekly[r["term"]].items())
        return top

    durable = pick([r for r in listed if r["burst"] < 0.5], 25)
    episodic = pick([r for r in listed if r["burst"] >= 0.6], 25)

    # Influence: who coins terms that others pick up, and which families pick them up.
    influence, coined, flow = collections.Counter(), collections.Counter(), collections.Counter()
    for r in listed:
        coined[r["origin"]] += 1
        influence[r["origin"]] += len(r["adopters"])
        for a in r["adopters"]:
            flow[(r["origin_family"], a["family"])] += 1

    # How adoption happens, by quarter of adoption: newcomers inheriting old terms vs
    # agents who were present at coinage; and, once rooms exist, adoption across rooms.
    by_q = collections.defaultdict(collections.Counter)
    for r in listed:
        for a in r["adopters"]:
            q = by_q[quarter(a["t"])]
            q["adoptions"] += 1
            q["newcomer"] += a["newcomer"]
            if a["t"] >= dt.datetime(2026, 2, 25):
                q["roomed"] += 1
                q["cross_room"] += a["room"] != r["room"]

    # Spread speed by quarter of coinage: share of agents present at coinage who adopt within
    # REACH_DAYS. Uses the looser doc floor and skips terms coined too close to the end of the data.
    speed = collections.defaultdict(list)
    horizon = msgs[-1]["t"] - dt.timedelta(days=REACH_DAYS)
    for r in records:
        if r["seeded"] or r["origin_time"] > horizon or r["present"] < 2:
            continue
        cut = r["origin_time"] + dt.timedelta(days=REACH_DAYS)
        n = len({seat(a["agent"]) for a in r["adopters"] if a["t"] <= cut and not a["newcomer"]} - {seat(r["origin"])})
        speed[quarter(r["origin_time"])].append((n / (r["present"] - 1), r["present"]))

    human = sorted((r for r in listed if r["origin"] == "Human"), key=lambda r: -len(r["adopters"]))[:15]

    def slim(r):
        return {k: r[k] for k in ("term", "origin", "origin_family", "origin_time", "room", "docs", "burst",
                                  "present", "adopters", "weekly")}

    write_json("ideas.json", {
        "params": {"min_docs": MIN_DOCS, "min_docs_speed": MIN_DOCS_SPEED, "novelty_days": NOVELTY_DAYS,
                   "min_uses_to_adopt": MIN_USES_TO_ADOPT, "reach_days": REACH_DAYS, "min_lift": MIN_LIFT},
        "counts": {
            "candidates": len(cands),
            "listed": len(listed),
            "seeded_adopted": sum(r["seeded"] and len(r["adopters"]) >= 3 for r in records),
            "human_origin": sum(r["origin"] == "Human" for r in listed),
            "adoptions": sum(len(r["adopters"]) for r in listed),
            "newcomer_adoptions": sum(a["newcomer"] for r in listed for a in r["adopters"]),
        },
        "durable": [slim(r) for r in durable],
        "episodic": [slim(r) for r in episodic],
        "human_top": [{"term": r["term"], "origin_time": r["origin_time"], "docs": r["docs"],
                       "adopters": len(r["adopters"])} for r in human],
        "influence": [{"agent": a, "family": fam.get(a, "Human"), "coined": coined[a], "adoptions": n}
                      for a, n in influence.most_common()],
        "flow": [{"from": a, "to": b, "n": n} for (a, b), n in flow.items()],
        "adoption_by_quarter": [{"quarter": q, **c} for q, c in sorted(by_q.items())],
        "speed": [{"quarter": q, "terms": len(v),
                   "mean_reach": statistics.mean(x for x, _ in v),
                   "share_any": sum(x > 0 for x, _ in v) / len(v),
                   "mean_present": statistics.mean(p for _, p in v)}
                  for q, v in sorted(speed.items())],
    })


if __name__ == "__main__":
    main()
