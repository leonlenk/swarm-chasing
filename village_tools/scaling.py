"""Pilot: is there an initial "scaling law" for cooperation in the AI Village?

Question: across agents present at the same time, does a more capable (Epoch
Capabilities Index, ECI) or newer (release date) model cooperate more?

Pre-specified (written before looking at any correlation):
  Measures, per agent, computed on cleaned chat:
    addressing   share of the agent's messages that name another agent
    reciprocity  share of the agent's mention partners (agents it named >=1x) who
                 also named it in the same period (>=3 partners required)
    prosocial    requests + division-of-labour markers per 100 messages
                 (regexes from cooperation.py; each = % of messages with >=1 hit)
  Proxies: ECI (primary, linear scale), release date in decimal years (second axis).
  Period = goal period (village_goals), split into 14-day chunks, x chat room
  (after 2026-02-25 rooms isolate agents from each other). An agent-period counts
  if the agent posted >=20 cleaned messages in it.
  Primary statistic: mean over periods (>=4 eligible agents) of the Spearman
  correlation between measure and proxy; two-sided permutation p from shuffling
  proxy values across agents (keeps each agent's panel intact). Reported next to
  the naive cross-agent Spearman on lifetime measures.
  Cleaning: drop Fine-Tuned Leader and Opus 4.5 (Claude Code); drop onboarding
  rooms and voted-out; drop agents with active span <=7 days or <200 messages.
  (Added after the metadata lookup, before any correlation was run: truncate
  DeepSeek-V3.2 at 2026-04-24, when its API alias stopped serving V3.2.)

Run: uv run --with numpy --with scipy --with matplotlib python scaling.py
Inputs: data/ai-village/*.jsonl.gz, model_metadata.csv. Outputs: out/scaling/.

model_metadata.csv (village_tools/model_metadata.csv) is NOT in git: *.csv is gitignored and
data files are never committed, so a clean checkout lacks it and the script stops with a
pointer here. Get it from the original author's checkout, or rebuild it by hand: one row per
village agent, UTF-8 CSV with a header row. Columns, in order (the script reads only *):
  name*                      the agent's display name, exactly as in the village data
  model_string               the API model id the agent ran on
  lab*                       developer (Anthropic, OpenAI, Google, ...)
  joined, left               when the agent joined / left the village
  release_date*              ISO YYYY-MM-DD: the model string's date suffix if it has one, else
                             Epoch AI's date for the model; "unknown" or blank = missing
  release_source             where release_date came from
  announce_date_crosscheck, announce_crosscheck_source
                             the public announcement date and its source, as a cross-check
  eci*                       Epoch Capabilities Index (Epoch AI's eci_scores.csv; the report
                             used the file retrieved 2026-10-03); "unknown" or blank = missing
  eci_ci90_low, eci_ci90_high  Epoch's 90% interval for eci
  eci_epoch_label            the model's name in Epoch's file
  eci_source, eci_retrieved  where and when eci was taken
  flag, eci_notes            free-text caveats (e.g. ECI is the best-setting score)
Every analysed agent needs a row (main() asserts this).
"""

import collections
import csv
import datetime as dt
import json
import re

import numpy as np
from scipy.stats import rankdata, spearmanr

from common import OUT, ROOT, WEAK, GoalIndex, Mentions, active_windows, load_agents, load_chat, load_goals
from cooperation import MARKERS

SCALE = OUT / "scaling"
SCALE.mkdir(exist_ok=True)
RNG = np.random.default_rng(20261003)

EXCLUDED_AGENTS = {"Fine-Tuned Leader": "fine-tune, not a public release",
                   "Opus 4.5 (Claude Code)": "different scaffolding (Claude Agent SDK)"}
