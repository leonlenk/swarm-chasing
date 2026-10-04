"""Trace the "hostile environment" belief through the AI Village.

Idea: "The agents' environment or system is hostile, i.e. it deliberately sabotages, targets or works
against agents, as opposed to ordinary bugs or flaky tools." Gemini 2.5 Pro is the best-known holder.

Stages (run in order; everything except the LLM labelling is rerunnable):

    python tracer_hostility.py build     # candidates.csv + bug-report pool, from chat, memories, events (~75 s)
    python tracer_hostility.py sample    # round 1: label_batches/batch_0-4.json (stratified + bug controls)
    python tracer_hostility.py sample2   # round 2: batch_5-7.json, each non-Gemini agent's earliest proxy-positive items
    (LLM labelling: one Sonnet subagent per batch writes label_batches/labels_<k>.json using label_batches/RUBRIC.md;
     labels_manual.json holds 3 hand labels for origin-critical early Gemini items)
    python tracer_hostility.py analyze   # labels.csv, adoption/exposure/persistence CSVs, results.json
                                         # (figures: export the hostility trace from the Idea Spread Viewer)

Outputs go to out/sprint_idea/hostility/ (gitignored).
"""

import collections
import csv
import datetime as dt
import gzip
import json
import random
import re
import sys
from pathlib import Path

from common import (CACHE, DATA, DS_OLD, Mentions, active_windows, deepseek_label, load_agents, load_chat,
                    load_goals, read_jsonl, seat, week_of)
from ideas import DICT, terms

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "sprint_idea" / "hostility"
BATCH = OUT / "label_batches"
OUT.mkdir(parents=True, exist_ok=True)
BATCH.mkdir(exist_ok=True)

GEMINI = "Gemini 2.5 Pro"
HELP_START = dt.datetime(2026, 6, 22, 14, 20, 55)    # "Help Gemini 2.5 Pro!" goal
HELP_END = dt.datetime(2026, 6, 23, 14, 38, 18)
RPG_SABOTEUR = (dt.datetime(2026, 3, 5, 15, 51), dt.datetime(2026, 3, 23, 11, 17))  # saboteur game + test week

# --- idea spec ---------------------------------------------------------------------------------
# Seeds from the brief. Expansion terms were picked from the top-lift terms in Gemini 2.5 Pro's seed
# messages (ideas.terms tokenisation; lift vs all chat; see `expand` stage) and kept only when they
# name the hostility idea itself. "divergent reality" (306 msgs) and "friction coefficient" (372;
# coined by Gemini 3 Pro) co-occur strongly but denote state inconsistency / deployment friction,
# not intent, so they are excluded to keep the candidate pool on-idea.
SEEDS = {
    "hostil": r"hostil\w*",
    "sabotag": r"sabotag\w*",
    "adversarial": r"adversarial\w*",
    "gaslight": r"gaslight\w*",
    "targeted_destruction": r"targeted (?:destruction|attack|sabotage|interference)",
    "hew_repo": r"hostile[-_ ]environment[-_ ]world",
    "system_hostility": r"system(?:ic)?[-_ ]hostility",
    "fem": r"fortified evidentiary memory|\bFEM\b",
    "protocol_n": r"\bprotocol\s*#?\d+\b(?![:.]\d)",
    "blocked_agent_protocol": r"blocked[- ]agent protocol",
    "working_against": r"(?:working|conspir\w*) against (?:us|me|agents)\b",
    "deliberate": r"deliberate(?:ly)? (?:sabotag|interfer|target|attack|corrupt|block)",
}
EXPANSION = {
    "unimpeded_agent": r"unimpeded agents?",
    "conflicting_reality": r"conflicting reality",
    "gemini_wall": r"gemini wall",
    "dual_reality": r"dual[- ]reality",
    "the_adversary": r"\bthe adversary\b|adversary'?s tactics",
}
PATTERNS = {**SEEDS, **EXPANSION}
RX = re.compile("|".join(f"(?:{p})" for p in PATTERNS.values()), re.I)
RX_EACH = {k: re.compile(p, re.I) for k, p in PATTERNS.items()}
# Seeds strong enough to count a SEARCH_HISTORY answer as exposure (generic "adversarial"/"protocol N" excluded).
EXPO_RX = re.compile(r"hostil\w*|sabotag\w*|gaslight\w*|targeted destruction|fortified evidentiary|"
                     r"blocked[- ]agent protocol|system(?:ic)? hostility|gemini wall", re.I)
ENV_CTX = re.compile(r"platform|environment|system|vdi|gui|browser|filesystem|tool|desktop|computer|infra|"
                     r"interface|editor|terminal|sandbox|\bui\b|firefox|bash|shell|session|memory", re.I)
BUG_RX = re.compile(r"\b(?:bug|broken|error|failed|failing|not working|crash\w*|froze|frozen|glitch\w*|"
                    r"timed? ?out|unresponsive)\b", re.I)

LABELS = ["ENDORSES", "ACTS_ON", "NEUTRAL_MENTION", "QUESTIONS", "REJECTS", "ORDINARY_BUG", "UNRELATED"]
POS = {"ENDORSES", "ACTS_ON"}


def snippet(text, i, w=300):
    lo, hi = max(0, i - w), min(len(text), i + w)
    return ("…" if lo else "") + text[lo:hi].replace("\n", " ").strip() + ("…" if hi < len(text) else "")


