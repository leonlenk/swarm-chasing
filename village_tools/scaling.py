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
     (--plot-only redraws out/scaling/scaling_scatter.png from results.json and per_agent.csv)
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
import sys
from pathlib import Path

import numpy as np
from scipy.stats import rankdata, spearmanr

import paperfig
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

MEASURE_LABELS = {"addressing": "Addressing", "reciprocity": "Reciprocity", "prosocial": "Prosocial"}
PROXY_SHORT = {"eci": "ECI", "release_year": "release"}


def short(name):
    return name.replace("Claude ", "").replace("Gemini ", "Gem ")


def _place_labels(ax, pts, markers, *, avoid=(), fontsize=7):
    """Label points (x, y, text) without overlaps. For each point, try directions E, W, S, N and the
    diagonals at growing distances; keep the first box that stays inside the axes and clears every
    other marker, every placed label and the `avoid` artists. Far placements get a thin leader line.
    A point with no free spot stays unlabelled (per_agent.csv lists every agent)."""
    import math

    fig = ax.figure
    fig.canvas.draw()  # settle constrained layout first, so pixel positions are final
    rend = fig.canvas.get_renderer()
    to_px = ax.transData.transform
    box = ax.get_window_extent(rend)
    pt = fig.dpi / 72
    r_px = 2.8 * pt  # marker half-size plus a hair
    others = [tuple(a.get_window_extent(rend).extents) for a in avoid]
    placed = []
    dirs = [0, 180, 270, 90, 315, 225, 45, 135]  # degrees, counter-clockwise from east
    for x, y, text in pts:
        obstacles = others + [(*(to_px(m) - r_px), *(to_px(m) + r_px)) for m in markers if tuple(m) != (x, y)]
        done = False
        for radius in (3.5, 7.0, 11.0, 15.0):
            for deg in dirs:
                c, sn = math.cos(math.radians(deg)), math.sin(math.radians(deg))
                ha = "left" if c > 0.3 else "right" if c < -0.3 else "center"
                va = "bottom" if sn > 0.3 else "top" if sn < -0.3 else "center"
                t = ax.annotate(text, (x, y), xytext=(radius * c, radius * sn), textcoords="offset points",
                                ha=ha, va=va, fontsize=fontsize, color=paperfig.INK,
                                arrowprops=dict(arrowstyle="-", color=paperfig.INK3, lw=0.4, shrinkA=0,
                                                shrinkB=2.5) if radius > 7 else None)
                bb = t.get_window_extent(rend)
                r = (bb.x0 - 0.5 * pt, bb.y0 - 0.5 * pt, bb.x1 + 0.5 * pt, bb.y1 + 0.5 * pt)
                inside = box.x0 <= r[0] and r[2] <= box.x1 and box.y0 <= r[1] and r[3] <= box.y1
                hit = any(not (r[2] < o[0] or r[0] > o[2] or r[3] < o[1] or r[1] > o[3]) for o in placed + obstacles)
                if inside and not hit:
                    placed.append(r)
                    done = True
                    break
                t.remove()
            if done:
                break


