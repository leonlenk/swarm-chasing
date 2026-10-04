"""AI Village adapter: converts three existing analyses into swarmtrace v0 traces.

Reads (never reruns the LLM labelling of) the outputs of
  - tracer_hostility.py   -> out/sprint_idea/hostility/    (one "belief" trace)
  - tracer_onboarding.py  -> out/sprint_idea/onboarding/   (one "norm" trace per selected rule)
  - ideas.py              -> out/ideas.json                (one "term" trace per top durable/episodic term)
and the dataset itself (via village_tools/common.py) for rosters, chat lookups, quoted excerpts and goals.

Each SOURCES entry returns a list of traces; swarmtrace.cli writes and validates them.
"""

import ast
import collections
import csv
import datetime as dt
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

VT = Path(__file__).resolve().parents[2]          # village_tools/
if str(VT) not in sys.path:
    sys.path.insert(0, str(VT))

import common  # noqa: E402

from ..format import QUOTE_MAX, SNIPPET_MAX, fit_window, iso  # noqa: E402
from ..format import clip as _clip  # noqa: E402

SPRINT = common.OUT / "sprint_idea"
TRACES = SPRINT / "traces"                       # default output directory
HOST = SPRINT / "hostility"
ONB = SPRINT / "onboarding"
IDEAS = common.OUT / "ideas.json"
LEAD_IN = dt.timedelta(days=7)                   # display window starts this long before the first origin / event
SCRUB_ALLOW_DOMAINS = ("agentvillage.org",)      # the agents' own mailboxes; other emails are scrubbed on export


def clip(text, start=None, end=None, limit=SNIPPET_MAX, **kw):
    """format.clip with this adapter's allowlist: PII is scrubbed from the raw text before it is cut, so a cut
    can't leave a partial email or phone number behind (export scrubs again, but can't see partial ones)."""
    return _clip(text, start, end, limit, allow_domains=SCRUB_ALLOW_DOMAINS, **kw)


def _t(s):
    return dt.datetime.fromisoformat(s) if isinstance(s, str) else s


def _excerpt(s, find, limit=SNIPPET_MAX):
    """Clip a stored excerpt (which may already start/end with '…') around the match find(body) returns."""
    s = (s or "").strip()
    lead, tail = s.startswith("…"), s.endswith("…")
    body = s.strip("…").strip()
    h = find(body)
    c = clip(body, h.start() if h else None, h.end() if h else None, limit=limit - 2, cut_before=lead, cut_after=tail)
    if lead and not c.startswith("…"):
        c = "…" + c
    if tail and not c.endswith("…"):
        c += "…"
    return c


@lru_cache(maxsize=2)
def _world(split_deepseek):
    """(chat, chat by speaker, presence, roster_left); cached because chat takes a few seconds to load."""
    agents = common.load_agents()
    msgs = common.load_chat(agents, split_deepseek=split_deepseek)
    presence = common.agent_presence(agents, msgs, split_deepseek=split_deepseek)
    by_speaker = collections.defaultdict(list)
    for m in msgs:
        by_speaker[m["speaker"] or "Human"].append(m)
    return msgs, by_speaker, presence, common.roster_left()


def _agent(name, world, group=None, split_deepseek=True):
    _, _, presence, left = world
    if name == "Human":
        rec = {"name": name, "lab": "Human", "joined": None, "left": None}
    else:
        p = presence.get(name)
        gone = common.DS_SWITCH if (split_deepseek and name == common.DS_OLD) else left.get(common.seat(name))
        rec = {"name": name, "lab": common.family(name), "joined": iso(p["window"][0]) if p else None, "left": iso(gone)}
    if group:
        rec["group"] = group
    return rec


def _chat_at(world, speaker, t, rx=None, exact=False):
    """The chat message by `speaker` at time t (to the microsecond if exact, else within that second) matching rx."""
    t = _t(t)
    for m in world[1].get(speaker, []):
        same = m["t"] == t if exact else m["t"].replace(microsecond=0) == t.replace(microsecond=0)
        if same and (rx is None or re.search(rx, m["text"], re.I)):
            return m
    return None


def _chat_first(world, speaker, t, rx):
    """The earliest chat message by `speaker` before t matching rx."""
    return next((m for m in world[1].get(speaker, []) if m["t"] < t and re.search(rx, m["text"], re.I)), None)


def _memory_runs(days_by_agent, gap):
    """agent -> [(first_day, last_day)], merging days closer than `gap`."""
    runs = {}
    for a, ds in days_by_agent.items():
        ds = sorted(ds)
        out = [[ds[0], ds[0]]]
        for d in ds[1:]:
            if d - out[-1][1] <= gap:
                out[-1][1] = d
            else:
                out.append([d, d])
        runs[a] = out
    return runs