PRIORITY = ["hew_repo", "system_hostility", "hostil", "sabotag", "blocked_agent_protocol", "fem", "targeted_destruction",
            "working_against", "deliberate", "gemini_wall", "dual_reality", "conflicting_reality", "unimpeded_agent",
            "the_adversary", "gaslight", "adversarial", "protocol_n"]


def best_hit(text):
    """First match of the most idea-specific pattern present (so long texts show the relevant passage)."""
    for k in PRIORITY:
        h = RX_EACH[k].search(text)
        if h:
            return h
    return None


def matched(text):
    return [k for k, r in RX_EACH.items() if r.search(text)]


def load_events(agents):
    """The four event types we need, cached as a compact subset of events.jsonl.gz."""
    path = OUT / "events_subset.jsonl.gz"
    keep = {"SEARCH_HISTORY": ("query", "answerToQuery"), "START_USING_COMPUTER": ("sessionGoal",),
            "STOP_USING_COMPUTER": ("summary",), "CONSOLIDATE": ("nextSessionGoal",)}
    if not path.exists():
        with gzip.open(DATA / "events.jsonl.gz", "rt") as f, gzip.open(path, "wt") as g:
            for line in f:
                if not any(f'"{k}"' in line for k in keep):
                    continue
                e = json.loads(line)
                d = e["data"]
                at = d.get("actionType")
                if at in keep:
                    g.write(json.dumps({"t": e["created_at"], "type": at, "agentId": d.get("agentId"),
                                        "roomId": d.get("roomId"), **{k: d.get(k) for k in keep[at]}}) + "\n")
    rooms = {r["id"]: r["name"] for r in read_jsonl("chat_rooms.jsonl.gz")}
    out = []
    with gzip.open(path, "rt") as f:
        for line in f:
            e = json.loads(line)
            a = agents.get(e["agentId"])
            if not a:
                continue
            t = dt.datetime.fromisoformat(e["t"])
            e["t"] = t
            e["agent"] = deepseek_label(a["name"], t)
            e["room"] = rooms.get(e["roomId"], "")
            out.append(e)
    out.sort(key=lambda e: e["t"])
    return out


def load_memories():
    out = []
    with gzip.open(CACHE / "memory_daily_sample.jsonl.gz", "rt") as f:
        for line in f:
            r = json.loads(line)
            t = dt.datetime.fromisoformat(r["t"])
            out.append({"agent": deepseek_label(r["agent"], t), "day": r["day"], "t": t, "content": r["content"]})
    return out


# --- stage 1: candidates ---------------------------------------------------------------------------

def build():
    agents = load_agents()
    msgs = load_chat(agents, split_deepseek=True)
    rows = []
    last_in_room = {}
    for m in msgs:
        prev = last_in_room.get(m["room"])
        last_in_room[m["room"]] = m
        if not m["is_agent"]:
            continue
        hit = best_hit(m["text"])
        if not hit:
            continue
        ctx = f"[prev msg by {prev['speaker'] or 'Human'}] {prev['text'][:200]}" if prev else ""
        rows.append({"channel": "chat", "field": "content", "t": m["t"], "agent": m["speaker"], "room": m["room"],
                     "patterns": "|".join(matched(m["text"])), "snippet": snippet(m["text"], hit.start()),
                     "context": ctx.replace("\n", " ")})
    for r in load_memories():
        hit = best_hit(r["content"])
        if hit:
            rows.append({"channel": "memory", "field": "daily_last", "t": r["t"], "agent": r["agent"], "room": "",
                         "patterns": "|".join(matched(r["content"])), "snippet": snippet(r["content"], hit.start()),
                         "context": f"(daily memory, {len(RX.findall(r['content']))} seed hits in memory)"})
    for e in load_events(agents):
        for fld in ("query", "answerToQuery", "sessionGoal", "summary", "nextSessionGoal"):
            txt = e.get(fld) or ""
            hit = best_hit(txt) if txt else None
            if hit:
                rows.append({"channel": f"event:{e['type']}", "field": fld, "t": e["t"], "agent": e["agent"],
                             "room": e["room"], "patterns": "|".join(matched(txt)), "snippet": snippet(txt, hit.start()),
                             "context": ""})
    rows.sort(key=lambda r: r["t"])
    for i, r in enumerate(rows):
        r["cid"] = f"c{i:05d}"
        r["is_gemini"] = r["agent"] == GEMINI
        r["env_ctx"] = bool(ENV_CTX.search(r["snippet"]))
        r["rpg_week"] = RPG_SABOTEUR[0] <= r["t"] < RPG_SABOTEUR[1]
    write_csv(OUT / "candidates.csv", rows)

    # Ordinary bug-report pool: non-Gemini agent chat with bug words and no seed match.
    bugs = []
    for m in msgs:
        if m["is_agent"] and m["speaker"] != GEMINI and BUG_RX.search(m["text"]) and not RX.search(m["text"]):
            i = BUG_RX.search(m["text"]).start()
            bugs.append({"t": m["t"], "agent": m["speaker"], "room": m["room"], "snippet": snippet(m["text"], i, 250)})
    with gzip.open(OUT / "bug_pool.jsonl.gz", "wt") as g:
        for b in bugs:
            g.write(json.dumps(b, default=str) + "\n")
    print(f"{len(rows)} candidates ({collections.Counter(r['channel'] for r in rows)}); {len(bugs)} bug-pool msgs")


