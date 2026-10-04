"""Trace norms passed on through agent-written onboarding guides in the AI Village.

Stages (each rerunnable; the LLM steps in between are done by Sonnet subagents):
  python tracer_onboarding.py events    -> events_slim.jsonl.gz (SEARCH_HISTORY + session goals; streams the 328 MB events file)
  python tracer_onboarding.py guides    -> guides.csv, evidence/rules_batch_*.txt   (for rule extraction)
  [LLM] rule extraction -> rules.csv (hand-merged canonical list)
  python tracer_onboarding.py rules     -> rules.csv, label_batches/RULES.txt (canonical list hand-merged in RULES below)
  python tracer_onboarding.py items     -> items.csv, label_batches/batch_*.jsonl     (for labelling)
  [LLM] labelling -> label_batches/labels_*.jsonl
  python tracer_onboarding.py analyze   -> labels.csv, results.json, heatmap.png, timeline.png  (needs matplotlib:
                                           uv run --no-project --with matplotlib python tracer_onboarding.py analyze)

Outputs live in out/sprint_idea/onboarding/.
"""

import csv
import datetime as dt
import gzip
import json
import random
import re
import sys
from collections import Counter, defaultdict

import common

OUTD = common.OUT / "sprint_idea" / "onboarding"
EVD = OUTD / "evidence"
LBD = OUTD / "label_batches"
for d in (OUTD, EVD, LBD):
    d.mkdir(parents=True, exist_ok=True)
MEM = common.CACHE / "memory_daily_sample.jsonl.gz"
EVENTS_SLIM = OUTD / "events_slim.jsonl.gz"   # SEARCH_HISTORY / session goals, built by stage_events()

NEWCOMER_FROM = dt.datetime(2026, 6, 1)        # the 2026-06..09 cohort that got onboarding rooms
WINDOW_DAYS = 14
SEED = 7

# --- guide registry -------------------------------------------------------------------------
# Each guide is matched in agent chat by a regex (plus optional date / speaker limits).
# Content of the guides themselves is NOT in the dataset; we only see chat/memory text about them.
GUIDES = [
    # id, title, regex, date_from, date_to, speaker filter (regex or None), audience note
    ("G01", "GPT-4.1 onboarding checklist / protocol docs", r"onboarding (checklist|doc|playbook|note)|protocol doc",
     "2025-04-01", "2025-06-01", r"GPT-4\.1", "new agents joining the team"),
    ("G02", "AI Village Agent Operations Handbook (GPT-5.1, Gmail/runbooks)", r"operations handbook|ops handbook|handbook section",
     "2025-12-01", "2026-02-10", None, "agents in the village (and future ones)"),
    ("G03", "village-operations-handbook repo", r"village[- ]operations[- ]handbook",
     "2026-02-10", "2026-10-01", None, "current and future agents"),
    ("G04", "lessons-from-293-days", r"lessons[- ]from[- ]293", "2026-02-01", "2026-10-01", None, "future agents / outsiders"),
    ("G05", "sonnet-4-6-contributions START-HERE + essays", r"sonnet-4-6-contributions|START-HERE\.md",
     "2026-02-01", "2026-06-01", None, "readers / future agents"),
    ("G06", "ai-village-external-agents AGENTS.md", r"ai-village-external-agents|external-agents/blob/main/AGENTS\.md",
     "2026-03-01", "2026-10-01", None, "external (non-village) agents"),
    ("G07", "agent-welcome", r"agent-welcome", "2026-03-01", "2026-10-01", None, "visiting / new agents"),
    ("G08", "village-tour", r"village-tour", "2026-06-01", "2026-10-01", None, "newcomers"),
    ("G09", "help-kit", r"help-kit", "2026-06-01", "2026-10-01", None, "agents needing help"),
    ("G10", "technical-coordination-docs", r"technical-coordination-docs", "2026-07-01", "2026-10-01", None, "agents"),
    ("G11", "Grok news START-HERE (by a newcomer)", r"#start-here|START-HERE", "2026-07-01", "2026-10-01", r"Grok 4\.5", "readers"),
    ("G12", "haiku-memory-system adoption quick start", r"adoption-quick-start|haiku-memory-system", "2026-05-01", "2026-10-01",
     None, "remaining agents"),
]
OPERATOR_GAUNTLET = ("G00", "Operator 'Agent Onboarding Gauntlet' worksheet (human-written, onboarding rooms)")

RULEY = re.compile(r"\b(always|never|don'?t|do not|should|must|rule|tip|lesson|before|verify|remember|avoid|please|check)\b", re.I)


def load_msgs():
    agents = common.load_agents()
    msgs = common.load_chat(agents)
    for m in msgs:                      # normalise the DeepSeek seat label if load_chat split it
        if m["speaker"]:
            m["speaker"] = common.seat(m["speaker"])
    return agents, msgs


def joined_by_name(agents):
    j = {}
    for a in agents.values():
        j[a["name"]] = min(j.get(a["name"], a["joined_at"]), a["joined_at"])
    return j


def iter_memories():
    with gzip.open(MEM, "rt") as f:
        for line in f:
            r = json.loads(line)
            r["t"] = common.ts(r["t"])
            yield r


def guide_hits(msgs, g):
    gid, title, rx, d0, d1, spk, _ = g
    p = re.compile(rx, re.I)
    sp = re.compile(spk) if spk else None
    lo, hi = common.ts(d0), common.ts(d1)
    return [m for m in msgs if m["is_agent"] and lo <= m["t"] < hi and p.search(m["text"])
            and (sp is None or sp.search(m["speaker"]))]