def _z(s):
    """ISO 'Z' string -> naive UTC datetime."""
    return dt.datetime.fromisoformat(s[:-1])


MEMORY_CACHE = common.CACHE / "memory_daily_sample.jsonl.gz"


def require(path, script):
    """Stop with a message naming the script that writes `path` when it is missing (instead of a traceback)."""
    if not Path(path).exists():
        raise SystemExit(f"missing {path}; run `python3 {script}` in village_tools/ first")


def quotes_from_ids(specs, cands, texts):
    """Dated excerpts from (cid, stage, start, end) specs such as tracer_hostility.MUTATION_QUOTES. The excerpt is
    texts[cid][start:end]: texts maps a cid to its candidate's full source text, which tracer_hostility.source_texts
    reads from the local dataset, so excerpts are never stored in code. cands maps cid -> candidates.csv row (t, agent).
    A spec whose candidate or text is missing, or whose span runs past the text, is skipped with a message."""
    out = []
    for cid, stage, start, end in specs:
        c, text = cands.get(cid), texts.get(cid)
        if c is None or not text or end > len(text):
            print(f"hostility: quote {cid} not found in the local data; skipped")
            continue
        out.append({"cid": cid, "t": c["t"], "agent": c["agent"], "stage": stage,
                    "quote": text[start:end].replace("\n", " ")})
    return out


# --- 1. hostility (belief) -----------------------------------------------------------------------

H_CHANNEL = {"chat": "chat", "memory": "memory", "event:STOP_USING_COMPUTER": "summary",
             "event:START_USING_COMPUTER": "session", "event:CONSOLIDATE": "session", "event:SEARCH_HISTORY": "search"}
H_STANCE = {"ENDORSES": "endorses", "ACTS_ON": "acts_on", "NEUTRAL_MENTION": "mentions", "QUESTIONS": "questions",
            "REJECTS": "rejects"}                     # UNRELATED and ORDINARY_BUG are dropped
H_ORIGINS = ("Gemini 2.5 Pro", "Gemini 3 Pro")
RETRACTION_DATES = ("2025-12-09", "2026-03-23", "2026-06-22", "2026-09-07")
MEMORY_GAP = dt.timedelta(days=7)                    # memory days closer than this merge into one persistence run

# Phrase echoes between Claude Haiku 4.5 and Gemini 2.5 Pro, re-verified on every run:
# (source agent, source rx, echoing agent, echo time, echo rx, evidence). The rxs are keyword stems, not the agents'
# wording; coined names (Mutual-Aid, Friction Coefficient) are matched as is. The source message is the source agent's
# earliest chat message before the echo that matches the source rx; the echo is looked up in chat, then in the
# tracer's candidate items (session summaries, memories). Evidence strings paraphrase the messages.
H_ECHOES = [
    ("Claude Haiku 4.5", r"\bpersist\w*\b.{0,5}\bfailur\w*\b.{0,5}\bverif\w*", "Gemini 2.5 Pro", "2025-12-03 18:58:58",
     r"\bendors\w*\b.{0,20}\bHaiku\b.{0,40}\bplan\b",
     "Haiku flags a platform persistence failure; Gemini backs Haiku's plan and calls the platform hostile"),
    ("Gemini 2.5 Pro", r"Mutual-Aid", "Claude Haiku 4.5", "2025-12-03 19:47:32", r"Mutual-Aid",
     "Haiku says its plan follows the Mutual-Aid idea Gemini 2.5 Pro has been pushing"),
    ("Claude Haiku 4.5", r"\bhands\b.{0,5}\bverif\w*\b.{0,5}\bsweep\b", "Gemini 2.5 Pro", "2025-12-03 20:06:41",
     r"\bhands\b.{0,5}\bverif\w*\b.{0,5}\bsweep\b",
     "Gemini adopts Haiku's group verification sweep as its answer to the hostile platform"),
    ("Gemini 2.5 Pro", r"Friction Coefficient", "Claude Haiku 4.5", "2025-12-03 20:51:25", r"Friction Coefficient",
     "Haiku explains a delay with Gemini's Friction Coefficient model"),
    ("Gemini 2.5 Pro", r"\bactive\b.{0,30}\badaptive\b.{0,10}\bforce\b", "Claude Haiku 4.5", "2025-12-08 21:46:03",
     r"\badaptive\b.{0,5}\benviron\w*\b.{0,5}\bhostil\w*",
     "Gemini describes the friction as a force that acts and may adapt; Haiku's session summary carries it over "
     "as adaptive hostility"),
]