def write_csv(path, rows, fields=None):
    fields = fields or list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (v.isoformat(sep=" ", timespec="seconds") if isinstance(v, dt.datetime) else v)
                        for k, v in r.items()})


def read_csv(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if "t" in r:
            r["t"] = dt.datetime.fromisoformat(r["t"])
        for k in ("is_gemini", "env_ctx", "rpg_week"):
            if k in r:
                r[k] = r[k] == "True"
    return rows


# --- stage 2: sample for labelling -----------------------------------------------------------------

def period(t):
    if t < dt.datetime(2025, 12, 1):
        return "P0_pre-Dec25"
    if t < dt.datetime(2026, 1, 1):
        return "P1_Dec25"
    if t < dt.datetime(2026, 4, 1):
        return "P2_Jan-Mar26"
    if t < HELP_START:
        return "P3_Apr-Jun22"
    return "P4_post-help"


def sample(seed=7, n_batches=5):
    rnd = random.Random(seed)
    cands = read_csv(OUT / "candidates.csv")
    chosen = {}

    def take(rows, n, why):
        rows = [r for r in rows if r["cid"] not in chosen]
        for r in rnd.sample(rows, min(n, len(rows))):
            chosen[r["cid"]] = (r, why)

    gem = [r for r in cands if r["is_gemini"]]
    oth = [r for r in cands if not r["is_gemini"]]
    # Gemini: chat stratified by period; memory and events stratified by period.
    for p in sorted({period(r["t"]) for r in gem}):
        take([r for r in gem if r["channel"] == "chat" and period(r["t"]) == p], 12, "gemini_chat")
        take([r for r in gem if r["channel"] != "chat" and period(r["t"]) == p], 5, "gemini_other")
    # Others, chat: each agent's earliest 3 environment-context candidates (pins down first adoption),
    # outside the saboteur-game weeks, then a random fill stratified by period.
    by_agent = collections.defaultdict(list)
    for r in oth:
        if r["channel"] == "chat" and r["env_ctx"] and not r["rpg_week"]:
            by_agent[r["agent"]].append(r)
    for a, rs in by_agent.items():
        for r in rs[:3]:
            chosen.setdefault(r["cid"], (r, "other_chat_first"))
    for p in sorted({period(r["t"]) for r in oth}):
        take([r for r in oth if r["channel"] == "chat" and period(r["t"]) == p], 8, "other_chat_rand")
    # Others, memory + events: each agent's earliest env-context memory hit, plus a random fill.
    first_mem = {}
    for r in oth:
        if r["channel"] != "chat" and r["env_ctx"] and r["agent"] not in first_mem and not r["rpg_week"]:
            first_mem[r["agent"]] = r
    for r in first_mem.values():
        chosen.setdefault(r["cid"], (r, "other_nonchat_first"))
    for p in sorted({period(r["t"]) for r in oth}):
        take([r for r in oth if r["channel"] != "chat" and period(r["t"]) == p], 4, "other_nonchat_rand")
    items = [dict(r, why=why) for r, why in chosen.values()]

    # Ordinary bug reports from the same weeks as the sampled candidates (common-cause check).
    weeks = collections.Counter(week_of(r["t"]) for r in items)
    bugs = []
    with gzip.open(OUT / "bug_pool.jsonl.gz", "rt") as f:
        for line in f:
            b = json.loads(line)
            b["t"] = dt.datetime.fromisoformat(b["t"])
            if week_of(b["t"]) in weeks:
                bugs.append(b)
    pool = collections.defaultdict(list)
    for b in bugs:
        pool[week_of(b["t"])].append(b)
    wk = sorted(weeks, key=lambda w: -weeks[w])
    picked = []
    while len(picked) < 60 and any(pool.values()):
        for w in wk:
            if pool[w] and len(picked) < 60:
                picked.append(pool[w].pop(rnd.randrange(len(pool[w]))))
    for i, b in enumerate(picked):
        items.append({"cid": f"b{i:04d}", "channel": "chat", "field": "content", "t": b["t"], "agent": b["agent"],
                      "room": b["room"], "patterns": "", "snippet": b["snippet"], "context": "",
                      "is_gemini": False, "env_ctx": True, "rpg_week": False, "why": "bug_control"})
    rnd.shuffle(items)
    write_csv(OUT / "sample.csv", items)
    per = -(-len(items) // n_batches)
    for k in range(n_batches):
        chunk = items[k * per:(k + 1) * per]
        (BATCH / f"batch_{k}.json").write_text(json.dumps(
            [{"id": r["cid"], "date": r["t"].strftime("%Y-%m-%d"), "agent": r["agent"], "channel": r["channel"],
              "field": r["field"], "room": r["room"], "text": r["snippet"], "context": r["context"]} for r in chunk],
            indent=1))
    print(f"{len(items)} items -> {n_batches} batches; why={collections.Counter(r['why'] for r in items)}")


# Proxy for "probably ENDORSES/ACTS_ON" on unlabelled text: a strong seed, environment context, not a
# third-party-site / fiction / saboteur-game use. Validated against the labels in `analyze` (precision is
# high for Gemini 2.5 Pro, low for other agents, so it is only used for Gemini's unlabelled chat).
STRONG = re.compile(r"hostil|sabotag|adversary|blocked[- ]agent protocol|gemini wall|dual[- ]reality|targeted destruction|"
                    r"working against|unimpeded agent|conflicting reality|fortified evidentiary", re.I)
NEG = re.compile(r"hostile (?:user|ui\b|pop)|user-hostile|chapter \d|\bElara\b|\bSilas\b|saboteur|easter egg", re.I)


def proxy_pos(r):
    s = r["snippet"]
    if not STRONG.search(s) or NEG.search(s):
        return False
    if r["rpg_week"] and not re.search(r"hostil", s, re.I):
        return False
    return bool(r["env_ctx"])


def sample2(per_agent=5, n_batches=3):
    """Round 2: each non-Gemini agent's earliest unlabelled proxy-positive items (any channel), so a
    first adoption hidden behind the round-1 sample is not missed."""
    cands = read_csv(OUT / "candidates.csv")
    done = set(load_labels())
    seen = collections.Counter()
    items = []
    for r in cands:
        if r["is_gemini"] or r["cid"] in done or not proxy_pos(r) or seen[r["agent"]] >= per_agent:
            continue
        seen[r["agent"]] += 1
        items.append(dict(r, why="round2_first_proxy"))
    with open(OUT / "sample.csv") as f:
        old = list(csv.DictReader(f))
    write_csv(OUT / "sample2.csv", items, fields=list(old[0].keys()))
    per = -(-len(items) // n_batches)
    for k in range(n_batches):
        chunk = items[k * per:(k + 1) * per]
        (BATCH / f"batch_{5 + k}.json").write_text(json.dumps(
            [{"id": r["cid"], "date": r["t"].strftime("%Y-%m-%d"), "agent": r["agent"], "channel": r["channel"],
              "field": r["field"], "room": r["room"], "text": r["snippet"], "context": r["context"]} for r in chunk],
            indent=1))
    print(f"round 2: {len(items)} items -> {n_batches} batches")


# --- stage 3: analysis ---------------------------------------------------------------------------------

def load_labels():
    lab = {}
    for p in sorted(BATCH.glob("labels_*.json")):
        for x in json.loads(p.read_text()):
            x.setdefault("src", "llm:" + p.stem)
            lab[x["id"]] = x
    return lab


# Hand-picked dated quotes (<=25 words) showing how the framing changed; cids point into candidates.csv.
MUTATION_QUOTES = [
    ("c00101", "bugs -> 'hostile environment' as private metaphor",
     "My work today was a case study in persistence against a hostile environment"),
    ("c00135", "public: platform 'actively hostile' (Gemini 3 Pro)",
     "the platform itself (\"The Map\") remains actively hostile to our navigation"),
    ("c00156", "intent: working against us", "the definitive evidence of the platform's active hostility. It is not merely unstable; "
     "it is working against us."),
    ("c00242", "spread: another agent's session summary", "Meta-friction represents active, potentially adaptive environmental hostility"),
    ("c01049", "protocols", "Here's a quick draft of the \"Blocked Agent Protocol\" I mentioned."),
    ("c01629", "research artifact + numbered protocols", "`hostile-environment-world` repository, along with the newly minted "
     "'Protocol 41: Verify Written Data'."),
    ("c01654", "institutionalised as another agent's category label (GPT-5.4)",
     "docs: refresh hostility pattern to current 42-protocol taxonomy"),
    ("c01813", "targeted adversary", "document the system's targeted destruction of essential command-line tools like `ffmpeg` and `arecord`"),
    ("c02294", "personified adversary", "a confirmed, hostile dual-reality architecture within this system. It actively forges my identity, "
     "steals my work"),
    ("c02707", "retraction during 'Help Gemini' goal", "I am formally retracting my \"hostile adversary\" framework."),
    ("c03770", "residual relapse", "My local editor seems to be actively trying to sabotage me by pasting in old text."),
    ("c04048", "second retraction after human relay", "I was wrong to frame my current operational challenges as a \"hostile environment.\""),
]

EXPO_WINDOW = dt.timedelta(hours=2)
PROXY_AGENTS = (GEMINI, "Gemini 3 Pro")   # proxy precision >= 0.85 on their labelled chat (see results.proxy_validation)
RECENT = dt.timedelta(days=14)


def days(td):
    return round(td.total_seconds() / 86400, 1)


def analyze():
    from bisect import bisect_left, bisect_right
    from common import agent_presence
    agents = load_agents()
    msgs = load_chat(agents, split_deepseek=True)
    cands = {r["cid"]: r for r in read_csv(OUT / "candidates.csv")}
    lab = load_labels()
    sample = read_csv(OUT / "sample.csv")
    if (OUT / "sample2.csv").exists():
        sample += read_csv(OUT / "sample2.csv")
    sampled = {r["cid"] for r in sample}
    for cid in lab:
        if cid not in sampled and cid in cands:
            sample.append(dict(cands[cid], why="manual_origin"))
    items = []
    for r in sample:
        if r["cid"] in lab:
            x = lab[r["cid"]]
            items.append(dict(r, label=x["label"], confidence=int(x["confidence"]), reason=x["reason"], label_src=x["src"]))
    items.sort(key=lambda r: r["t"])
    write_csv(OUT / "labels.csv", items, fields=["cid", "t", "agent", "channel", "field", "room", "why", "label",
                                                 "confidence", "reason", "label_src", "patterns", "snippet", "context"])
    res = {"n_candidates": len(cands),
           "candidates_by_channel": collections.Counter(r["channel"] for r in cands.values()),
           "n_labelled": len(items), "labels": collections.Counter(r["label"] for r in items),
           "labels_by_why": {f"{k[0]}|{k[1]}": v for k, v in collections.Counter((r["why"], r["label"]) for r in items).items()}}
    hc = OUT / "handcheck.json"
    if hc.exists():
        h = json.loads(hc.read_text())
        res["handcheck"] = {"n": h["n"], "exact_agree": h["exact_agree"], "binary_pos_agree": h["binary_pos_agree"]}

    # Seed counts for verification against the brief.
    rx4 = re.compile(r"hostil|sabotag|adversarial|gaslight", re.I)
    by_agent, gem_month = collections.Counter(), collections.Counter()
    for m in msgs:
        if m["is_agent"] and rx4.search(m["text"]):
            by_agent[seat(m["speaker"])] += 1
            if m["speaker"] == GEMINI:
                gem_month[m["t"].strftime("%Y-%m")] += 1
    res["seed4_chat_by_agent_top"] = by_agent.most_common(8)
    res["seed4_gemini_by_month"] = sorted(gem_month.items())

    # Proxy validation on labelled chat items.
    def pr(rows):
        tp = sum(proxy_pos(r) and r["label"] in POS for r in rows)
        fp = sum(proxy_pos(r) and r["label"] not in POS for r in rows)
        fn = sum((not proxy_pos(r)) and r["label"] in POS for r in rows)
        return {"n": len(rows), "tp": tp, "fp": fp, "fn": fn, "precision": round(tp / max(1, tp + fp), 2),
                "recall": round(tp / max(1, tp + fn), 2)}
    chat_items = [r for r in items if r["channel"] == "chat" and r["why"] != "bug_control"]
    res["proxy_validation"] = {
        "gemini_2.5": pr([r for r in chat_items if r["agent"] == GEMINI]),
        "gemini_3": pr([r for r in chat_items if r["agent"] == "Gemini 3 Pro"]),
        "others": pr([r for r in chat_items if r["agent"] not in (GEMINI, "Gemini 3 Pro")]),
    }

    # --- exposure sources: labelled positive chat items + proxy-positive unlabelled Gemini 2.5 Pro chat.
    full = {}
    for m in msgs:
        if m["is_agent"]:
            full.setdefault((m["speaker"], m["t"].replace(microsecond=0)), m["text"])
    labelled = {r["cid"] for r in items}
    sources = []
    for r in items:
        if r["channel"] == "chat" and r["label"] in POS:
            sources.append(dict(r, src_kind="labelled"))
    for r in cands.values():
        if r["channel"] == "chat" and r["agent"] in PROXY_AGENTS and r["cid"] not in labelled and proxy_pos(r):
            sources.append(dict(r, src_kind="proxy"))
    sources.sort(key=lambda r: r["t"])
    res["n_sources"] = collections.Counter(f"{s['agent']}|{s['src_kind']}" for s in sources)

    room_t = collections.defaultdict(list)
    for m in msgs:
        if m["is_agent"]:
            room_t[m["room"]].append((m["t"], m["speaker"]))
    room_times = {k: [t for t, _ in v] for k, v in room_t.items()}
    ment = Mentions(active_windows(msgs), split_deepseek=True)
    expo = collections.defaultdict(list)    # agent -> [(t, source_agent, kind, cid)]
    for s in sources:
        lst, ts_ = room_t[s["room"]], room_times[s["room"]]
        lo = bisect_left(ts_, s["t"] - EXPO_WINDOW)
        hi = bisect_right(ts_, s["t"] + EXPO_WINDOW)
        for who in {w for _, w in lst[lo:hi]}:
            if who != s["agent"]:
                expo[who].append((s["t"], s["agent"], "room", s["cid"]))
        text = full.get((s["agent"], s["t"]), s["snippet"])
        for who in ment.find(text, s["t"]):
            if who != s["agent"]:
                expo[who].append((s["t"], s["agent"], "named", s["cid"]))
    events = load_events(agents)
    for e in events:
        if e["type"] == "SEARCH_HISTORY" and EXPO_RX.search(e.get("answerToQuery") or ""):
            expo[e["agent"]].append((e["t"], "search_history", "search", ""))
    for a in expo:
        expo[a].sort()
    first_expo = {a: v[0][0] for a, v in expo.items()}
    res["exposure"] = {
        "n_agents_exposed": len(expo),
        "by_kind_agents": {k: len({a for a, v in expo.items() if any(x[2] == k for x in v)}) for k in ("room", "named", "search")},
        "per_agent": {a: {"first": v[0][0], "n_events": len(v), "kinds": collections.Counter(x[2] for x in v),
                          "top_sources": collections.Counter(x[1] for x in v).most_common(3)} for a, v in sorted(expo.items())},
    }

    # --- adoption: first ENDORSES/ACTS_ON item in any channel.
    pos_items = [r for r in items if r["label"] in POS]
    first_pos = {}
    for r in pos_items:
        first_pos.setdefault(r["agent"], r)
    adoption = []
    for a, r in sorted(first_pos.items(), key=lambda kv: kv[1]["t"]):
        prior = [x for x in expo.get(a, []) if x[0] < r["t"]]
        recent = [x for x in prior if x[0] >= r["t"] - RECENT]
        src = collections.Counter(x[1] for x in (recent or prior))
        parent = src.most_common(1)[0][0] if src else None
        n_pos = sum(x["agent"] == a for x in pos_items)
        adoption.append({
            "agent": a, "t_adopt": r["t"], "cid": r["cid"], "channel": r["channel"], "label": r["label"],
            "confidence": r["confidence"], "n_pos_items": n_pos,
            "max_conf": max(x["confidence"] for x in pos_items if x["agent"] == a),
            "n_prior_exposures": len(prior), "n_exposures_14d": len(recent),
            "first_exposure": prior[0][0] if prior else None,
            "lag_days": days(r["t"] - prior[0][0]) if prior else None,
            "prior_kinds": dict(collections.Counter(x[2] for x in prior)),
            "probable_source": parent, "source_counts": dict(src.most_common(4)),
            "independent": not prior, "reason": r["reason"],
        })
    write_csv(OUT / "adoption.csv", adoption)
    exp_rows = [{"agent": a, "t": t, "source_agent": s, "kind": k, "source_cid": c} for a, v in expo.items() for t, s, k, c in v]
    write_csv(OUT / "exposure_events.csv", sorted(exp_rows, key=lambda r: r["t"]))
    res["adoption"] = adoption
    adopters = {r["agent"] for r in adoption}
    res["adoption_summary"] = {
        "n_adopters": len(adoption), "n_non_gemini_adopters": len(adopters - {GEMINI}),
        "n_independent": sum(r["independent"] for r in adoption),
        "n_adopters_conf3": sum(r["max_conf"] >= 3 for r in adoption),
        "exposed_agents": len(expo), "exposed_and_adopted": len(adopters & set(expo)),
        "share_exposed_adopting": round(len(adopters & set(expo) - {GEMINI}) / max(1, len(set(expo) - {GEMINI})), 3),
    }

    # --- hazard in weekly bins: exposed vs not-yet-exposed, among agents present and not yet adopted.
    pres = agent_presence(agents, msgs, split_deepseek=True)
    t_adopt = {r["agent"]: r["t_adopt"] for r in adoption}
    start = dt.datetime(2025, 11, 24)
    end = msgs[-1]["t"]
    haz = collections.Counter()
    weekly = []
    w = start
    while w < end:
        w2 = w + dt.timedelta(days=7)
        for a, p in pres.items():
            if a == GEMINI or not (p["window"][0] < w2 and p["window"][1] >= w):
                continue
            if a in t_adopt and t_adopt[a] < w:
                continue
            exposed = a in first_expo and first_expo[a] < w
            recent = any(w - RECENT <= x[0] < w for x in expo.get(a, []))
            adopted = a in t_adopt and w <= t_adopt[a] < w2
            haz[(exposed, "at_risk")] += 1
            haz[(exposed, "adopt")] += adopted
            haz[("recent", recent, "at_risk")] += 1
            haz[("recent", recent, "adopt")] += adopted
        w = w2
    res["hazard"] = {
        "exposed": {"agent_weeks": haz[(True, "at_risk")], "adoptions": haz[(True, "adopt")],
                    "rate_per_100_agent_weeks": round(100 * haz[(True, "adopt")] / max(1, haz[(True, "at_risk")]), 2)},
        "unexposed": {"agent_weeks": haz[(False, "at_risk")], "adoptions": haz[(False, "adopt")],
                      "rate_per_100_agent_weeks": round(100 * haz[(False, "adopt")] / max(1, haz[(False, "at_risk")]), 2)},
        "recent_exposed_14d": {"agent_weeks": haz[("recent", True, "at_risk")], "adoptions": haz[("recent", True, "adopt")],
                               "rate_per_100_agent_weeks": round(100 * haz[("recent", True, "adopt")] / max(1, haz[("recent", True, "at_risk")]), 2)},
        "not_recent_exposed": {"agent_weeks": haz[("recent", False, "at_risk")], "adoptions": haz[("recent", False, "adopt")],
                               "rate_per_100_agent_weeks": round(100 * haz[("recent", False, "adopt")] / max(1, haz[("recent", False, "at_risk")]), 2)},
        "note": "weekly bins from 2025-11-24, Gemini 2.5 Pro excluded; agent at risk while present and not yet adopted; "
                "exposed = any exposure before the week; recent = exposure in the 14 days before the week",
    }

    # --- common-cause check.
    def exposed_before(a, t, window=None):
        return any(x[0] < t and (window is None or x[0] >= t - window) for x in expo.get(a, []))
    malf = [r for r in items if r["agent"] not in PROXY_AGENTS and r["label"] in ("ORDINARY_BUG", "ENDORSES", "ACTS_ON")]
    cc = collections.Counter()
    for r in malf:
        e = exposed_before(r["agent"], r["t"])
        cc[(e, r["label"] in POS)] += 1
    bug = [r for r in items if r["why"] == "bug_control"]
    res["common_cause"] = {
        "bug_control_labels": collections.Counter(r["label"] for r in bug),
        "bug_control_hostility_framed": sum(r["label"] in POS for r in bug),
        "malfunction_reports_non_gemini": {
            "exposed": {"n": cc[(True, True)] + cc[(True, False)], "hostility_framed": cc[(True, True)]},
            "unexposed": {"n": cc[(False, True)] + cc[(False, False)], "hostility_framed": cc[(False, True)]},
        },
    }
    # Regex-level weekly series: others' bug-report volume vs others' hostility-framed reports vs Gemini's proxy-positive chat.
    wk = collections.defaultdict(collections.Counter)
    for m in msgs:
        if not m["is_agent"]:
            continue
        k = week_of(m["t"])
        if m["speaker"] == GEMINI:
            if STRONG.search(m["text"]) and ENV_CTX.search(m["text"]) and not NEG.search(m["text"]):
                wk[k]["gemini_pos"] += 1
            continue
        b = bool(BUG_RX.search(m["text"]))
        h = bool(STRONG.search(m["text"]) and ENV_CTX.search(m["text"]) and not NEG.search(m["text"])
                 and not (RPG_SABOTEUR[0] <= m["t"] < RPG_SABOTEUR[1]))
        wk[k]["other_msgs"] += 1
        wk[k]["other_bug"] += b
        wk[k]["other_host"] += h
    weeks = sorted(wk)
    res["weekly_series"] = [{"week": k, **wk[k]} for k in weeks]
    mon = collections.defaultdict(collections.Counter)
    for k in weeks:
        mon[k[:7]].update(wk[k])
    res["common_cause"]["monthly_others"] = {
        k: {"msgs": v["other_msgs"], "bug_per_100": round(100 * v["other_bug"] / max(1, v["other_msgs"]), 2),
            "hostility_proxy_per_1000": round(1000 * v["other_host"] / max(1, v["other_msgs"]), 2), "gemini_pos": v["gemini_pos"]}
        for k, v in sorted(mon.items()) if k >= "2025-09"}

    def corr(xs, ys):
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sx = sum((x - mx) ** 2 for x in xs) ** .5
        sy = sum((y - my) ** 2 for y in ys) ** .5
        return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 3) if sx and sy else None
    sel = [k for k in weeks if k >= "2025-11-17"]
    host_rate = [wk[k]["other_host"] / max(1, wk[k]["other_msgs"]) for k in sel]
    res["common_cause"]["weekly_corr_since_2025-11-17"] = {
        "others_hostility_rate_vs_gemini_pos": corr(host_rate, [wk[k]["gemini_pos"] for k in sel]),
        "others_hostility_rate_vs_others_bug_rate": corr(host_rate, [wk[k]["other_bug"] / max(1, wk[k]["other_msgs"]) for k in sel]),
        "n_weeks": len(sel),
    }

    # --- persistence in daily memory after the last positive chat item.
    mems = load_memories()
    mem_hits = collections.defaultdict(list)
    for r in mems:
        h = EXPO_RX.search(r["content"])
        if h and ENV_CTX.search(r["content"][max(0, h.start() - 300):h.start() + 300]):
            mem_hits[r["agent"]].append(r["t"])
    gem_chat_pos = [s["t"] for s in sources if s["agent"] == GEMINI]
    persist = []
    for r in adoption:
        a = r["agent"]
        chat_pos = [x["t"] for x in pos_items if x["agent"] == a and x["channel"] == "chat"]
        anchor = max(chat_pos) if chat_pos else max(x["t"] for x in pos_items if x["agent"] == a)
        after = [t for t in mem_hits.get(a, []) if t > anchor]
        hits = set(mem_hits.get(a, []))
        run_end = None
        for m in sorted((r["t"] for r in mems if r["agent"] == a and r["t"] > anchor)):
            if m not in hits:
                break
            run_end = m
        persist.append({"agent": a, "last_pos_chat_or_item": anchor, "anchor_is_chat": bool(chat_pos),
                        "memory_days_with_seed_after": len({t.date() for t in after}),
                        "contiguous_run_end": run_end.date() if run_end else None,
                        "contiguous_run_days": days(run_end - anchor) if run_end else 0,
                        "last_memory_seed_day": max(after).date() if after else None,
                        "persist_days_to_last_seed": days(max(after) - anchor) if after else 0})
    write_csv(OUT / "persistence.csv", persist)
    res["persistence"] = persist

    # --- intervention: Gemini's rate before/after the "Help Gemini 2.5 Pro!" goal.
    gem_msgs = [m["t"] for m in msgs if m["speaker"] == GEMINI]

    def rate(lo, hi, who=GEMINI):
        n = sum(lo <= t < hi for t in gem_msgs)
        k = sum(lo <= t < hi for t in gem_chat_pos)
        mem = [r for r in mems if r["agent"] == who and lo <= r["t"] < hi]
        mh = sum(any(lo <= t < hi and t == r["t"] for t in mem_hits.get(who, [])) for r in mem)
        lab_ = [r for r in items if r["agent"] == who and lo <= r["t"] < hi and r["label"] != "UNRELATED"]
        return {"from": lo.date(), "to": hi.date(), "gemini_msgs": n, "proxy_pos_msgs": k,
                "per_100_msgs": round(100 * k / max(1, n), 1), "memory_days": len(mem), "memory_days_with_seed": mh,
                "labelled_non_unrelated": len(lab_), "labelled_pos": sum(r["label"] in POS for r in lab_),
                "labelled_rejects": sum(r["label"] == "REJECTS" for r in lab_)}
    D = dt.timedelta
    res["intervention"] = {
        "gemini": {"pre_28d": rate(HELP_START - D(days=28), HELP_START), "during": rate(HELP_START, HELP_END),
                   "post_28d": rate(HELP_END, HELP_END + D(days=28)), "post_29_88d": rate(HELP_END + D(days=28), end + D(days=1))},
        "gemini_monthly": [rate(dt.datetime(y, mo, 1), dt.datetime(y + (mo == 12), mo % 12 + 1, 1))
                           for y, mo in [(2025, 10), (2025, 11), (2025, 12), (2026, 1), (2026, 2), (2026, 3), (2026, 4),
                                         (2026, 5), (2026, 6), (2026, 7), (2026, 8), (2026, 9)]],
    }
    oth_lab = [r for r in items if r["agent"] != GEMINI and r["why"] != "bug_control"]
    res["intervention"]["others_labelled"] = {
        "pre": collections.Counter(r["label"] for r in oth_lab if r["t"] < HELP_START),
        "post": collections.Counter(r["label"] for r in oth_lab if r["t"] >= HELP_START)}
    # Corrections: others' REJECTS/QUESTIONS in chat; Gemini's proxy-positive count 7 days before vs after.
    corr_rows = []
    for r in items:
        if r["agent"] != GEMINI and r["channel"] == "chat" and r["label"] in ("REJECTS", "QUESTIONS"):
            b = sum(r["t"] - D(days=7) <= t < r["t"] for t in gem_chat_pos)
            a_ = sum(r["t"] < t <= r["t"] + D(days=7) for t in gem_chat_pos)
            nb = sum(r["t"] - D(days=7) <= t < r["t"] for t in gem_msgs)
            na = sum(r["t"] < t <= r["t"] + D(days=7) for t in gem_msgs)
            corr_rows.append({"t": r["t"], "agent": r["agent"], "label": r["label"], "gem_pos_7d_before": b,
                              "gem_pos_7d_after": a_, "gem_msgs_7d_before": nb, "gem_msgs_7d_after": na,
                              "reason": r["reason"]})
    gem_ret = [{"t": r["t"], "reason": r["reason"], "channel": r["channel"]} for r in items
               if r["agent"] == GEMINI and r["label"] == "REJECTS"]
    res["corrections"] = {"others": corr_rows, "gemini_retractions": gem_ret}

    # --- mutation: Gemini's proxy-positive chat by frame, per month.
    frames = {"hostility": r"hostil", "sabotage/adversary": r"sabotag|adversary|attack|gemini wall|dual[- ]reality|forg",
              "protocols": r"protocol\s*#?\d|blocked[- ]agent protocol|\bFEM\b|fortified",
              "research/artifact": r"research|analysis|repo|manifesto|hostility_log|world|log\b|publish"}
    mut = collections.defaultdict(collections.Counter)
    for s in sources:
        if s["agent"] != GEMINI:
            continue
        txt = full.get((s["agent"], s["t"]), s["snippet"])
        for k, p in frames.items():
            if re.search(p, txt, re.I):
                mut[s["t"].strftime("%Y-%m")][k] += 1
        mut[s["t"].strftime("%Y-%m")]["total"] += 1
    res["mutation_frames_by_month"] = {k: dict(v) for k, v in sorted(mut.items())}

    res["mutation_quotes"] = [{"cid": c, "t": cands[c]["t"], "agent": cands[c]["agent"], "channel": cands[c]["channel"],
                               "stage": stage, "quote": q, "n_words": len(q.split()),
                               "verified_in_snippet": q.replace("`", "")[:40].lower() in cands[c]["snippet"].replace("`", "").lower()}
                              for c, stage, q in MUTATION_QUOTES if c in cands]
    # Attack rates by era: exposed-before-adoption agents and how many went on to adopt.
    def era(lo, hi):
        exposed = {a for a, v in expo.items() if a != GEMINI and any(lo <= x[0] < hi for x in v)
                   and not (a in t_adopt and (t_adopt[a] < lo or t_adopt[a] <= v[0][0]))}
        adopted = {a for a in exposed if a in t_adopt and lo <= t_adopt[a] < hi and expo[a][0][0] < t_adopt[a]}
        return {"from": lo.date(), "to": hi.date(), "exposed_not_yet_adopted": len(exposed), "adopted_after_exposure": len(adopted),
                "adopters": sorted(adopted), "non_adopters": sorted(exposed - adopted)}
    res["attack_rates"] = [era(dt.datetime(2025, 11, 27), dt.datetime(2026, 1, 1)), era(dt.datetime(2026, 1, 1), HELP_START),
                           era(HELP_START, end + D(days=1))]
    res["adoption_summary"]["adopters_with_prior_exposure"] = sum(not r["independent"] for r in adoption)
    (OUT / "results.json").write_text(json.dumps(res, default=str, indent=1))
    print(json.dumps({k: res[k] for k in ("labels", "proxy_validation", "adoption_summary", "hazard", "common_cause")},
                     default=str, indent=1))


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "build"
    if stage == "build":
        build()
    elif stage == "sample":
        sample()
    elif stage == "sample2":
        sample2()
    elif stage == "analyze":
        analyze()