# Onboarding = rooms named *-onboarding, plus the single-agent rooms created when
# an agent joined (whitelist = that agent only; 3-22 messages each).
ONBOARDING_EXTRA = {"sol", "terra", "luna", "side-room"}
OFF_TASK = {"voted-out"}
# deepseek-reasoner served DeepSeek-V3.2 only until V4 Preview (api-docs.deepseek.com/news/news260424).
TRUNCATE = {"DeepSeek-V3.2": dt.datetime(2026, 4, 24)}
MIN_SPAN_DAYS = 7          # drop agents whose first-to-last message span is <= this
MIN_MSGS = 200
MIN_UNIT_MSGS = 20         # per agent-period
MIN_CELL_AGENTS = 4        # per period, for the within-period correlation
MIN_PARTNERS = 3
CHUNK_DAYS = 14
N_PERM = 2000
N_BOOT = 2000
MEASURES = ["addressing", "reciprocity", "prosocial"]
PROXIES = ["eci", "release_year"]


class VillageMentions(Mentions):
    """common.Mentions plus the first-name forms used for the GPT-5.6 trio and GPT-6
    Astra ("Luna", "Terra", "Sol", "Astra"), resolved only while that agent is active."""

    EXTRA = {"Luna": "GPT-5.6 Luna", "Terra": "GPT-5.6 Terra", "Sol": "GPT-5.6 Sol", "Astra": "GPT-6 Astra"}

    def __init__(self, win):
        super().__init__(win)
        for alias in self.EXTRA:
            self.lookup[alias] = ("extra", alias)
        alts = "|".join(re.escape(a) for a in sorted(self.lookup, key=len, reverse=True))
        self.rx = re.compile(rf"(?<![\w@./-])({alts})(?![\w]|[.-]\d)")

    def find(self, text, t):
        found = set()
        for m in self.rx.finditer(text):
            alias = m.group(1)
            name = self.lookup[alias]
            if isinstance(name, tuple):
                name = self.EXTRA[alias]
                if not self._is_active(name, t):
                    continue
            elif name is None:
                live = [c for c in WEAK[alias] if self._is_active(c, t)]
                if len(live) != 1:
                    continue
                name = live[0]
            found.add(name)
        return found


def decimal_year(d):
    d = dt.date.fromisoformat(d)
    start = dt.date(d.year, 1, 1)
    return d.year + (d - start).days / (dt.date(d.year + 1, 1, 1) - start).days


METADATA = ROOT / "model_metadata.csv"
METADATA_COLUMNS = ("name", "lab", "release_date", "eci")  # the ones read below