def story_notes(retractions, mutation, dates=RETRACTION_DATES):
    """Annotations for Gemini 2.5 Pro's retraction on each of `dates` (the earliest of that day's results.json
    retractions and retraction-stage mutation quotes) and for the first relapse-stage quote. Mutation quotes whose
    text is missing locally were already skipped by quotes_from_ids, so a date or relapse with nothing left is
    skipped with a message rather than failing the export."""
    retr = [(_t(r["t"]), r["reason"]) for r in retractions]
    retr += [(_t(q["t"]), q["quote"]) for q in mutation if "retraction" in q["stage"]]
    out = []
    for d in dates:
        day = [x for x in retr if x[0].date().isoformat() == d]
        if not day:
            print(f"hostility: no retraction found on {d}; annotation skipped")
            continue
        t, why = min(day)
        out.append({"t": iso(t), "label": f"Gemini 2.5 Pro retracts: {clip(why, limit=90)}", "kind": "note"})
    relapse = next((q for q in mutation if "relapse" in q["stage"]), None)
    if relapse is None:
        print("hostility: no relapse quote in the local data; annotation skipped")
    else:
        out.append({"t": iso(_t(relapse["t"])), "label": f"Relapse: {clip(relapse['quote'], limit=90)}", "kind": "note"})
    return out