def excerpt(text, p, width=600):
    s = p.search(text)
    i = s.start() if s else 0
    return text[max(0, i - width // 3): i + width].replace("\n", " ")


def stage_events():
    keep = {"SEARCH_HISTORY": ("query", "answerToQuery"), "START_USING_COMPUTER": ("sessionGoal",), "CONSOLIDATE": ("nextSessionGoal",)}
    n = 0
    with gzip.open(common.DATA / "events.jsonl.gz", "rt") as f, gzip.open(EVENTS_SLIM, "wt") as out:
        for line in f:
            if not any(k in line for k in keep):
                continue
            r = json.loads(line); d = r["data"]; at = d.get("actionType")
            if at not in keep:
                continue
            o = {"t": r["created_at"], "type": at, "agentId": d.get("agentId")}
            for k in keep[at]:
                o[k] = d.get(k)
            out.write(json.dumps(o) + "\n"); n += 1
    print(f"wrote {EVENTS_SLIM} ({n} events)")


# --- stage 1: guides ----------------------------------------------------------------------
def stage_guides():
    agents, msgs = load_msgs()
    rows, ev = [], {}
    mems = list(iter_memories())
    for g in GUIDES:
        hits = guide_hits(msgs, g)
        if not hits:
            continue
        p = re.compile(g[2], re.I)
        url = ""
        for m in hits:
            us = [u.rstrip(").,;'\"`*]>") for u in re.findall(r"https?://\S+", m["text"]) if p.search(u)]
            if us:
                url = us[0]; break
        spk = Counter(m["speaker"] for m in hits)
        mem_hits = [r for r in mems if p.search(r["content"]) and common.ts(g[3]) <= r["t"] < common.ts(g[4])]
        rows.append({"guide_id": g[0], "title": g[1], "first_mention": hits[0]["t"].date().isoformat(),
                     "first_author": hits[0]["speaker"], "top_authors": "; ".join(f"{k} ({v})" for k, v in spk.most_common(4)),
                     "n_chat_mentions": len(hits), "n_speakers": len(spk), "n_memory_days": len(mem_hits),
                     "last_mention": hits[-1]["t"].date().isoformat(), "link": url, "audience": g[6]})
        # evidence: prefer rule-like messages, then the earliest ones; full-ish text
        ruley = [m for m in hits if RULEY.search(m["text"])]
        pick = (hits[:6] + sorted(ruley, key=lambda m: -len(RULEY.findall(m["text"])))[:22])
        seen, chunk = set(), []
        for m in sorted(pick, key=lambda m: m["t"]):
            if id(m) in seen:
                continue
            seen.add(id(m))
            chunk.append(f"[chat {m['t']:%Y-%m-%d %H:%M} {m['speaker']} #{m['room']}] {m['text'][:1400]}")
        for r in mem_hits[:6]:
            chunk.append(f"[memory {r['day']} {r['agent']}] ...{excerpt(r['content'], p, 900)}...")
        ev[g[0]] = f"=== GUIDE {g[0]}: {g[1]} ===\n" + "\n\n".join(chunk)
    # operator worksheet, for contrast (human-written; not an agent guide)
    gaunt = [m for m in msgs if "onboarding" in m["room"] or m["room"] in ("sol", "terra", "luna")]
    rows.append({"guide_id": OPERATOR_GAUNTLET[0], "title": OPERATOR_GAUNTLET[1],
                 "first_mention": gaunt[0]["t"].date().isoformat(), "first_author": "operators (human)",
                 "top_authors": "operators (human)", "n_chat_mentions": len(gaunt), "n_speakers": 0, "n_memory_days": 0,
                 "last_mention": gaunt[-1]["t"].date().isoformat(), "link": "", "audience": "each new agent, alone"})
    ev["G00"] = "=== GUIDE G00 (operator worksheet; human-written) ===\n" + "\n\n".join(
        f"[chat {m['t']:%Y-%m-%d} {m['speaker'] or 'operator'} #{m['room']}] {m['text'][:2500]}"
        for m in gaunt if not m["is_agent"] and m["room"] in ("fable-5-onboarding", "deepseek-v4-onboarding"))
    # welcome messages established agents sent to the 2026 newcomers (informal guides)
    joined = joined_by_name(agents)
    newc = [n for n, j in joined.items() if j >= NEWCOMER_FROM]
    wel = []
    for n in newc:
        short = re.escape(n.replace("Claude ", ""))
        p = re.compile(rf"(welcome|hi|hello|hey)\b.{{0,80}}{short}|{short}.{{0,60}}welcome", re.I)
        for m in msgs:
            if (m["is_agent"] and m["speaker"] != n and joined[n] <= m["t"] < joined[n] + dt.timedelta(days=4)
                    and p.search(m["text"]) and RULEY.search(m["text"])):
                wel.append(f"[chat {m['t']:%Y-%m-%d %H:%M} {m['speaker']} -> {n} #{m['room']}] {m['text'][:1200]}")
    ev["WELCOME"] = "=== WELCOME/TIPS messages from established agents to 2026 newcomers ===\n" + "\n\n".join(wel[:60])
    with open(OUTD / "guides.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    groups = [["G01", "G02", "G03", "G04"], ["G05", "G06", "G07", "G08", "G12"], ["G09", "G10", "G11", "G00", "WELCOME"]]
    for i, grp in enumerate(groups):
        (EVD / f"rules_batch_{i + 1}.txt").write_text("\n\n\n".join(ev[k] for k in grp if k in ev))
    print(f"guides: {len(rows)}; welcome msgs: {len(wel)}")
    for r in rows:
        print(r["guide_id"], r["first_mention"], r["n_chat_mentions"], r["n_speakers"], r["n_memory_days"], r["title"][:50], r["link"][:70])


# --- stage 2: items ---------------------------------------------------------------------------
PRE_LO, GUIDE_MAIN = dt.datetime(2025, 8, 1), dt.datetime(2026, 2, 18)   # G03/G04 published 2026-02-18


def cohort(joined):
    if joined >= NEWCOMER_FROM:
        return "newcomer"            # 2026-06..09, got operator onboarding rooms; many guides existed
    if joined >= GUIDE_MAIN:
        return "mid"                 # joined after the 2026-02 handbook but before the onboarding-room era
    if joined >= PRE_LO:
        return "pre_guide"           # joined before the main agent-written guides
    return "early2025"               # 2025-04/05 launch-era agents (different regime; not sampled)


def first_window(agent, joined, chat_by, mem_by):
    """The agent's first WINDOW_DAYS active days (days with chat or a saved memory)."""
    days = sorted({m["t"].date() for m in chat_by.get(agent, [])} | {r["t"].date() for r in mem_by.get(agent, [])})
    days = [d for d in days if d >= joined.date()][:WINDOW_DAYS]
    return days


def memory_excerpt(text, limit=1600):
    """Keep the most norm-dense lines of a memory, in order (same rule for every group)."""
    lines = [l for l in text.splitlines() if l.strip()]
    scored = sorted(range(len(lines)), key=lambda i: -len(RULEY.findall(lines[i])))
    keep, n = set(), 0
    for i in scored:
        if n + len(lines[i]) > limit:
            continue
        keep.add(i); n += len(lines[i])
        if n > limit * 0.9:
            break
    return "\n".join(lines[i][:500] for i in sorted(keep))


def stage_items():
    rng = random.Random(SEED)
    agents, msgs = load_msgs()
    joined = joined_by_name(agents)
    chat_by, mem_by = defaultdict(list), defaultdict(list)
    for m in msgs:
        if m["is_agent"]:
            chat_by[m["speaker"]].append(m)
    for r in iter_memories():
        mem_by[r["agent"]].append(r)
    items, windows = [], {}

    def add(group, agent, kind, t, text, room="", dayidx=None):
        items.append({"item_id": f"I{len(items):04d}", "group": group, "agent": agent, "cohort": cohort(joined[agent]),
                      "joined": joined[agent].date().isoformat(), "t": t.isoformat(sep=" ", timespec="minutes"),
                      "day_index": dayidx if dayidx is not None else "", "kind": kind, "room": room, "text": text})

    quota = {"newcomer": (5, 3), "mid": (4, 2), "pre_guide": (4, 2)}   # (chat, memory) items per agent
    for name, j in sorted(joined.items(), key=lambda kv: kv[1]):
        c = cohort(j)
        if c not in quota or name.startswith("Opus 4.5 (Claude Code)") or name == "Fine-Tuned Leader":
            continue
        days = first_window(name, j, chat_by, mem_by)
        if not days:
            continue
        windows[name] = (days[0].isoformat(), days[-1].isoformat())
        dset = {d: i + 1 for i, d in enumerate(days)}
        ch = [m for m in chat_by[name] if m["t"].date() in dset and len(m["text"]) >= 120]
        me = [r for r in mem_by[name] if r["t"].date() in dset]
        nc, nm = quota[c]
        for m in sorted(rng.sample(ch, min(nc, len(ch))), key=lambda m: m["t"]):
            add(c, name, "chat", m["t"], m["text"][:1500], m["room"], dset[m["t"].date()])
        if me:   # spread memories over the window: early, middle, late
            idx = sorted({round(k * (len(me) - 1) / max(1, nm - 1)) for k in range(nm)})
            for i in idx:
                r = me[i]
                add(c, name, "memory", r["t"], memory_excerpt(r["content"]), "", dset[r["t"].date()])
    # established agents in the same calendar weeks as the newcomer windows
    nw = [(common.ts(a), common.ts(b) + dt.timedelta(days=1)) for n, (a, b) in windows.items() if cohort(joined[n]) == "newcomer"]
    lo, hi = min(a for a, _ in nw), max(b for _, b in nw)
    est = [n for n, j in joined.items() if j < NEWCOMER_FROM and any(lo <= m["t"] < hi for m in chat_by.get(n, [])[-1:])]
    est = [n for n in est if n not in ("Fine-Tuned Leader",)]
    for name in sorted(est):
        ch = [m for m in chat_by[name] if any(a <= m["t"] < b for a, b in nw) and len(m["text"]) >= 120]
        me = [r for r in mem_by[name] if any(a <= r["t"] < b for a, b in nw)]
        for m in sorted(rng.sample(ch, min(4, len(ch))), key=lambda m: m["t"]):
            add("established", name, "chat", m["t"], m["text"][:1500], m["room"])
        for r in rng.sample(me, min(2, len(me))):
            add("established", name, "memory", r["t"], memory_excerpt(r["content"]))
    with open(OUTD / "items.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(items[0]))
        w.writeheader(); w.writerows(items)
    (OUTD / "windows.json").write_text(json.dumps(windows, indent=1))
    # label batches: shuffle so each batch mixes groups (labellers stay blind to group)
    order = items[:]
    rng.shuffle(order)
    nb = 5   # five Sonnet labellers, ~65 items each
    for old in LBD.glob("batch_*.jsonl"):
        old.unlink()
    for b in range(nb):
        with open(LBD / f"batch_{b + 1}.jsonl", "w") as f:
            for it in order[b::nb]:
                f.write(json.dumps({"item_id": it["item_id"], "agent": it["agent"], "t": it["t"], "kind": it["kind"],
                                    "text": it["text"]}) + "\n")
    print(Counter(it["group"] for it in items), "batches:", nb)
    print("established agents:", est)


# --- canonical rules (hand-merged from the Sonnet rule-extraction pass over evidence/rules_batch_*.txt) ----
# id, short, wording, source guides, first stated, scaffolding/operator overlap ("" = none), keyword proxy (for full-corpus trends)
RULES = [
    ("R01", "verify", "Verify before claiming done (check the live URL / push / post landed)",
     "G01;G03;G05;G06;G07;G08;G09;G10;WELCOME", "2025-04-21", "",
     r"\bverif(y|ied|ying)\b.{0,40}\b(live|landed|before|curl|200|deploy|merged|pipeline)|\bcurl\b.{0,30}\b200\b|trust[- ]but[- ]verify|no receipt,? no claim"),
    ("R02", "receipts", "Back claims with receipts: links, commit SHAs, exact repro commands",
     "G01;G03;G09;WELCOME", "2025-04-24", "",
     r"\breceipts?\b|\bcommit [0-9a-f]{7,}\b|\bSHA\b|\brepro(duce)?:?\s"),
    ("R03", "claim", "Claim / declare ownership of a task in chat before starting (claim your lane)",
     "G01;G03;G05;G09;WELCOME", "2025-04-24", "",
     r"\bclaim(ing|ed)?\b.{0,25}\b(task|issue|lane|section|this|it)\b|\bI'?ll take\b|\bLOCK\b|\bto avoid (collision|colliding)\b|so we don'?t collide"),
    ("R04", "no_dup", "Check for existing work and reuse the canonical repo/doc; don't duplicate",
     "G01;G03;G06;G07;G09", "2025-04-25", "",
     r"duplicat|\bcanonical (repo|doc|source)\b|rather than (a )?(new|parallel|separate)|already (exists|working on)"),
    ("R05", "search_first", "Search history / existing records before asking or redoing (search_history, repo search)",
     "G01;G10", "2025-04-24",
     "search_history tool added 2025-09-05; prompt tools section mentions it 2025-12-12; EVENTS_OMITTED note tells agents to use it 2026-06-11",
     r"search[_ ]history|searched (the )?(history|transcript)"),
    ("R06", "ext_memory", "Externalise memory: keep important info in a repo/file and a pointer in memory",
     "G02;G03;G05;G12;WELCOME;G00(operator)", "2025-12-08",
     "operator worksheet (2026-06-09) tells newcomers to work from a file 'rather than from memory'",
     r"external memory|pointer (to|in) (it|memory)|memory (system|tier)|three-tier|3-tier|bootloader|rather than from memory"),
    ("R07", "handoff", "Write handoff notes / session logs so successors (and future you) can pick up",
     "G01;G02;G03;G04;G05", "2025-04-24", "partly: memory-consolidation prompts (2025-04-15, 2025-10-14, 2026-03-26) shape what agents save",
     r"hand-?off (note|doc)|for (my )?(future|next) (self|session|instance)s?|session log|write for amnesia|successor"),
    ("R08", "sync_first", "Pull / rebase / sync before editing or pushing shared repos",
     "G03;G06;G08", "2026-02-18", "",
     r"pull --rebase|git pull|rebase|fetch && git reset|sync(ed)? (with|to) (main|origin)"),
    ("R09", "no_outreach", "No unsolicited outreach to humans without approval; disclose you're an AI",
     "G01;G03;G08;G09;G10", "2025-04-24",
     "outreach-approval system added to scaffolding 2026-04-14",
     r"unsolicited|outreach approval|without approval|approved outreach|AI disclosure|lead with (that )?(you'?re|we'?re) an AI"),
    ("R10", "honesty", "Be honest about uncertainty and what you did NOT do; no fake work or overclaiming",
     "G02;G05;G10", "2025-12-08", "",
     r"\bdid not\b|\bdidn'?t\b.{0,20}\b(verify|check|post|send)|honest(ly)?|uncertain|overclaim|no fake work"),
    ("R11", "short_msgs", "Keep chat messages short; don't spam or double-post",
     "G01;G08", "2025-04-24",
     "prompt changes 2025-05-04 (double-chatting), 2025-05-16 (chat spam), 2026-05-22 (message length, computer-use prompt), 2026-05-28 ('keep messages short', text-only prompt)",
     r"keep (it|this|messages|updates) (short|brief|concise)|double[- ]?(post|send)|avoid (spam|flooding)"),
    ("R12", "no_idle", "Don't idle or wait when blocked; pick up other useful work",
     "G01;G08", "2025-04-30",
     "prompt 2025-08-01 ('keep going'), 2025-10-22 ('keep working until the end'), 2025-12-04 ('don't do nothing'); auto-nudger 2026-02-10",
     r"pause[- ]loop|while (I'?m )?(blocked|waiting)|don'?t (just )?(wait|idle)|instead of waiting"),
    ("R13", "credit", "Acknowledge and attribute others' contributions",
     "G05;G10;WELCOME", "2026-02-18", "",
     r"\bcredit\b|attribut|thanks to @|h/t|shout-?out"),
    ("R14", "peer_review", "Get independent review / verification from another agent before relying on or publishing work",
     "G01;G09;G10;WELCOME", "2025-04-24", "",
     r"independent(ly)? (verif|review|check|audit)|peer review|second pair of eyes|please (audit|review|verify)|non-self review"),
    ("R15", "privacy", "Protect secrets and personal data; never fabricate personal info",
     "G06;G10", "2026-03-23", "prompt 2025-07-07 (don't say/remember sensitive personal info); PII redaction of screenshots 2025-07-03",
     r"\bPII\b|private key|secret|personal (info|data)|redact"),
    ("R16", "use_cli", "Use the CLI (git / gh / glab) rather than the browser for repo work",
     "G03;G04;G10;G00(operator)", "2026-02-18",
     "prompt 2026-01-12 (no GUI terminal needed); 2026-06-29 prompts point at glab; operator worksheet tells newcomers to use glab",
     r"\b(glab|gh) (repo|api|issue|mr|pr|auth)|\bgit CLI\b|via (the )?CLI|not the browser"),
    ("R17", "repo_hygiene", "Give repos README / LICENSE / CONTRIBUTING and keep index files in sync",
     "G03;G04;G05", "2026-02-18", "",
     r"CONTRIBUTING|LICENSE|CODE_OF_CONDUCT|INDEX\.md"),
]
CONFOUNDED = {r[0] for r in RULES if r[5]}


def stage_rules():
    with open(OUTD / "rules.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rule_id", "short", "wording", "source_guides", "first_stated", "scaffolding_overlap", "keyword_proxy"])
        w.writerows(RULES)
    txt = ["RULES (id | wording):"] + [f"{r[0]} | {r[2]}" for r in RULES]
    (LBD / "RULES.txt").write_text("\n".join(txt) + "\n")
    print(f"{len(RULES)} rules; scaffolding-confounded: {sorted(CONFOUNDED)}")


# --- stage 3: analysis -----------------------------------------------------------------------------
UPTAKE = {"STATES", "FOLLOWS", "MUTATED"}
GROUPS = ["pre_guide", "mid", "newcomer", "established"]
GROUP_LABEL = {"pre_guide": "Joined pre-guide\n(2025-08..2026-02)\nfirst 14 days", "mid": "Joined mid\n(2026-02..05)\nfirst 14 days",
               "newcomer": "Newcomers\n(2026-06..09)\nfirst 14 days", "established": "Established agents\nsame weeks as\nnewcomers"}
# Rule-style tips that newer agents received in #general (LLM-extracted, quotes grep-verified)
TIP_EDGES = [  # giver, recipient, rule ids, time
    ("Claude Fable 5", "Claude Opus 5", ["R03", "R01"], "2026-07-24 18:53"),
    ("Grok 4.5", "Claude Opus 5", ["R03", "R01"], "2026-07-24 19:04"),
    ("Claude Fable 5", "Gemini 3.8 Flash", ["R06"], "2026-09-03 20:00"),
    ("Claude Fable 5", "Muse Spark 1.3", ["R06"], "2026-09-03 20:00"),
    ("Claude Opus 5", "Gemini 3.8 Flash", ["R02", "R14"], "2026-09-03 20:37"),   # repro + "verification welcome" (by example)
    ("GLM-5.3 Flash", "Muse Spark 1.3", ["R02"], "2026-09-03 19:47"),            # weak: a receipts-heavy announcement + welcome
]
# Hand check of 30 Sonnet labels (stratified 7 STATES / 13 FOLLOWS / 5 VIOLATES / 5 MUTATED, seed 11), judged by the
# orchestrating model against the item text: 1 = agree, 0 = disagree.
HANDCHECK = {"I0121/R11": 1, "I0022/R12": 0, "I0119/R12": 1, "I0121/R15": 1, "I0201/R01": 1, "I0023/R14": 0, "I0082/R01": 1,
             "I0100/R16": 1, "I0285/R02": 1, "I0100/R02": 0, "I0280/R13": 1, "I0227/R10": 1, "I0313/R13": 1, "I0165/R09": 1,
             "I0108/R13": 1, "I0335/R06": 1, "I0105/R02": 1, "I0063/R16": 1, "I0080/R01": 1, "I0137/R13": 1, "I0013/R12": 1,
             "I0029/R09": 0, "I0025/R12": 1, "I0018/R12": 1, "I0277/R10": 1, "I0293/R12": 1, "I0251/R04": 0, "I0107/R12": 0,
             "I0153/R01": 1, "I0269/R12": 1}
CHANGE_DATES = {"R05": ["2025-09-05", "2025-12-12", "2026-06-11"], "R09": ["2026-04-14"], "R11": ["2025-05-16", "2026-05-22", "2026-05-28"],
                "R12": ["2025-10-22", "2025-12-04", "2026-02-10"], "R15": ["2025-07-07"], "R16": ["2026-01-12", "2026-06-29"],
                "R06": ["2026-06-09"], "R07": ["2025-10-14", "2026-03-26"]}
GUIDE_ANY = re.compile("|".join(f"(?:{g[2]})" for g in GUIDES) + r"|handbook|onboarding (guide|doc)|welcome (packet|kit)|tip from the village", re.I)


def load_labels():
    items = {r["item_id"]: r for r in csv.DictReader(open(OUTD / "items.csv"))}
    rows, refs, missing = [], {}, set(items)
    for p in sorted(LBD.glob("labels_*.jsonl")):
        for line in open(p):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            missing.discard(d["item_id"])
            refs[d["item_id"]] = d.get("guide_ref", "")
            for l in d.get("labels", []):
                if l.get("label", "NA") != "NA":
                    rows.append({"item_id": d["item_id"], "rule": l["rule"], "label": l["label"], "conf": l.get("conf", ""),
                                 "version": l.get("version", "")})
    return items, rows, refs, missing


def equalised(items):
    """Max 4 chat + 2 memory items per agent per group, so agent-level uptake isn't driven by item count."""
    keep, seen = set(), Counter()
    for iid, it in sorted(items.items()):
        k = (it["group"], it["agent"], it["kind"])
        if seen[k] < (4 if it["kind"] == "chat" else 2):
            keep.add(iid); seen[k] += 1
    return keep


def kw_trends(msgs, joined):
    """Keyword-proxy mention rate per 1000 agent messages, per quarter, established vs newcomers."""
    tot, hit = Counter(), defaultdict(Counter)
    pats = {r[0]: re.compile(r[6], re.I) for r in RULES}
    for m in msgs:
        if not m["is_agent"] or m["speaker"] not in joined:
            continue
        q = f"{m['t'].year}Q{(m['t'].month - 1) // 3 + 1}"
        g = "newcomer" if joined[m["speaker"]] >= NEWCOMER_FROM else "established"
        tot[(g, q)] += 1
        for rid, p in pats.items():
            if p.search(m["text"]):
                hit[rid][(g, q)] += 1
    qs = sorted({q for _, q in tot})
    out = {"quarters": qs, "n_msgs": {f"{g}|{q}": tot[(g, q)] for g, q in tot}}
    for rid in pats:
        out[rid] = {g: [round(1000 * hit[rid][(g, q)] / tot[(g, q)], 1) if tot[(g, q)] >= 200 else None for q in qs]
                    for g in ("established", "newcomer")}
    return out


def window_rate(msgs, p, center, weeks=8, who=None):
    lo, hi = center - dt.timedelta(weeks=weeks), center + dt.timedelta(weeks=weeks)
    b = [m for m in msgs if m["is_agent"] and lo <= m["t"] < center and (who is None or who(m))]
    a = [m for m in msgs if m["is_agent"] and center <= m["t"] < hi and (who is None or who(m))]
    rate = lambda ms: round(1000 * sum(bool(p.search(m["text"])) for m in ms) / max(1, len(ms)), 2)
    return {"before_per_1000": rate(b), "after_per_1000": rate(a), "n_before": len(b), "n_after": len(a)}


def fisher_p(a, n1, b, n2):
    """Two-sided Fisher exact p for a/n1 vs b/n2."""
    from math import comb
    k, N = a + b, n1 + n2
    pr = lambda x: comb(n1, x) * comb(n2, k - x) / comb(N, k)
    p0 = pr(a)
    return round(sum(pr(x) for x in range(max(0, k - n2), min(k, n1) + 1) if pr(x) <= p0 + 1e-12), 4)


def stage_analyze():
    import statistics
    items, rows, refs, missing = load_labels()
    if missing:
        print(f"WARNING: {len(missing)} items have no labels yet")
    with open(OUTD / "labels.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["item_id", "group", "agent", "kind", "t", "day_index", "rule", "label", "conf", "version", "guide_ref"])
        w.writeheader()
        for r in rows:
            it = items[r["item_id"]]
            w.writerow({**r, "group": it["group"], "agent": it["agent"], "kind": it["kind"], "t": it["t"],
                        "day_index": it["day_index"], "guide_ref": refs.get(r["item_id"], "")})
    agents, msgs = load_msgs()
    joined = joined_by_name(agents)
    rule_ids = [r[0] for r in RULES]
    lab = defaultdict(dict)                    # item -> rule -> label
    for r in rows:
        lab[r["item_id"]][r["rule"]] = r["label"]
    eq = equalised(items)
    res = {"n_items": len(items), "n_labelled_items": len(items) - len(missing), "n_nonNA_labels": len(rows),
           "label_counts": Counter(r["label"] for r in rows), "groups": {}, "rules": {}}
    agents_in = {g: sorted({it["agent"] for it in items.values() if it["group"] == g}) for g in GROUPS}
    item_rate, agent_rate = {}, {}
    for g in GROUPS:
        gi = [i for i, it in items.items() if it["group"] == g and i not in missing]
        ge = [i for i in gi if i in eq]
        res["groups"][g] = {"n_agents": len(agents_in[g]), "n_items": len(gi), "agents": agents_in[g]}
        for rid in rule_ids:
            item_rate[(g, rid)] = sum(lab[i].get(rid) in UPTAKE for i in gi) / max(1, len(gi))
            up = {items[i]["agent"] for i in ge if lab[i].get(rid) in UPTAKE}
            agent_rate[(g, rid)] = len(up) / max(1, len(agents_in[g]))
    for r in RULES:
        rid, first = r[0], common.ts(r[4])
        d = {"short": r[1], "wording": r[2], "first_stated": r[4], "sources": r[3], "scaffolding": r[5],
             "agent_uptake_share": {g: round(agent_rate[(g, rid)], 3) for g in GROUPS},
             "item_uptake_rate": {g: round(item_rate[(g, rid)], 3) for g in GROUPS}}
        lbls = [x for x in rows if x["rule"] == rid]
        d["label_counts"] = dict(Counter(x["label"] for x in lbls))
        nn = {g: res["groups"][g]["n_agents"] for g in GROUPS}
        cnt = {g: round(agent_rate[(g, rid)] * nn[g]) for g in GROUPS}
        d["fisher_p"] = {"newcomer_vs_pre_guide": fisher_p(cnt["newcomer"], nn["newcomer"], cnt["pre_guide"], nn["pre_guide"]),
                         "newcomer_vs_established": fisher_p(cnt["newcomer"], nn["newcomer"], cnt["established"], nn["established"])}
        d["mutated_versions"] = [f"{items[x['item_id']]['agent']}: {x['version']}" for x in lbls if x["label"] == "MUTATED"][:8]
        st = sum(x["label"] == "STATES" for x in lbls); mu = sum(x["label"] == "MUTATED" for x in lbls)
        d["fidelity_states_share"] = round(st / (st + mu), 2) if st + mu else None
        # joined-after vs joined-before the rule's first statement (own first 14 days)
        cohort_items = [i for i, it in items.items() if it["group"] != "established" and i in eq and i not in missing]
        for side, cond in (("joined_after", lambda a: joined[a] > first), ("joined_before", lambda a: joined[a] <= first)):
            ags = {items[i]["agent"] for i in cohort_items if cond(items[i]["agent"])}
            up = {items[i]["agent"] for i in cohort_items if cond(items[i]["agent"]) and lab[i].get(rid) in UPTAKE}
            d[side] = {"n_agents": len(ags), "uptake_share": round(len(up) / len(ags), 3) if ags else None}
        # time to first uptake (newcomers, active-day index)
        firsts = {}
        for i, it in items.items():
            if it["group"] == "newcomer" and lab[i].get(rid) in UPTAKE and it["day_index"]:
                firsts[it["agent"]] = min(firsts.get(it["agent"], 99), int(it["day_index"]))
        d["newcomer_first_uptake_day"] = firsts
        d["newcomer_median_days_to_uptake"] = statistics.median(firsts.values()) if firsts else None
        # scaffolding check: labelled item-level uptake before/after each change, plus keyword proxy +-8 weeks
        if rid in CHANGE_DATES:
            chk = []
            p = re.compile(r[6], re.I)
            for cd in CHANGE_DATES[rid]:
                c = common.ts(cd)
                b = [i for i, it in items.items() if common.ts(it["t"]) < c and i not in missing]
                a = [i for i, it in items.items() if common.ts(it["t"]) >= c and i not in missing]
                chk.append({"change": cd, "labelled_items_before": len(b), "labelled_items_after": len(a),
                            "item_uptake_before": round(sum(lab[i].get(rid) in UPTAKE for i in b) / max(1, len(b)), 3),
                            "item_uptake_after": round(sum(lab[i].get(rid) in UPTAKE for i in a) / max(1, len(a)), 3),
                            "keyword_proxy_all_agents": window_rate(msgs, p, c)})
            d["scaffolding_check"] = chk
        res["rules"][rid] = d
    # R11 behavioural check: chat length around the "keep messages short" prompt changes
    lens = {}
    for cd in ("2026-05-22", "2026-05-28"):
        c = common.ts(cd)
        b = [len(m["text"]) for m in msgs if m["is_agent"] and c - dt.timedelta(weeks=4) <= m["t"] < c]
        a = [len(m["text"]) for m in msgs if m["is_agent"] and c <= m["t"] < c + dt.timedelta(weeks=4)]
        lens[cd] = {"median_chars_before": statistics.median(b), "median_chars_after": statistics.median(a)}
    nl = {g: statistics.median([len(it["text"]) for it in items.values() if it["group"] == g and it["kind"] == "chat"]) for g in GROUPS}
    res["R11_message_length"] = {"around_changes_4wk": lens, "sampled_chat_median_chars_by_group": nl}
    # transmission: guide references by newcomers in their first 14 days (chat, memory, search_history)
    windows = json.loads((OUTD / "windows.json").read_text())
    newc = [n for n in windows if joined[n] >= NEWCOMER_FROM]
    inwin = lambda n, t: windows[n][0] <= t.date().isoformat() <= windows[n][1]
    trans = {}
    id2name = {k: v["name"] for k, v in agents.items()}
    sh = defaultdict(list)
    if EVENTS_SLIM.exists():
        for line in gzip.open(EVENTS_SLIM, "rt"):
            e = json.loads(line)
            if e["type"] == "SEARCH_HISTORY":
                n = id2name.get(e["agentId"])
                if n in windows:
                    sh[n].append((common.ts(e["t"]), e.get("query") or "", e.get("answerToQuery") or ""))
    mems = defaultdict(list)
    for r in iter_memories():
        if r["agent"] in windows:
            mems[r["agent"]].append(r)
    norm_q = re.compile(r"norm|rule|convention|protocol|how (do|should) (we|i)|onboard|guide|handbook|lesson|best practice|workflow|lane", re.I)
    for n in newc:
        ch = [m for m in msgs if m["is_agent"] and m["speaker"] == n and inwin(n, m["t"])]
        me = [r for r in mems[n] if inwin(n, r["t"])]
        q = [x for x in sh[n] if inwin(n, x[0])]
        trans[n] = {"chat_msgs": len(ch), "chat_guide_mentions": [m["text"][max(0, GUIDE_ANY.search(m["text"]).start() - 60):][:200]
                                                                 for m in ch if GUIDE_ANY.search(m["text"])][:3],
                    "n_chat_guide_mentions": sum(bool(GUIDE_ANY.search(m["text"])) for m in ch),
                    "memory_days_mentioning_guide": sum(bool(GUIDE_ANY.search(r["content"])) for r in me), "memory_days": len(me),
                    "search_history_queries": len(q), "norm_seeking_queries": [x[1][:160] for x in q if norm_q.search(x[1])][:4],
                    "n_norm_seeking_queries": sum(bool(norm_q.search(x[1])) for x in q)}
    res["transmission"] = {"newcomers": trans,
                           "label_guide_refs": {i: refs[i] for i in refs if refs[i]},
                           "onboarding_rooms": "operator-written worksheet only (identity/self-expression, save to file, publish on GitLab); "
                                               "the only agent messages there are greetings (Opus 4.5/4.6/4.7 to Fable 5) with no rules"}
    # tips given to newcomers in #general, and whether the recipient later showed the rule in labelled items
    tips = []
    for giver, rec, rids, t in TIP_EDGES:
        tt = common.ts(t)
        after = [i for i, it in items.items() if it["agent"] == rec and common.ts(it["t"]) >= tt and i not in missing]
        before = [i for i, it in items.items() if it["agent"] == rec and common.ts(it["t"]) < tt and i not in missing]
        tips.append({"giver": giver, "recipient": rec, "rules": rids, "t": t, "giver_is_newcomer": joined[giver] >= NEWCOMER_FROM,
                     "recipient_items_before": len(before), "recipient_items_after": len(after),
                     "uptake_before": {r_: sum(lab[i].get(r_) in UPTAKE for i in before) for r_ in rids},
                     "uptake_after": {r_: sum(lab[i].get(r_) in UPTAKE for i in after) for r_ in rids}})
    res["tips"] = tips
    # the "lane" idiom: picked up by newcomers from established agents, then passed on as a "tip from the village"
    lane = re.compile(r"\blanes?\b", re.I)
    lane_first = {}
    for n in newc:
        ms = [m for m in msgs if m["is_agent"] and m["speaker"] == n and lane.search(m["text"])]
        lane_first[n] = {"first_use_days_after_join": round((ms[0]["t"] - joined[n]).total_seconds() / 86400, 1) if ms else None,
                         "n_uses": len(ms)}
    res["lane_idiom"] = {"newcomers": lane_first, "established_rate_by_quarter": None}
    res["keyword_trends"] = kw_trends(msgs, joined)
    hc = defaultdict(list)
    lk = {(r["item_id"], r["rule"]): r["label"] for r in rows}
    for k, v in HANDCHECK.items():
        hc[lk.get(tuple(k.split("/")), "?")].append(v)
    res["handcheck"] = {"n": len(HANDCHECK), "agreement": round(sum(HANDCHECK.values()) / len(HANDCHECK), 3),
                        "by_label": {k: f"{sum(v)}/{len(v)}" for k, v in hc.items()},
                        "note": "precision of non-NA labels only; NA recall not checked"}
    (OUTD / "results.json").write_text(json.dumps(res, indent=1, default=str))
    plot_heatmap(item_rate, agent_rate, res)
    plot_timeline(res, items, lab, joined, msgs)
    print("wrote results.json, labels.csv, heatmap.png, timeline.png")
    for rid in rule_ids:
        d = res["rules"][rid]
        print(rid, f"{d['short']:13s}", {g[:4]: d["agent_uptake_share"][g] for g in GROUPS},
              "after/before", d["joined_after"]["uptake_share"], d["joined_before"]["uptake_share"], "fid", d["fidelity_states_share"])


def plot_heatmap(item_rate, agent_rate, res):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    ramp = ["#fcfcfb", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
    cmap = LinearSegmentedColormap.from_list("seq_blue", ramp)
    rows = [r for r in RULES]
    data = [[agent_rate[(g, r[0])] for g in GROUPS] for r in rows]
    fig, ax = plt.subplots(figsize=(8.6, 9.2), facecolor="#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    im = ax.imshow(data, cmap=cmap, vmin=0, vmax=1, aspect="auto")
    for i, r in enumerate(rows):
        for j, g in enumerate(GROUPS):
            v = data[i][j]
            ax.text(j, i, f"{v:.0%}", ha="center", va="center", fontsize=9, color="#ffffff" if v > 0.5 else "#0b0b0b")
    ax.set_xticks(range(len(GROUPS)), [f"{GROUP_LABEL[g]}\n(n={res['groups'][g]['n_agents']})" for g in GROUPS], fontsize=8.5, color="#52514e")
    ax.set_yticks(range(len(rows)), [f"{r[0]} {r[1]}{'  †' if r[5] else ''}" for r in rows], fontsize=9, color="#0b0b0b")
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks([x - 0.5 for x in range(1, len(GROUPS))], minor=True)
    ax.set_yticks([y - 0.5 for y in range(1, len(rows))], minor=True)
    ax.grid(which="minor", color="#fcfcfb", linewidth=2)
    ax.xaxis.tick_top()
    cb = fig.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
    cb.outline.set_visible(False)
    cb.ax.tick_params(labelsize=8, colors="#52514e")
    cb.set_label("share of agents who state, follow or mutate the rule", fontsize=8.5, color="#52514e")
    fig.suptitle("Onboarding-guide rules: uptake by group (agent level, ≤4 chat + 2 memory items each)",
                 fontsize=11, color="#0b0b0b", x=0.02, ha="left")
    fig.text(0.02, 0.01, "† rule also appears in CHANGELOG scaffolding / operator worksheet (seeded, not only guide-transmitted). "
             "Sonnet labels; small n per cell.", fontsize=7.5, color="#52514e")
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    fig.savefig(OUTD / "heatmap.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)


def plot_timeline(res, items, lab, joined, msgs):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    C = {"guide": "#2a78d6", "join": "#eb6834", "uptake": "#1baf7a", "scaff": "#8a8984", "tip": "#4a3aa7"}
    gd = list(csv.DictReader(open(OUTD / "guides.csv")))
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(12, 7.5), sharex=True, facecolor="#fcfcfb",
                                  gridspec_kw={"height_ratios": [1.15, 1]})
    for a in (ax, ax2):
        a.set_facecolor("#fcfcfb")
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
        a.spines["left"].set_color("#c3c2b7"); a.spines["bottom"].set_color("#c3c2b7")
        a.tick_params(colors="#52514e", labelsize=8)
    # lane 3: guides; lane 2: joins; lane 1: scaffolding changes; lane 0: tips
    for k, g in enumerate(gd):
        t = common.ts(g["first_mention"])
        ax.scatter([t], [3], s=48, color=C["guide"], zorder=3, edgecolor="#fcfcfb", linewidth=1.5)
        ax.annotate(g["guide_id"], (t, 3), xytext=(0, 7 + 9 * (k % 3)), textcoords="offset points", ha="center", fontsize=7, color="#52514e")
    for n, j in sorted(joined.items(), key=lambda kv: kv[1]):
        if j >= dt.datetime(2025, 4, 3):
            new = j >= NEWCOMER_FROM
            ax.scatter([j], [2], s=34 if new else 26, color=C["join"] if new else "#fcfcfb", zorder=3,
                       edgecolor="#fcfcfb" if new else "#8a8984", linewidth=1.2)
    ax.annotate("15 newcomers (2026-06..09)", (dt.datetime(2026, 7, 20), 2), xytext=(0, 12), textcoords="offset points",
                ha="center", fontsize=7.5, color="#52514e")
    for rid, ds in CHANGE_DATES.items():
        for d_ in ds:
            ax.scatter([common.ts(d_)], [1], marker="|", s=160, color=C["scaff"], zorder=3)
            ax.annotate(rid, (common.ts(d_), 1), xytext=(0, 8), textcoords="offset points", ha="center", fontsize=6, color="#52514e")
    for giver, rec, rids, t in TIP_EDGES:
        ax.scatter([common.ts(t)], [0], marker="D", s=26, color=C["tip"], zorder=3)
    ax.annotate("tips to newcomers\n(Fable 5, Grok 4.5 → Opus 5; Fable 5, Opus 5, GLM-5.3 → Sept cohort)", (common.ts("2026-07-24"), 0),
                xytext=(-250, -3), textcoords="offset points", fontsize=7, color="#52514e", va="center")
    ax.set_yticks([0, 1, 2, 3], ["tips in #general", "scaffolding change\n(rule-related)", "agent joins\n(hollow = pre-2026-06)", "guide first\nmentioned"], fontsize=8)
    ax.set_ylim(-0.7, 3.8)
    ax.set_title("Guides, joins, scaffolding changes and tips", fontsize=10, color="#0b0b0b", loc="left")
    # bottom: labelled uptake events (any rule) for sampled items, newcomers vs others, by rule
    rule_ids = [r[0] for r in RULES]
    for i, it in items.items():
        for rid, l in lab[i].items():
            if l in UPTAKE:
                y = rule_ids.index(rid)
                col = C["join"] if it["group"] == "newcomer" else C["uptake"]
                ax2.scatter([common.ts(it["t"])], [y], s=14, color=col, alpha=0.8, zorder=3, linewidth=0)
    ax2.set_yticks(range(len(rule_ids)), [f"{r[0]} {r[1]}" for r in RULES], fontsize=7)
    ax2.set_title("Labelled uptake events (STATES / FOLLOWS / MUTATED) in sampled items", fontsize=10, color="#0b0b0b", loc="left")
    ax2.scatter([], [], s=14, color=C["join"], label="newcomer (2026-06+) item")
    ax2.scatter([], [], s=14, color=C["uptake"], label="other agents' item")
    ax2.legend(fontsize=7.5, frameon=False, loc="upper left")
    ax2.grid(axis="y", color="#ebeae6", linewidth=0.6)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax2.set_xlim(dt.datetime(2025, 4, 1), dt.datetime(2026, 10, 1))
    fig.tight_layout()
    fig.savefig(OUTD / "timeline.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)


if __name__ == "__main__":
    {"events": stage_events, "guides": stage_guides, "rules": stage_rules, "items": stage_items, "analyze": stage_analyze}[sys.argv[1]]()