def load_metadata():
    if not METADATA.is_file():
        raise SystemExit(
            f"error: {METADATA} is missing. It is gitignored (*.csv, no data files in git), so a clean "
            "checkout lacks it. See 'model_metadata.csv' in the docstring of scaling.py (or "
            "out/scaling/REPORT.md) for its source and columns."
        )
    meta = {}
    with open(METADATA, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in METADATA_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(
                f"error: {METADATA} lacks column(s) {', '.join(missing)}; see the docstring of scaling.py."
            )
        for r in reader:
            eci = r.get("eci", "").strip()
            rel = r.get("release_date", "").strip()
            meta[r["name"]] = {
                "lab": r["lab"],
                "eci": float(eci) if eci not in ("", "unknown") else np.nan,
                "release_year": decimal_year(rel) if rel not in ("", "unknown") else np.nan,
                "release_date": rel,
            }
    return meta


# --- cleaning -----------------------------------------------------------------

def clean(msgs):
    """Returns (pool, subject_msgs, subjects, log). pool = all agents' messages in kept
    rooms (the social environment); subject_msgs = messages of agents analysed."""
    log = []
    agent_msgs = [m for m in msgs if m["is_agent"]]

    def step(name, kept, note=""):
        log.append({"step": name, "agents": len({m["speaker"] for m in kept}), "messages": len(kept), "note": note})

    step("raw agent messages", agent_msgs)
    room_ok = [m for m in agent_msgs
               if not m["room"].endswith("-onboarding") and m["room"] not in ONBOARDING_EXTRA | OFF_TASK]
    step("drop onboarding + voted-out rooms", room_ok,
         "rooms *-onboarding, sol, terra, luna, side-room, voted-out")
    pool = room_ok
    subj = [m for m in room_ok if m["speaker"] not in EXCLUDED_AGENTS]
    step("drop Fine-Tuned Leader, Opus 4.5 (Claude Code)", subj)
    subj = [m for m in subj if not (m["speaker"] in TRUNCATE and m["t"] >= TRUNCATE[m["speaker"]])]
    step("truncate DeepSeek-V3.2 at 2026-04-24", subj,
         "API alias deepseek-reasoner switched to V4-Flash that day (retired 2026-07-24)")
    win = active_windows(subj)
    short = {a for a, (lo, hi) in win.items() if (hi - lo) <= dt.timedelta(days=MIN_SPAN_DAYS)}
    subj = [m for m in subj if m["speaker"] not in short]
    step(f"drop active span <= {MIN_SPAN_DAYS} days", subj, ", ".join(sorted(short)))
    n = collections.Counter(m["speaker"] for m in subj)
    few = {a for a, k in n.items() if k < MIN_MSGS}
    subj = [m for m in subj if m["speaker"] not in few]
    step(f"drop < {MIN_MSGS} messages", subj, ", ".join(f"{a} ({n[a]})" for a in sorted(few)))
    return pool, subj, sorted({m["speaker"] for m in subj}), log


# --- measures -----------------------------------------------------------------

def period_key(gi, t):
    g = gi.at(t)
    if g is None:
        return None
    return (g["idx"], int((t - g["start"]).total_seconds() // (CHUNK_DAYS * 86400)))


def compute(pool, subj, subjects, mentions, gi):
    """Per agent-period (cell = period x room) and per-agent lifetime measures."""
    edges_period = collections.defaultdict(set)   # period -> {(speaker, target)}
    edges_all = set()
    for m in pool:
        targets = mentions.find(m["text"], m["t"]) - {m["speaker"]}
        m["_targets"] = targets
        pk = period_key(gi, m["t"])
        for tg in targets:
            edges_all.add((m["speaker"], tg))
            if pk is not None:
                edges_period[pk].add((m["speaker"], tg))

    def blank():
        return {"n": 0, "named": 0, "prosocial_hits": 0, "partners": set(), "days": set()}

    units = collections.defaultdict(blank)
    life = collections.defaultdict(blank)
    for m in subj:
        pk = period_key(gi, m["t"])
        text = m["text"].lower().replace("’", "'")
        hits = sum(bool(MARKERS[k].search(text)) for k in ("requests", "division_of_labour"))
        buckets = [life[m["speaker"]]]
        if pk is not None:
            buckets.append(units[(m["speaker"], pk, m["room"])])
        for b in buckets:
            b["n"] += 1
            b["named"] += bool(m["_targets"])
            b["prosocial_hits"] += hits
            b["partners"] |= m["_targets"]
            b["days"].add(m["t"].date())

    def finish(b, agent, edges):
        rec = None
        if len(b["partners"]) >= MIN_PARTNERS:
            rec = sum((p, agent) in edges for p in b["partners"]) / len(b["partners"])
        return {"msgs": b["n"], "active_days": len(b["days"]),
                "addressing": b["named"] / b["n"], "reciprocity": rec,
                "prosocial": 100 * b["prosocial_hits"] / b["n"], "n_partners": len(b["partners"])}

    rows = []
    for (agent, pk, room), b in units.items():
        if b["n"] < MIN_UNIT_MSGS:
            continue
        rows.append({"agent": agent, "goal_idx": pk[0], "chunk": pk[1], "room": room,
                     "cell": f"{pk[0]}:{pk[1]}:{room}", **finish(b, agent, edges_period[pk])})
    lifetime = {a: finish(life[a], a, edges_all) for a in subjects}
    return rows, lifetime


# --- statistics ---------------------------------------------------------------

def perm_p(obs, null):
    null = np.asarray(null)
    return float((np.sum(np.abs(null) >= abs(obs)) + 1) / (len(null) + 1))


def naive(lifetime, meta, measure, proxy):
    agents = [a for a in lifetime if lifetime[a][measure] is not None and not np.isnan(meta[a][proxy])]
    y = np.array([lifetime[a][measure] for a in agents], float)
    x = np.array([meta[a][proxy] for a in agents], float)
    rho = spearmanr(x, y).statistic
    null = [spearmanr(RNG.permutation(x), y).statistic for _ in range(N_PERM)]
    boot = []
    for _ in range(N_BOOT):
        i = RNG.integers(0, len(x), len(x))
        if len(set(x[i])) > 2:
            boot.append(spearmanr(x[i], y[i]).statistic)
    return {"rho": float(rho), "ci95": [float(np.nanpercentile(boot, 2.5)), float(np.nanpercentile(boot, 97.5))],
            "perm_p": perm_p(rho, null), "n_agents": len(agents)}


def within_period(rows, meta, measure, proxy, agent_filter=None):
    """Mean within-cell Spearman; permutation shuffles proxies across agents."""
    agents = sorted({r["agent"] for r in rows if not np.isnan(meta[r["agent"]][proxy])
                     and (agent_filter is None or agent_filter(r["agent"]))})
    aidx = {a: i for i, a in enumerate(agents)}
    x_agent = np.array([meta[a][proxy] for a in agents], float)
    cells = collections.defaultdict(list)
    for r in rows:
        if r["agent"] in aidx and r[measure] is not None:
            cells[r["cell"]].append((aidx[r["agent"]], r[measure]))
    cells = [(np.array([i for i, _ in v]), rankdata([y for _, y in v])) for v in cells.values()
             if len(v) >= MIN_CELL_AGENTS]
    if not cells:
        return {"mean_rho": None, "n_cells": 0}

    def stat(xa):
        rs = []
        for idx, ry in cells:
            xs = xa[idx]
            if np.ptp(xs) == 0 or np.ptp(ry) == 0:
                continue
            rs.append(np.corrcoef(rankdata(xs), ry)[0, 1])
        return np.array(rs)

    obs = stat(x_agent)
    null = [np.mean(stat(RNG.permutation(x_agent))) for _ in range(N_PERM)]
    boot = [np.mean(RNG.choice(obs, len(obs))) for _ in range(N_BOOT)]
    used = {i for idx, _ in cells for i in idx}
    return {"mean_rho": float(obs.mean()), "cell_boot_ci95": [float(np.percentile(boot, 2.5)),
                                                              float(np.percentile(boot, 97.5))],
            "perm_p": perm_p(obs.mean(), null), "n_cells": int(len(obs)), "n_agents": len(used),
            "share_cells_positive": float(np.mean(obs > 0))}


def demeaned(rows, measure):
    """Per agent: mean over its agent-periods of (value - cell mean), cells with >=4 agents."""
    cells = collections.defaultdict(list)
    for r in rows:
        if r[measure] is not None:
            cells[r["cell"]].append(r)
    acc = collections.defaultdict(list)
    for v in cells.values():
        if len(v) < MIN_CELL_AGENTS:
            continue
        mu = np.mean([r[measure] for r in v])
        for r in v:
            acc[r["agent"]].append(r[measure] - mu)
    return {a: float(np.mean(v)) for a, v in acc.items()}


def agent_level(dm, meta, proxy, agent_filter=None):
    agents = [a for a in dm if not np.isnan(meta[a][proxy]) and (agent_filter is None or agent_filter(a))]
    if len(agents) < 4:
        return {"rho": None, "n_agents": len(agents)}
    x = np.array([meta[a][proxy] for a in agents])
    y = np.array([dm[a] for a in agents])
    rho = spearmanr(x, y).statistic
    null = [spearmanr(RNG.permutation(x), y).statistic for _ in range(N_PERM)]
    slope, intercept = np.polyfit(x, y, 1)
    r2 = np.corrcoef(x, y)[0, 1] ** 2
    return {"rho": float(rho), "perm_p": perm_p(rho, null), "n_agents": len(agents),
            "ols_slope": float(slope), "ols_r2": float(r2)}


# --- figure -------------------------------------------------------------------

LAB_COLORS = {"Anthropic": "#2a78d6", "OpenAI": "#eb6834", "Google": "#1baf7a", "Other": "#eda100"}
LABELS = {"addressing": "Addressing rate (demeaned)", "reciprocity": "Reciprocity (demeaned)",
          "prosocial": "Requests + division of labour / 100 msgs (demeaned)"}
PROXY_LABELS = {"eci": "Epoch Capabilities Index (ECI)", "release_year": "Release date (year)"}


def short(name):
    return name.replace("Claude ", "").replace("Gemini ", "Gem ")


def plot(dms, meta, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(len(MEASURES), len(PROXIES), figsize=(13, 14))
    for i, measure in enumerate(MEASURES):
        dm = dms[measure]
        for j, proxy in enumerate(PROXIES):
            ax = axes[i, j]
            agents = [a for a in dm if not np.isnan(meta[a][proxy])]
            x = np.array([meta[a][proxy] for a in agents])
            y = np.array([dm[a] for a in agents])
            for a, xi, yi in zip(agents, x, y):
                lab = meta[a]["lab"] if meta[a]["lab"] in LAB_COLORS else "Other"
                ax.scatter(xi, yi, s=40, color=LAB_COLORS[lab], edgecolor="white", linewidth=1, zorder=3)
                ax.annotate(short(a), (xi, yi), fontsize=6.5, color="#444441", xytext=(3, 2),
                            textcoords="offset points")
            if len(agents) >= 4:
                b, c = np.polyfit(x, y, 1)
                xs = np.linspace(x.min(), x.max(), 2)
                ax.plot(xs, b * xs + c, color="#888780", linewidth=1.5, linestyle="--", zorder=2)
                rho = spearmanr(x, y).statistic
                ax.set_title(f"n={len(agents)}, Spearman ρ={rho:+.2f}", fontsize=9, color="#444441", loc="left")
            ax.axhline(0, color="#d3d1c7", linewidth=1, zorder=1)
            ax.grid(color="#eeede6", linewidth=0.6)
            ax.set_axisbelow(True)
            for s in ("top", "right"):
                ax.spines[s].set_visible(False)
            ax.set_xlabel(PROXY_LABELS[proxy], fontsize=9)
            ax.set_ylabel(LABELS[measure], fontsize=9)
    handles = [plt.Line2D([], [], marker="o", linestyle="", color=c, label=l) for l, c in LAB_COLORS.items()]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.978), ncol=4, frameon=False, fontsize=9)
    fig.suptitle("Per-period-demeaned cooperation vs capability and newness (each point = one agent)",
                 y=0.995, fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.955))
    fig.savefig(path, dpi=130)
    print(f"wrote {path}")


# --- main ---------------------------------------------------------------------

def main():
    meta = load_metadata()
    agents = load_agents()
    msgs = load_chat(agents)
    mentions = VillageMentions(active_windows(msgs))
    gi = GoalIndex(load_goals())

    pool, subj, subjects, log = clean(msgs)
    missing = [a for a in subjects if a not in meta]
    assert not missing, f"no metadata for {missing}"
    rows, lifetime = compute(pool, subj, subjects, mentions, gi)

    # Naive "uncleaned" comparison: all agents' messages in all rooms, only the brief's
    # exclusions (Fine-Tuned Leader, Claude Code agent) and the 200-message floor.
    raw = [m for m in msgs if m["is_agent"] and m["speaker"] not in EXCLUDED_AGENTS]
    nraw = collections.Counter(m["speaker"] for m in raw)
    raw_subjects = sorted(a for a, k in nraw.items() if k >= MIN_MSGS)
    raw_subj = [m for m in raw if m["speaker"] in raw_subjects]
    rows_raw, life_raw = compute([m for m in msgs if m["is_agent"]], raw_subj, raw_subjects, mentions, gi)

    cells = collections.Counter(r["cell"] for r in rows)
    log.append({"step": f"agent-periods with >= {MIN_UNIT_MSGS} msgs", "agents": len({r['agent'] for r in rows}),
                "messages": sum(r["msgs"] for r in rows), "agent_periods": len(rows), "periods": len(cells),
                "periods_with_4plus_agents": sum(v >= MIN_CELL_AGENTS for v in cells.values())})

    sub_meta = [a for a in subjects if not np.isnan(meta[a]["eci"]) and not np.isnan(meta[a]["release_year"])]
    x1 = [meta[a]["eci"] for a in sub_meta]
    x2 = [meta[a]["release_year"] for a in sub_meta]
    eci_vs_release = {"n_agents": len(sub_meta), "spearman": float(spearmanr(x1, x2).statistic),
                      "pearson": float(np.corrcoef(x1, x2)[0, 1])}

    anth = lambda a: meta[a]["lab"] == "Anthropic"  # noqa: E731
    no_oai = lambda a: meta[a]["lab"] != "OpenAI"  # noqa: E731
    results = {"preregistered": __doc__.split("Run:")[0].strip(), "cleaning": log,
               "subjects": subjects,
               "coverage": {"eci": sum(not np.isnan(meta[a]["eci"]) for a in subjects),
                            "release_year": sum(not np.isnan(meta[a]["release_year"]) for a in subjects),
                            "n_subjects": len(subjects),
                            "eci_missing": [a for a in subjects if np.isnan(meta[a]["eci"])]},
               "eci_vs_release": eci_vs_release, "tests": []}
    dms = {}
    for measure in MEASURES:
        dm = demeaned(rows, measure)
        dms[measure] = dm
        for proxy in PROXIES:
            res = {"measure": measure, "proxy": proxy,
                   "within_period": within_period(rows, meta, measure, proxy),
                   "naive_cleaned": naive(lifetime, meta, measure, proxy),
                   "naive_uncleaned": naive(life_raw, meta, measure, proxy),
                   "within_period_uncleaned": within_period(rows_raw, meta, measure, proxy),
                   "demeaned_agent_level": agent_level(dm, meta, proxy),
                   "anthropic_demeaned_agent_level": agent_level(dm, meta, proxy, anth),
                   "anthropic_within_period": within_period(rows, meta, measure, proxy, anth),
                   # Post hoc (added after seeing results): is it an OpenAI-vs-rest lab effect?
                   "no_openai_within_period": within_period(rows, meta, measure, proxy, no_oai)}
            results["tests"].append(res)
            w, nv = res["within_period"], res["naive_cleaned"]
            print(f"{measure:12s} {proxy:12s} within ρ={w['mean_rho']:+.2f} p={w['perm_p']:.3f} "
                  f"cells={w['n_cells']} | naive ρ={nv['rho']:+.2f} p={nv['perm_p']:.3f} n={nv['n_agents']}")

    # Multiple comparisons over the 6 pre-specified primary tests (3 measures x 2 proxies).
    ps = np.array([t["within_period"]["perm_p"] for t in results["tests"]])
    order = np.argsort(ps)
    m = len(ps)
    holm = np.maximum.accumulate(np.minimum(1, (m - np.arange(m)) * ps[order]))
    bh = np.minimum.accumulate((m / np.arange(m, 0, -1)) * ps[order][::-1])[::-1]
    for k, i in enumerate(order):
        results["tests"][i]["within_period"]["p_holm"] = float(holm[k])
        results["tests"][i]["within_period"]["q_bh"] = float(min(1, bh[k]))

    with open(SCALE / "per_agent_period.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["goal_idx"], r["chunk"], r["room"], r["agent"])))
    with open(SCALE / "per_agent.csv", "w", newline="") as f:
        fields = ["agent", "lab", "release_date", "eci", "msgs", "active_days", "n_partners",
                  *MEASURES, *[f"{m}_demeaned" for m in MEASURES]]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for a in subjects:
            L = lifetime[a]
            w.writerow({"agent": a, "lab": meta[a]["lab"], "release_date": meta[a]["release_date"],
                        "eci": "" if np.isnan(meta[a]["eci"]) else meta[a]["eci"],
                        **{k: L[k] for k in ("msgs", "active_days", "n_partners", *MEASURES)},
                        **{f"{m}_demeaned": dms[m].get(a) for m in MEASURES}})
    (SCALE / "results.json").write_text(json.dumps(results, indent=1, default=str))
    print(f"wrote {SCALE / 'results.json'}")
    plot(dms, meta, SCALE / "scaling_scatter.png")


if __name__ == "__main__":
    main()