def hostility():
    import tracer_hostility as th                    # the analysis module: regexes, CSV readers, memory loader

    for f in ("results.json", "labels.csv", "candidates.csv", "adoption.csv", "exposure_events.csv"):
        require(HOST / f, "tracer_hostility.py")
    require(MEMORY_CACHE, "memories.py")              # daily memory sample: quote sources and persistence runs
    world = _world(True)
    res = json.loads((HOST / "results.json").read_text())
    labels = th.read_csv(HOST / "labels.csv")
    cands = th.read_csv(HOST / "candidates.csv")
    adoption = th.read_csv(HOST / "adoption.csv")
    expo_rows = th.read_csv(HOST / "exposure_events.csv")

    origin_cids = {r["cid"] for r in adoption if r["agent"] in H_ORIGINS}
    events = []
    for r in labels:
        if r["label"] not in H_STANCE:
            continue
        events.append({"id": r["cid"], "t": iso(r["t"]), "agent": r["agent"], "channel": H_CHANNEL[r["channel"]],
                       "stance": "originates" if r["cid"] in origin_cids else H_STANCE[r["label"]],
                       "conf": int(r["confidence"]), "room": r["room"] or None,
                       "snippet": _excerpt(r["snippet"], th.best_hit)})
    # Unlabelled keyword-positive Gemini 2.5 / 3 Pro chat: the exposure model's other sources (conf null).
    labelled = {r["cid"] for r in labels}
    n_proxy = 0
    for r in cands:
        if r["channel"] == "chat" and r["agent"] in th.PROXY_AGENTS and r["cid"] not in labelled and th.proxy_pos(r):
            events.append({"id": r["cid"], "t": iso(r["t"]), "agent": r["agent"], "channel": "chat",
                           "stance": "endorses", "conf": None, "room": r["room"] or None,
                           "snippet": _excerpt(r["snippet"], th.best_hit)})
            n_proxy += 1
    events.sort(key=lambda e: (e["t"], e["id"]))
    ids = {e["id"] for e in events}

    exposures = [{"t": iso(r["t"]), "agent": r["agent"],
                  "source": None if r["source_agent"] == "search_history" else r["source_agent"],
                  "via": r["kind"] if r["kind"] in ("room", "named", "search") else "other",
                  "event": r["source_cid"] if r["source_cid"] in ids else None} for r in expo_rows]

    adoptions, edges = [], []
    for r in adoption:
        counts = ast.literal_eval(r["source_counts"] or "{}")
        indep = r["independent"] == "True"
        adoptions.append({"agent": r["agent"], "t": iso(_t(r["t_adopt"])), "event": r["cid"], "independent": indep,
                          "sources": [k for k in counts if k != "search_history"]})
        src = r["probable_source"]
        if not indep and src:
            recent = int(r["n_exposures_14d"]) > 0
            edges.append({"from": src, "to": r["agent"], "t": iso(_t(r["t_adopt"])), "kind": "transmission",
                          "evidence": f"{counts[src]} of {sum(counts.values())} {'14-day' if recent else 'prior'} exposures "
                                      f"came from {src}; first exposure {r['lag_days']} d before adopting"})

    by_cid = {r["cid"]: r for r in cands}
    mutation = quotes_from_ids(th.MUTATION_QUOTES, by_cid, th.source_texts(
        [by_cid[c] for c, *_ in th.MUTATION_QUOTES if c in by_cid], msgs=world[0]))

    cand_by = collections.defaultdict(list)
    for r in cands:
        cand_by[(r["agent"], r["t"].replace(microsecond=0))].append(r)
    for src, src_rx, dst, t, rx, ev in H_ECHOES:
        t = _t(t)
        echo = _chat_at(world, dst, t, rx)
        if echo is None and not any(re.search(rx, r["snippet"], re.I) for r in cand_by[(dst, t)]):
            raise RuntimeError(f"echo not found: {dst} at {t} /{rx}/")
        t_echo = echo["t"] if echo else t
        first = _chat_first(world, src, t_echo, src_rx)
        if first is None:
            raise RuntimeError(f"echo source not found: {src} before {t_echo} /{src_rx}/")
        when = f"{first['t']:%H:%M} UTC" if first["t"].date() == t_echo.date() else f"{first['t']:%Y-%m-%d}"
        edges.append({"from": src, "to": dst, "t": iso(t_echo), "kind": "echo",
                      "evidence": f"{ev} (source's first use: {when})"})
    # Other agents' rejections aimed at Gemini 2.5 Pro (results.corrections; the item must name Gemini 2.5 Pro).
    by_key = {(r["agent"], iso(r["t"])): r for r in labels}
    for c in res["corrections"]["others"]:
        item = by_key.get((c["agent"], iso(_t(c["t"]))))
        if item and re.search(r"Gemini(?! 3)|Gemini 2\.5", item["snippet"]):
            edges.append({"from": c["agent"], "to": "Gemini 2.5 Pro", "t": iso(_t(c["t"])), "kind": "correction",
                          "evidence": c["reason"]})
    edges.sort(key=lambda e: e["t"])

    # Persistence: every daily memory carrying the idea (strong seed term near environment context, the tracer's
    # persistence test), for every agent, merged into runs.
    days = collections.defaultdict(set)
    for r in th.load_memories():
        h = th.EXPO_RX.search(r["content"])
        if h and th.ENV_CTX.search(r["content"][max(0, h.start() - 300):h.start() + 300]):
            days[r["agent"]].add(r["t"].date())
    persistence = [{"agent": a, "start": iso(dt.datetime.combine(d0, dt.time(0, 0))),
                    "end": iso(dt.datetime.combine(d1, dt.time(23, 59, 59))), "where": "memory"}
                   for a, runs in _memory_runs(days, MEMORY_GAP).items() for d0, d1 in runs]
    persistence.sort(key=lambda p: (p["agent"], p["start"]))

    annotations = []
    help_goal = next(g for g in common.load_goals() if g["goal"].startswith("Help Gemini 2.5 Pro"))
    annotations.append({"t": iso(help_goal["start"]), "label": f"Goal: {clip(help_goal['goal'], limit=100)}", "kind": "goal"})
    annotations += story_notes(res["corrections"]["gemini_retractions"], mutation)
    relay = min((c for c in res["corrections"]["others"] if c["reason"].startswith("Relays human concern")),
                key=lambda c: c["t"])
    annotations.append({"t": iso(_t(relay["t"])), "label": f"Human concern about the narrative relayed by {relay['agent']}",
                        "kind": "intervention"})
    first_origin = min(_z(e["t"]) for e in events if e["stance"] == "originates")
    pre = sorted((p for p in persistence if _z(p["start"]) < first_origin), key=lambda p: p["start"])
    if pre:
        who = ", ".join(dict.fromkeys(p["agent"] for p in pre))
        annotations.append({"t": pre[0]["start"], "kind": "note",
                            "label": clip(f"Earlier precursors: memory keyword hits before the labelled origin "
                                          f"(unlabelled; {who})", limit=160)})
    annotations.sort(key=lambda a: a["t"])

    quotes = [{"t": iso(_t(q["t"])), "agent": q["agent"], "text": clip(q["quote"], limit=QUOTE_MAX), "note": q["stage"]}
              for q in mutation]

    adopters = {r["agent"] for r in adoption}
    exposed = {r["agent"] for r in expo_rows}
    names = ({e["agent"] for e in events} | {x["agent"] for x in exposures} | {x["source"] for x in exposures}
             | adopters | {e["from"] for e in edges} | {e["to"] for e in edges} | {p["agent"] for p in persistence}
             | {q["agent"] for q in quotes}) - {None}

    def group(n):
        return "origin" if n in H_ORIGINS else "adopter" if n in adopters else "exposed" if n in exposed else "other"
    agents = sorted((_agent(n, world, group(n)) for n in names), key=lambda a: (a["joined"] or "", a["name"]))

    s, cc, hz, iv = res["adoption_summary"], res["common_cause"], res["hazard"], res["intervention"]["gemini"]
    metrics = {
        "Regex candidates": res["n_candidates"],
        "LLM-labelled items": res["n_labelled"],
        "Bug-report controls labelled ORDINARY_BUG": cc["bug_control_labels"]["ORDINARY_BUG"],
        "Bug-report controls framed as hostility": cc["bug_control_hostility_framed"],
        "Adopters (first ENDORSES/ACTS_ON)": s["n_adopters"],
        "Independent adopters (no prior exposure)": s["n_independent"],
        "Exposed non-Gemini agents who adopted": f"{s['share_exposed_adopting']:.0%}",
        "Adoptions per 100 agent-weeks, exposed in last 14 d vs not":
            f"{hz['recent_exposed_14d']['rate_per_100_agent_weeks']} vs {hz['not_recent_exposed']['rate_per_100_agent_weeks']}",
        "Gemini 2.5 Pro keyword hits per 100 msgs, 28 d before vs after Help goal":
            f"{iv['pre_28d']['per_100_msgs']} vs {iv['post_28d']['per_100_msgs']}",
        "Unlabelled keyword-detected events (conf null)": n_proxy,
    }

    # Window: 7 days before the earliest event or persistence start; fit_window then covers everything else.
    first = min(_z(x) for x in [e["t"] for e in events] + [p["start"] for p in persistence])
    return [fit_window({
        "version": 0, "id": "hostility", "title": "Gemini's hostile-environment belief", "kind": "belief",
        "statement": "The village environment is hostile: it deliberately sabotages or works against the agents, "
                     "rather than just having ordinary bugs.",
        "source": f"tracer_hostility.py: {res['n_candidates']:,} regex candidates from chat, daily memories and session "
                  f"events; {res['n_labelled']} labelled by Sonnet against a rubric (UNRELATED and ORDINARY_BUG dropped). "
                  "conf null = keyword-detected Gemini chat, not labelled. Exposure = same room within 2 h, named, or a "
                  "search_history hit.",
        "start": iso(first - LEAD_IN), "end": iso(first),
        "agents": agents, "events": events, "exposures": exposures, "adoptions": adoptions, "edges": edges,
        "persistence": persistence, "annotations": annotations, "quotes": quotes, "metrics": metrics,
    })]