def plot(path=None):
    """Two-panel figure from the saved outputs (results.json, per_agent.csv): no recomputation.

    (a) the six pre-registered tests: within-period mean Spearman rho (filled; Holm-adjusted
        permutation p at the right) and the naive cleaned cross-agent rho with its agent-bootstrap
        95% CI (hollow). The period-bootstrap CI is not drawn: it ignores that the same agents
        recur across periods and is too narrow.
    (b) per-period-demeaned addressing rate vs ECI, one point per agent, lab by colour and marker.
    """
    paperfig.use()
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    path = path or SCALE / "scaling_scatter.png"
    res = json.loads((SCALE / "results.json").read_text())
    tests = {(t["measure"], t["proxy"]): t for t in res["tests"]}
    with open(SCALE / "per_agent.csv", newline="") as f:
        rows = list(csv.DictReader(f))

    fig = plt.figure(figsize=paperfig.size(1.0, height_in=2.6))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.1, 1.0], wspace=0.12)
    ax, bx = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])

    # (a) forest plot -------------------------------------------------------------
    ys, labels, y = [], [], 0.0
    for m in MEASURES:
        for pr in PROXIES:
            ys.append(y)
            labels.append(f"{MEASURE_LABELS[m]}, {PROXY_SHORT[pr]}")
            y += 1.0
        y += 0.45  # gap between measures
    ys = np.array(ys)
    dy = 0.17
    ax.axvline(0, color=paperfig.RULE, lw=0.5, zorder=1)
    k = 0
    for m in MEASURES:
        for pr in PROXIES:
            t = tests[(m, pr)]
            nv, w = t["naive_cleaned"], t["within_period"]
            lo, hi = nv["ci95"]
            ax.plot([lo, hi], [ys[k] + dy] * 2, color=paperfig.INK3, lw=0.8, solid_capstyle="butt", zorder=2)
            ax.plot([nv["rho"]], [ys[k] + dy], "o", ms=3.6, mfc="white", mec=paperfig.INK3, mew=0.8, zorder=3)
            ax.plot([w["mean_rho"]], [ys[k] - dy], "o", ms=3.8, mfc=paperfig.INK, mec=paperfig.INK, zorder=4)
            ax.text(1.02, ys[k], f"{w['p_holm']:.2f}", transform=ax.get_yaxis_transform(), ha="left", va="center",
                    fontsize=7, color=paperfig.INK)
            k += 1
    ax.text(1.02, ys[0] - 0.95, "$p_{\\mathrm{Holm}}$", transform=ax.get_yaxis_transform(), ha="left",
            va="center", fontsize=7, color=paperfig.INK)
    ax.set_yticks(ys, labels)
    ax.tick_params(axis="y", length=0, pad=3)
    ax.spines["left"].set_visible(False)
    ax.set_ylim(ys[-1] + 0.7, ys[0] - 1.2)
    ax.set_xlim(-0.75, 0.75)
    ax.set_xticks([-0.6, -0.3, 0, 0.3, 0.6])
    ax.set_xticklabels(["\u22120.6", "\u22120.3", "0", "0.3", "0.6"])
    ax.set_xlabel("Spearman \u03c1 with ECI or release date")
    handles = [Line2D([], [], ls="", marker="o", ms=3.8, mfc=paperfig.INK, mec=paperfig.INK,
                      label="within period (mean)"),
               Line2D([], [], color=paperfig.INK3, lw=0.8, marker="o", ms=3.6, mfc="white", mec=paperfig.INK3,
                      mew=0.8, label="across agents, 95% CI")]
    leg = ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.0, 1.0), ncol=2, handlelength=1.4,
                    columnspacing=0.9, borderaxespad=0.15)
    leg.set_in_layout(False)  # it sits in the title row; keep it out of constrained layout
    ax.set_title("(a)", loc="left", x=-0.47, pad=5, fontweight="bold")

    # (b) one scatter: demeaned addressing vs ECI ------------------------------------
    pts = [(r["agent"], r["lab"], float(r["eci"]), 100 * float(r["addressing_demeaned"]))
           for r in rows if r.get("eci") and r.get("addressing_demeaned")]
    x = np.array([p[2] for p in pts])
    yv = np.array([p[3] for p in pts])
    bx.axhline(0, color=paperfig.HAIR, lw=0.6, zorder=1)
    slope, icpt = np.polyfit(x, yv, 1)
    xs = np.array([x.min(), x.max()])
    bx.plot(xs, slope * xs + icpt, color=paperfig.INK3, lw=0.8, zorder=2)
    for lab in paperfig.LAB:
        sel = [p for p in pts if (p[1] if p[1] in paperfig.LAB else "Other") == lab]
        if not sel:
            continue
        col, mk = paperfig.LAB[lab]
        bx.scatter([p[2] for p in sel], [p[3] for p in sel], s=13, marker=mk, color=col, edgecolor="white",
                   linewidth=0.4, zorder=3, label=lab if lab != "Other" else "other labs")
    agent_level = tests[("addressing", "eci")]["demeaned_agent_level"]
    rho_txt = f"{agent_level['rho']:+.2f}".replace("-", "\u2212")
    bx.set_title(f"Spearman \u03c1 = {rho_txt}, n = {agent_level['n_agents']} agents", loc="right", pad=5, fontsize=7)
    bx.set_xlim(x.min() - 0.3 * (x.max() - x.min()), x.max() + 0.03 * (x.max() - x.min()))  # room for the legend
    bx.set_xlabel("Epoch Capabilities Index (ECI)")
    bx.set_ylabel("Addressing rate, demeaned (pp)")
    leg = bx.legend(loc="lower left", ncol=1, handletextpad=0.1, borderaxespad=0.3, markerscale=1.0)
    bx.set_title("(b)", loc="left", x=-0.2, pad=5, fontweight="bold")
    order = np.argsort(yv)
    pick = list(order[:4]) + list(order[-2:])  # the most negative and most positive agents
    _place_labels(bx, [(x[i], yv[i], short(pts[i][0])) for i in pick], list(zip(x, yv)), avoid=(leg,))

    out = paperfig.save(fig, Path(path).with_suffix(""))
    plt.close(fig)
    print("wrote " + ", ".join(str(o) for o in out))


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
    plot()


if __name__ == "__main__":
    if "--plot-only" in sys.argv:  # re-render the figure from results.json + per_agent.csv
        plot()
    else:
        main()