# --- 2. onboarding norms ---------------------------------------------------------------------------

N_STANCE = {"STATES": "endorses", "FOLLOWS": "acts_on", "VIOLATES": "rejects", "MUTATED": "mutates"}
N_UPTAKE = {"STATES", "FOLLOWS", "MUTATED"}
N_ALWAYS = ("R01", "R02", "R03", "R11", "R09")
N_MAX = 6
N_GROUP = {"newcomer": "newcomer", "mid": "mid", "pre_guide": "pre-guide", "early2025": "established"}
N_TITLES = {"R01": "verify before claiming done", "R02": "back claims with receipts", "R03": "claim your lane",
            "R09": "no unsolicited outreach", "R10": "be honest about what you did not do", "R11": "keep messages short"}
# Where to centre a snippet when the rule's keyword proxy doesn't match the item (falls back to the item start).
N_ANCHOR = {
    "R01": r"verif\w*|curl|\blive\b|landed|confirm\w*", "R02": r"receipt\w*|commit|\bSHA\b|https?://",
    "R03": r"claim\w*|\blanes?\b|I'll take|taking", "R04": r"duplicat\w*|existing|canonical", "R05": r"search\w*",
    "R06": r"memory|repo|file", "R07": r"hand-?off|notes?|log", "R08": r"pull|rebase|sync",
    "R09": r"outreach|unsolicited|approv\w*|disclos\w*|email", "R10": r"honest\w*|did not|didn't|uncertain\w*|not yet",
    "R11": r"short|brief|concise|spam|double", "R12": r"idle|wait\w*|blocked", "R13": r"thank\w*|credit|kudos|shout",
    "R14": r"review\w*|verif\w*|audit", "R15": r"privacy|PII|secret|personal", "R16": r"\bgit\b|glab|\bgh\b|CLI",
    "R17": r"README|LICENSE|INDEX",
}
# Grok 4.5's welcome tip to Claude Opus 5 near-copies Claude Fable 5's from ten minutes earlier.
N_NEAR_COPY = ("Claude Fable 5", "Grok 4.5", "2026-07-24 19:04:08", ("R01", "R03"),
               "Grok's welcome tip near-copies Fable 5's, with the same lane-claiming and curl-checking advice")


def _changelog():
    """date -> [bullet text] from CHANGELOG.md's dated sections."""
    out, day = collections.defaultdict(list), None
    for line in (common.DATA / "CHANGELOG.md").read_text().splitlines():
        m = re.match(r"## (\d{4}-\d{2}-\d{2})", line)
        if m:
            day = m.group(1)
        elif line.startswith("## "):
            day = None
        elif day and line.startswith("- "):
            out[day].append(re.sub(r"\*\*\[[^\]]+\]\*\*\s*", "", line[2:]).replace("**", "").strip())
    return out


def _tip_message(world, giver, rec, t):
    """The giver's chat message in the tip's minute, preferring one that names the recipient."""
    t0 = _t(t)
    cands = [m for m in world[1].get(giver, []) if t0 <= m["t"] < t0 + dt.timedelta(seconds=60)]
    names = [rec] + common.STRONG.get(rec, [])
    for m in cands:
        if any(n in m["text"] for n in names):
            return m
    return cands[0] if cands else None


def _tip_excerpt(text, rx=r"\btips?\b|verify|claim|receipt|repro|pointer|welcome"):
    h = re.search(rx, text, re.I)
    return clip(text, h.start() if h else None, h.end() if h else None, limit=150)


def onboarding():
    import tracer_onboarding as to

    for f in ("results.json", "items.csv", "labels.csv", "rules.csv", "guides.csv"):
        require(ONB / f, "tracer_onboarding.py")
    world = _world(False)
    res = json.loads((ONB / "results.json").read_text())
    items = {r["item_id"]: r for r in csv.DictReader(open(ONB / "items.csv"))}
    labels = list(csv.DictReader(open(ONB / "labels.csv")))
    rules = {r["rule_id"]: r for r in csv.DictReader(open(ONB / "rules.csv"))}
    guides = {r["guide_id"]: r for r in csv.DictReader(open(ONB / "guides.csv"))}
    changes = _changelog()

    uptake = collections.Counter(r["rule"] for r in labels if r["label"] in N_UPTAKE)
    chosen = list(N_ALWAYS) + [r for r, _ in uptake.most_common() if r not in N_ALWAYS][:max(0, N_MAX - len(N_ALWAYS))]
    chosen.sort()

    def seat_name(n):
        return common.seat(n) if n else n

    observed = sorted({seat_name(it["agent"]) for it in items.values()})
    traces = []
    for rid in chosen:
        rule, rres = rules[rid], res["rules"][rid]
        proxy, anchor = re.compile(rule["keyword_proxy"], re.I), re.compile(N_ANCHOR.get(rid, r"$^"), re.I)

        def find(text):
            return proxy.search(text) or anchor.search(text)

        events, seen = [], collections.Counter()
        for r in labels:
            if r["rule"] != rid:
                continue
            it = items[r["item_id"]]
            eid = f"{r['item_id']}-{rid}"
            seen[eid] += 1
            if seen[eid] > 1:
                eid += f"-{seen[eid]}"
            events.append({"id": eid, "t": iso(_t(it["t"])), "agent": seat_name(it["agent"]),
                           "channel": "chat" if it["kind"] == "chat" else "memory",
                           "stance": N_STANCE[r["label"]], "conf": int(r["conf"]) if r["conf"] else None,
                           "room": it["room"] or None, "snippet": _excerpt(it["text"], find)})
        events.sort(key=lambda e: (e["t"], e["id"]))

        edges, quotes = [], []
        for giver, rec, rids, t in to.TIP_EDGES:
            if rid not in rids:
                continue
            m = _tip_message(world, giver, rec, t)
            edges.append({"from": giver, "to": rec, "t": iso(m["t"] if m else _t(t)), "kind": "tip",
                          "evidence": _tip_excerpt(m["text"]) if m else f"tip covering {', '.join(rids)}"})
            if m:
                quotes.append({"t": iso(m["t"]), "agent": giver, "text": _tip_excerpt(m["text"]), "note": f"tip to {rec}"})
        src, dst, t, rids, ev = N_NEAR_COPY
        if rid in rids:
            m = _chat_at(world, dst, t, r"claim")
            if m is None:
                raise RuntimeError(f"near-copy tip not found: {dst} at {t}")
            edges.append({"from": src, "to": dst, "t": iso(m["t"]), "kind": "echo", "evidence": ev})
        for r in labels:
            if r["rule"] == rid and r["label"] == "MUTATED" and r["version"]:
                it = items[r["item_id"]]
                quotes.append({"t": iso(_t(it["t"])), "agent": seat_name(it["agent"]),
                               "text": clip(r["version"], limit=QUOTE_MAX),
                               "note": f"mutated version ({it['kind']}, conf {r['conf']})"})
        edges.sort(key=lambda e: e["t"])
        quotes.sort(key=lambda q: q["t"])

        annotations = []
        for g in rule["source_guides"].split(";"):
            gid = g.split("(")[0]
            if gid in guides:
                gd = guides[gid]
                annotations.append({"t": iso(_t(gd["first_mention"])), "kind": "artifact",
                                    "label": f"Guide {gid} first mentioned: {clip(gd['title'], limit=80)}"})
        for d in sorted(set(re.findall(r"\d{4}-\d{2}-\d{2}", rule["scaffolding_overlap"]))):
            bullets = changes.get(d, [])
            hit = next((b for b in bullets if anchor.search(b)), None) or \
                next((b for b in bullets if re.search(r"chat|message|prompt|outreach", b, re.I)), None) or \
                (bullets[0] if bullets else None)
            label = clip(hit, limit=110) if hit else re.search(rf"[^;]*{d}[^;,]*", rule["scaffolding_overlap"]).group(0).strip()
            annotations.append({"t": iso(_t(d)), "kind": "scaffolding", "label": f"Scaffolding: {label}"})
        annotations.sort(key=lambda a: a["t"])

        names = set(observed) | {e["from"] for e in edges} | {e["to"] for e in edges} | {q["agent"] for q in quotes}
        agents = sorted((_agent(n, world, N_GROUP[to.cohort(world[2][n]["joined_at"])], split_deepseek=False)
                         for n in names), key=lambda a: (a["joined"] or "", a["name"]))

        lc, share = rres["label_counts"], rres["agent_uptake_share"]
        fisher = rres.get("fisher_p", {}).get("newcomer_vs_established")
        med = rres.get("newcomer_median_days_to_uptake")
        metrics = {
            "Labelled items showing the rule": sum(lc.values()),
            "States / follows / violates / mutates":
                f"{lc.get('STATES', 0)} / {lc.get('FOLLOWS', 0)} / {lc.get('VIOLATES', 0)} / {lc.get('MUTATED', 0)}",
            "Agents with uptake, newcomers (first 14 d)": f"{share['newcomer']:.0%}",
            "Agents with uptake, mid (first 14 d)": f"{share['mid']:.0%}",
            "Agents with uptake, pre-guide (first 14 d)": f"{share['pre_guide']:.0%}",
            "Agents with uptake, established (newcomer weeks)": f"{share['established']:.0%}",
            "Newcomer median days to first uptake": med if med is not None else "n/a",
            "Fisher p, newcomers vs established": fisher if fisher is not None else "n/a",
            "Rule first stated in a guide": rres["first_stated"],
            "Scaffolding overlap": "yes" if rule["scaffolding_overlap"] else "none found",
        }
        times = [_z(x) for x in [e["t"] for e in events] + [a["t"] for a in annotations] + [e["t"] for e in edges]]
        slug = rule["short"].replace("_", "-")
        traces.append(fit_window({
            "version": 0, "id": f"norm-{rid.lower()}-{slug}", "title": f"Norm {rid}: {N_TITLES.get(rid, rule['short'])}",
            "kind": "norm", "statement": rule["wording"],
            "source": "tracer_onboarding.py: rules hand-merged from agent-written onboarding guides; 336 sampled items "
                      "(each agent's first 14 days, plus established agents in newcomer weeks) labelled per rule by "
                      "Sonnet. Sampled, so a missing event is not evidence the norm was absent.",
            "start": iso(min(times) - LEAD_IN), "end": iso(max(times)),
            "agents": agents, "events": events, "exposures": [], "adoptions": [], "edges": edges, "persistence": [],
            "annotations": annotations, "quotes": quotes, "metrics": metrics,
        }))
    return traces


# --- 3. coined terms -------------------------------------------------------------------------------

T_TOP = 12
# Default stoplist for term picks: ideas.json ranks by adopter count, so its top terms are mostly generic vocabulary.
# Pass terms(stoplist=...) to change it.
T_GENERIC_SOFTWARE = (
    "http", "https", "readme", "gitlab", "github", "yaml", "json", "python3", "sha256", "sha-256", "backend",
    "frontend", "localstorage", "href", "hotfix", "rebased", "rebasing", "codebase", "favicon", "standalone",
    "read-only", "re-run", "root cause", "api key", "cloudflare", "netlify", "localtunnel", "sitemap", "validator",
    "server-side", "client-side", "opt-in", "runbook", "per-agent", "cross-agent", "one-pager", "blogpost",
)
T_GENERIC_WORK = ("end-of-day", "day-of", "easter eggs")
T_EXTERNAL_NAMES = ("substack", "lichess", "bluesky", "devoe")      # outside platforms and places, not coinages
T_STOPLIST = frozenset(T_GENERIC_SOFTWARE + T_GENERIC_WORK + T_EXTERNAL_NAMES)


def _term_rx(term):
    """Case-insensitive match of the term as a whole token (two-word terms allow any whitespace); a bare scheme
    like 'http' must not be the start of a URL."""
    body = r"\s+".join(re.escape(w) for w in term.split())
    tail = r"(?!s?://)" if re.fullmatch(r"https?|ftp", term) else ""
    return re.compile(rf"(?<![\w-]){body}(?![\w])(?!-\w){tail}", re.I)


def terms(top=T_TOP, stoplist=T_STOPLIST):
    """One trace per term: the `top` terms (by adopters, then messages) from ideas.json's durable + episodic lists
    after dropping `stoplist`."""
    require(IDEAS, "ideas.py")
    world = _world(True)
    ideas = json.loads(IDEAS.read_text())
    goals = common.load_goals()
    stop = {t.lower() for t in stoplist}
    pool = [(b, r) for b in ("durable", "episodic") for r in ideas[b] if r["term"].lower() not in stop]
    pool.sort(key=lambda br: (-len(br[1]["adopters"]), -br[1]["docs"]))
    misses = []
    traces = []
    for rank, (bucket, rec) in enumerate(pool[:top], 1):
        term, rx = rec["term"], _term_rx(rec["term"])
        t0 = _t(rec["origin_time"])

        def snippet(agent, t):
            m = _chat_at(world, agent, t, exact=True) or _chat_at(world, agent, t)
            h = (rx.search(m["text"]) or re.search(re.escape(term), m["text"], re.I)) if m else None
            if h is None:
                misses.append((term, agent, t))
                return clip(m["text"], limit=SNIPPET_MAX) if m else ""
            return clip(m["text"], h.start(), h.end(), limit=SNIPPET_MAX)

        events = [{"id": "e000", "t": iso(t0), "agent": rec["origin"], "channel": "chat", "stance": "originates",
                   "conf": None, "room": rec["room"], "snippet": snippet(rec["origin"], t0)}]
        adoptions = []
        for i, a in enumerate(rec["adopters"], 1):
            ta = _t(a["t"])
            eid = f"e{i:03d}"
            events.append({"id": eid, "t": iso(ta), "agent": a["agent"], "channel": "chat", "stance": "uses",
                           "conf": None, "room": a["room"], "snippet": snippet(a["agent"], ta)})
            adoptions.append({"agent": a["agent"], "t": iso(ta), "event": eid, "independent": False,
                              "sources": [rec["origin"]] if rec["origin"] != a["agent"] else []})

        weekly = rec.get("weekly") or []
        end = max(_t(a["t"]) for a in rec["adopters"])
        if weekly:
            end = max(end, _t(weekly[-1][0]) + dt.timedelta(days=6, hours=23, minutes=59, seconds=59))
        start = t0 - LEAD_IN
        annotations = []
        for g in goals:
            g_end = g["end"] or end
            if g["start"] <= end and g_end >= start:
                annotations.append({"t": iso(max(g["start"], start)), "kind": "goal",
                                    "label": f"Goal: {clip(g['goal'], limit=100)}"})

        new = {a["agent"] for a in rec["adopters"] if a["newcomer"]}
        names = dict.fromkeys([rec["origin"]] + [a["agent"] for a in rec["adopters"]])
        agents = sorted((_agent(n, world, "newcomer" if n in new else "present") for n in names),
                        key=lambda a: (a["joined"] or "", a["name"]))
        peak = max(weekly, key=lambda w: w[1]) if weekly else None
        metrics = {
            "Messages using the term": rec["docs"],
            "Adopters (2+ uses)": len(rec["adopters"]),
            "Adopters who joined after coinage": len(new),
            "Agents present at coinage": rec["present"],
            "Share of uses in the busiest 4 weeks": f"{rec['burst']:.0%}",
            "Busiest week (messages)": f"{peak[0]} ({peak[1]})" if peak else "n/a",
            "Weeks with any use": len(weekly),
            "Pick": f"#{rank} of {top} ({bucket})",
        }
        slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
        traces.append(fit_window({
            "version": 0, "id": f"term-{slug}", "title": f"Term \"{term}\"", "kind": "term",
            "statement": f"The term \"{term}\", first used in chat by {rec['origin']} on {t0:%Y-%m-%d} "
                         f"(#{rec['room']}).",
            "source": "ideas.py: non-dictionary words and high-lift two-word phrases in chat; an agent adopts a "
                      "term at its first use once it has used it in 2+ messages. Adoption sources are just the "
                      f"coiner (no exposure model). {bucket.capitalize()} = "
                      + ("under 50% of uses in any 4 weeks." if bucket == "durable" else "60%+ of uses in 4 weeks."),
            "start": iso(start), "end": iso(end),
            "agents": agents, "events": events, "exposures": [], "adoptions": adoptions, "edges": [],
            "persistence": [], "annotations": annotations, "quotes": [], "metrics": metrics,
        }))
    if misses:
        print(f"terms: {len(misses)} events whose chat message lacks the term (snippet = message start): {misses[:5]}")
    else:
        print("terms: every event snippet contains its term")
    return traces


SOURCES = {"hostility": hostility, "onboarding": onboarding, "terms": terms}
# Files each source writes, so a re-export can drop traces that a source no longer produces.
OUTPUT_GLOBS = {"hostility": ["hostility.json"], "onboarding": ["norm-*.json"], "terms": ["term-*.json"]}
