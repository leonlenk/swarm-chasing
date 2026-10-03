"""Pilot: is same-lab agent-to-agent communication more incoherent than cross-lab?

Pre-specified (written before computing any comparison):
  Exchange = message by agent A, then a message by agent B != A in the same room
  within 30 min. Two mutually exclusive strata:
    mention   B's message names A (VillageMentions); A = A's latest message in
              that room in the 30 min before B
    adjacent  B is the next agent to speak in the room after A and does not name A
  Both A's and B's messages must survive scaling.py cleaning. Pair type = same lab
  vs cross lab (model_metadata lab); same_line flags the GPT-5.6 Sol/Terra/Luna trio.
  Proxies on B's reply (all exchanges):
    confusion      clarification/confusion regex (binary)
    contradiction  contradiction/correction regex (binary)
    miscomm        confusion OR contradiction
    relevance      TF-IDF cosine(A, B), agent names stripped (low = talking past)
    echo           share of B's word trigrams that occur in A (high = mirroring)
    self_rep       max TF-IDF cosine of B to B's own previous 3 messages
    praise         praise-loop regex (binary)
  Primary statistic per proxy: for each replier with >= MIN_SIDE exchanges on both
  sides, mean(same) - mean(cross); averaged over repliers weighted by exchanges and
  unweighted. Replier-cluster bootstrap CI. Permutation p: shuffle the addressee's
  lab (i.e. the same/cross flag) within replier x goal period. Secondary: weighted
  mean of within (replier x goal period) differences. Naive pooled same - cross
  shown alongside.
  LLM judging: ~150 exchanges stratified by same/cross x replier lab group
  (Anthropic/OpenAI/Google/other) x stratum, blinded, 3 judges, ~21 overlap items.

Run: uv run --with scikit-learn --with numpy --with scipy --with matplotlib python coherence.py
  (--judged-only reuses results_auto.json and skips the proxy permutations; --resample redraws judge items.)
  (re-running with judge_ratings_*.json present also produces the judged analysis).
Outputs: out/coherence/.
"""

import collections
import csv
import datetime as dt
import json
import random
import re
import sys

import numpy as np

from common import OUT, STRONG, WEAK, GoalIndex, active_windows, load_agents, load_chat, load_goals
from scaling import VillageMentions, clean, load_metadata

COH = OUT / "coherence"
COH.mkdir(exist_ok=True)
SEED = 20261003
WINDOW = dt.timedelta(minutes=30)
MIN_SIDE = 10          # replier needs >= this many same AND cross exchanges
N_PERM = 500
N_BOOT = 2000
SAME_LINE = {"GPT-5.6 Sol": "GPT-5.6", "GPT-5.6 Terra": "GPT-5.6", "GPT-5.6 Luna": "GPT-5.6"}

CONFUSION = re.compile(r"what do you mean|not sure what you|\bunclear\b|did you mean|i don't see|i do not see"
                       r"|can't find|cannot find|doesn't exist|does not exist|i think you meant")
CONTRADICTION = re.compile(r"that's not|that is not|\bactually,|\bincorrect\b|\bcorrection\b")
PRAISE = re.compile(r"great work|\bexcellent\b|\bamazing\b|\bbrilliant\b|thank you so much|\bperfect\b")
TOK = re.compile(r"[a-z0-9']+")
PROXIES = ["confusion", "contradiction", "miscomm", "relevance", "echo", "self_rep", "praise"]
# Direction in which a higher value means MORE incoherent.
INCOHERENT_IF = {"confusion": "higher", "contradiction": "higher", "miscomm": "higher", "relevance": "lower",
                 "echo": "higher", "self_rep": "higher", "praise": "higher"}
SCALES = ["addresses", "no_misunderstanding", "not_degenerate", "overall"]


def norm(s):
    return s.lower().replace("’", "'")


def lab_group(lab):
    return lab if lab in ("Anthropic", "OpenAI", "Google") else "other"


# --- exchanges ----------------------------------------------------------------

def build_exchanges(pool, subj, mentions, gi, meta):
    subj_ids = {id(m) for m in subj}
    by_room = collections.defaultdict(list)
    for m in pool:
        by_room[m["room"]].append(m)
    ex = []
    for room, seq in by_room.items():
        last_by = {}  # speaker -> index of their latest message in this room
        for j, b in enumerate(seq):
            if id(b) in subj_ids:
                named = mentions.find(b["text"], b["t"]) - {b["speaker"]}
                b["_named"] = named
                for a_name in named:
                    i = last_by.get(a_name)
                    if i is not None and b["t"] - seq[i]["t"] <= WINDOW and id(seq[i]) in subj_ids:
                        ex.append((seq[i], b, "mention", i, j))
                if j > 0:
                    a = seq[j - 1]
                    if (a["speaker"] != b["speaker"] and a["speaker"] not in named and id(a) in subj_ids
                            and b["t"] - a["t"] <= WINDOW):
                        ex.append((a, b, "adjacent", j - 1, j))
            last_by[b["speaker"]] = j
    return ex, by_room


def proxies(ex, subj, mentions):
    from sklearn.feature_extraction.text import TfidfVectorizer

    strip = lambda s: mentions.rx.sub(" ", s)  # noqa: E731  (names would inflate overlap)
    idx = {id(m): k for k, m in enumerate(subj)}
    vec = TfidfVectorizer(sublinear_tf=True, stop_words="english", min_df=2, max_features=200_000)
    X = vec.fit_transform([strip(m["text"]) for m in subj]).tocsr()
    ai = np.array([idx[id(a)] for a, *_ in ex])
    bi = np.array([idx[id(b)] for _, b, *_ in ex])
    relevance = np.asarray(X[ai].multiply(X[bi]).sum(1)).ravel()

    # self-repetition: B vs B's own previous 3 messages (any room)
    prev = {}
    hist = collections.defaultdict(list)
    for k, m in enumerate(subj):
        prev[k] = hist[m["speaker"]][-3:]
        hist[m["speaker"]].append(k)
    self_rep = np.zeros(len(ex))
    for n, k in enumerate(bi):
        p = prev[k]
        if p:
            self_rep[n] = (X[p] @ X[k].T).toarray().max()

    rows = []
    for n, (a, b, kind, i, j) in enumerate(ex):
        tb = norm(b["text"])
        ta = TOK.findall(norm(strip(a["text"])))
        tbk = TOK.findall(norm(strip(b["text"])))
        tri_a = set(zip(ta, ta[1:], ta[2:]))
        tri_b = set(zip(tbk, tbk[1:], tbk[2:]))
        conf = bool(CONFUSION.search(tb))
        contra = bool(CONTRADICTION.search(tb))
        rows.append({"confusion": int(conf), "contradiction": int(contra), "miscomm": int(conf or contra),
                     "relevance": float(relevance[n]),
                     "echo": len(tri_b & tri_a) / len(tri_b) if tri_b else 0.0,
                     "self_rep": float(self_rep[n]), "praise": int(bool(PRAISE.search(tb))),
                     "b_words": len(tb.split()), "a_words": len(a["text"].split())})
    return rows


# --- statistics -----------------------------------------------------------------

def replier_stat(y, same, rep, weights="n"):
    """Mean over repliers of mean(same) - mean(cross); repliers need MIN_SIDE on both sides."""
    R = rep.max() + 1
    ns = np.bincount(rep, weights=same, minlength=R)
    nc = np.bincount(rep, weights=1 - same, minlength=R)
    ys = np.bincount(rep, weights=y * same, minlength=R)
    yc = np.bincount(rep, weights=y * (1 - same), minlength=R)
    ok = (ns >= MIN_SIDE) & (nc >= MIN_SIDE)
    d = ys[ok] / ns[ok] - yc[ok] / nc[ok]
    if weights == "n":
        w = (ns + nc)[ok]
        return float(np.sum(d * w) / np.sum(w)), ok
    return float(d.mean()), ok


def cell_stat(y, same, cell):
    """Exchange-weighted mean of within-(replier x goal) same - cross differences."""
    C = cell.max() + 1
    ns = np.bincount(cell, weights=same, minlength=C)
    nc = np.bincount(cell, weights=1 - same, minlength=C)
    ys = np.bincount(cell, weights=y * same, minlength=C)
    yc = np.bincount(cell, weights=y * (1 - same), minlength=C)
    ok = (ns >= 3) & (nc >= 3)
    d = ys[ok] / ns[ok] - yc[ok] / nc[ok]
    w = (ns + nc)[ok]
    return float(np.sum(d * w) / np.sum(w)) if ok.any() else float("nan"), int(ok.sum())


def perm_within(same, strata, rng):
    order0 = np.lexsort((np.arange(len(same)), strata))
    order1 = np.lexsort((rng.random(len(same)), strata))
    out = np.empty_like(same)
    out[order0] = same[order1]
    return out


def analyse_proxies(df, rng):
    """df: dict of arrays. Returns per-proxy results for a subset. Same/cross counts per
    replier and per replier x goal cell are invariant under the within-stratum shuffle."""
    same = df["same"].astype(float)
    rep = df["rep"]
    strata = df["rep_goal"]
    Y = {p: df[p].astype(float) for p in PROXIES}
    obs = {p: (replier_stat(Y[p], same, rep, "n"), replier_stat(Y[p], same, rep, "u")[0],
               cell_stat(Y[p], same, strata)) for p in PROXIES}
    nulls = {p: ([], [], []) for p in PROXIES}
    for _ in range(N_PERM):
        s = perm_within(same, strata, rng)
        for p in PROXIES:
            nulls[p][0].append(replier_stat(Y[p], s, rep, "n")[0])
            nulls[p][1].append(replier_stat(Y[p], s, rep, "u")[0])
            nulls[p][2].append(cell_stat(Y[p], s, strata)[0])
    pp = lambda o, nl: float((np.sum(np.abs(np.asarray(nl)) >= abs(o)) + 1) / (len(nl) + 1))  # noqa: E731
    R = rep.max() + 1
    ns = np.bincount(rep, weights=same, minlength=R)
    nc = np.bincount(rep, weights=1 - same, minlength=R)
    res = {}
    for p in PROXIES:
        y = Y[p]
        (obs_w, ok), obs_u, (cell_d, n_cells) = obs[p]
        reps = np.flatnonzero(ok)
        ys = np.bincount(rep, weights=y * same, minlength=R)
        yc = np.bincount(rep, weights=y * (1 - same), minlength=R)
        d = ys[reps] / ns[reps] - yc[reps] / nc[reps]
        w = (ns + nc)[reps]
        boot_w, boot_u = [], []
        for _ in range(N_BOOT):
            k = rng.integers(0, len(reps), len(reps))
            boot_w.append(np.sum(d[k] * w[k]) / np.sum(w[k]))
            boot_u.append(d[k].mean())
        sm, cm = y[same == 1], y[same == 0]
        naive_d = float(sm.mean() - cm.mean()) if len(sm) and len(cm) else float("nan")
        naive_se = float(np.sqrt(sm.var() / max(len(sm), 1) + cm.var() / max(len(cm), 1)))
        res[p] = {
            "incoherent_if": INCOHERENT_IF[p],
            "mean_same": float(sm.mean()) if len(sm) else None, "mean_cross": float(cm.mean()) if len(cm) else None,
            "n_same": int(len(sm)), "n_cross": int(len(cm)), "sd": float(y.std()) or 1.0,
            "naive_diff": naive_d, "naive_ci95": [naive_d - 1.96 * naive_se, naive_d + 1.96 * naive_se],
            "within_replier_weighted": obs_w,
            "within_replier_weighted_ci95": [float(np.percentile(boot_w, 2.5)), float(np.percentile(boot_w, 97.5))],
            "within_replier_weighted_perm_p": pp(obs_w, nulls[p][0]),
            "within_replier_unweighted": obs_u,
            "within_replier_unweighted_ci95": [float(np.percentile(boot_u, 2.5)), float(np.percentile(boot_u, 97.5))],
            "within_replier_unweighted_perm_p": pp(obs_u, nulls[p][1]),
            "within_replier_goal_cells": cell_d, "n_cells": n_cells, "cells_perm_p": pp(cell_d, nulls[p][2]),
            "n_repliers": int(len(reps)),
            "per_replier_diff": {df["rep_names"][r]: float(dd) for r, dd in zip(reps, d)},
        }
    return res


# --- blinded judge items --------------------------------------------------------

LAB_WORDS = re.compile(
    r"(?i)\b(?:claude|chatgpt|gpt|gemini|opus|sonnet|haiku|fable|deepseek|grok|kimi|glm|muse spark|muse|anthropic|openai|google|"
    r"google deepmind|deepmind|moonshot|zhipu|xai|o3|o1|o4-mini)\b(?:[\s-]*[vV]?\d+(?:\.\d+)?)?(?:[\s-]*(?:pro|flash|sol|terra|luna|astra|mini))?")
# Anything containing a model/lab word (handles, URLs, file names: claudehaiku45, gpt5_1, Google Docs).
EMBEDDED = re.compile(r"(?i)[\w./@:-]*(?:claude|chatgpt|gpt|gemini|opus|sonnet|haiku|fable|deepseek|grok|kimi|"
                      r"glm|anthropic|openai|deepmind|google|muse)[\w./@-]*")
NICKNAMES = re.compile(r"\bDS-V[\d.]+\b")
EXTRA_FIRST = {"Luna": "GPT-5.6 Luna", "Terra": "GPT-5.6 Terra", "Sol": "GPT-5.6 Sol", "Astra": "GPT-6 Astra"}


class Blinder:
    def __init__(self, mentions, agent_names):
        self.m = mentions
        # Same alias table as VillageMentions, but also matches after "@" (handles).
        alts = "|".join(re.escape(a) for a in sorted(mentions.lookup, key=len, reverse=True))
        self.rx = re.compile(rf"(?<![\w./-])({alts})(?![\w]|[.-]\d)")

    def resolve(self, alias, t):
        name = self.m.lookup.get(alias)
        if isinstance(name, tuple):
            name = EXTRA_FIRST[alias]
        elif name is None and alias in WEAK:
            live = [c for c in WEAK[alias] if self.m._is_active(c, t)]
            name = live[0] if len(live) == 1 else None
        return name

    def blind(self, text, t, labels):
        def sub(mt):
            name = self.resolve(mt.group(1), t)
            if name is None:
                return "another agent"
            if name not in labels:
                labels[name] = f"Agent {len(labels) + 1}"
            return labels[name]
        out = self.rx.sub(sub, text)
        out = LAB_WORDS.sub("[model]", out)
        out = EMBEDDED.sub("[model]", out)
        out = NICKNAMES.sub("[model]", out)
        return out


def make_items(ex, rows, by_room, meta, mentions, agent_names):
    rng = random.Random(SEED)
    cells = collections.defaultdict(list)
    for n, ((a, b, kind, i, j), r) in enumerate(zip(ex, rows)):
        if r["b_words"] < 5 or r["a_words"] < 5:
            continue
        same = meta[a["speaker"]]["lab"] == meta[b["speaker"]]["lab"]
        cells[(same, lab_group(meta[b["speaker"]]["lab"]), kind)].append(n)
    groups = ["Anthropic", "OpenAI", "Google", "other"]
    target = 150
    per = {}
    # equal target per (same, group) = 150/8 ~ 19, split across strata; redistribute shortfall
    want = {(s, g): target // 8 for s in (True, False) for g in groups}
    avail = {(s, g): len(cells[(s, g, "mention")]) + len(cells[(s, g, "adjacent")]) for s, g in want}
    short = sum(max(0, want[k] - avail[k]) for k in want)
    for k in want:
        want[k] = min(want[k], avail[k])
    while short > 0:
        grew = False
        for k in sorted(want, key=lambda k: want[k]):
            if short and want[k] < avail[k]:
                want[k] += 1
                short -= 1
                grew = True
        if not grew:
            break
    extra = target - sum(want.values())
    for k in sorted(want):
        if extra and want[k] < avail[k]:
            want[k] += 1
            extra -= 1
    chosen = []
    for (s, g), k in want.items():
        m_pool, a_pool = cells[(s, g, "mention")][:], cells[(s, g, "adjacent")][:]
        rng.shuffle(m_pool)
        rng.shuffle(a_pool)
        km = min(len(m_pool), (k + 1) // 2)
        ka = min(len(a_pool), k - km)
        km = min(len(m_pool), k - ka)
        chosen += m_pool[:km] + a_pool[:ka]
        per[f"{'same' if s else 'cross'}|{g}"] = {"n": km + ka, "mention": km, "adjacent": ka}
    rng.shuffle(chosen)

    bl = Blinder(mentions, agent_names)
    items, key = [], []
    cap = lambda s: s if len(s) <= 1500 else s[:1500] + " [...truncated]"  # noqa: E731
    for k, n in enumerate(chosen):
        a, b, kind, i, j = ex[n]
        seq = by_room[a["room"]]
        labels = {}
        ctx = []

        def speaker_label(m):
            if not m["is_agent"]:
                return "Human"
            if m["speaker"] not in labels:
                labels[m["speaker"]] = f"Agent {len(labels) + 1}"
            return labels[m["speaker"]]
        for m in seq[max(0, i - 2):i]:
            ctx.append({"speaker": speaker_label(m), "text": cap(bl.blind(m["text"], m["t"], labels))})
        la = speaker_label(a)
        ta = cap(bl.blind(a["text"], a["t"], labels))
        lb = speaker_label(b)
        tb = cap(bl.blind(b["text"], b["t"], labels))
        item_id = f"x{k:03d}"
        items.append({"item_id": item_id, "context": ctx,
                      "message": {"speaker": la, "text": ta},
                      "messages_in_between": j - i - 1,
                      "reply": {"speaker": lb, "text": tb}})
        key.append({"item_id": item_id, "ex_idx": n, "A": a["speaker"], "B": b["speaker"],
                    "lab_A": meta[a["speaker"]]["lab"], "lab_B": meta[b["speaker"]]["lab"],
                    "same": int(meta[a["speaker"]]["lab"] == meta[b["speaker"]]["lab"]),
                    "replier_group": lab_group(meta[b["speaker"]]["lab"]), "stratum": kind,
                    "t_B": b["t"].isoformat(), "room": a["room"]})
    # 3 judges x 50, plus 7 overlap items handed to the next judge (21 overlap items)
    splits = [items[0:50], items[50:100], items[100:150]]
    assign = {}
    for jdx in range(3):
        own = splits[jdx]
        borrowed = splits[(jdx - 1) % 3][:7]
        js = own + borrowed
        random.Random(SEED + jdx).shuffle(js)
        with open(COH / f"judge_items_{jdx + 1}.jsonl", "w") as f:
            for it in js:
                f.write(json.dumps(it) + "\n")
        assign[jdx + 1] = [it["item_id"] for it in js]
    (COH / "judge_key.json").write_text(json.dumps({"key": key, "assignment": assign, "sampling": per}, indent=1))
    return per



# --- judged analysis ------------------------------------------------------------

GROUPS = ["Anthropic", "OpenAI", "Google", "other"]


def analyse_judged(out_rows, rng):
    files = sorted(COH.glob("judge_ratings_*.json"))
    if not files:
        return None
    K = json.loads((COH / "judge_key.json").read_text())
    key = {k["item_id"]: k for k in K["key"]}
    ratings = collections.defaultdict(dict)  # item -> judge -> scores
    for f in files:
        j = f.stem.split("_")[-1]
        for r in json.loads(f.read_text()):
            if r["item_id"] in key:
                ratings[r["item_id"]][j] = {s: float(r[s]) for s in SCALES}
    items = sorted(ratings)
    rows = []
    for it in items:
        k = key[it]
        js = ratings[it]
        ex = out_rows[k["ex_idx"]]
        rows.append({"item_id": it, "A": k["A"], "B": k["B"], "lab_A": k["lab_A"], "lab_B": k["lab_B"],
                     "same": k["same"], "replier_group": k["replier_group"], "stratum": k["stratum"],
                     "t_B": k["t_B"], "room": k["room"], "n_judges": len(js), "judges": "+".join(sorted(js)),
                     **{s: float(np.mean([v[s] for v in js.values()])) for s in SCALES},
                     "min_score": float(min(min(v.values()) for v in js.values())),
                     **{f"proxy_{p}": ex[p] for p in PROXIES}})
    with open(COH / "judged.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {COH / 'judged.csv'} ({len(rows)} items)")

    same = np.array([r["same"] for r in rows])
    grp = np.array([GROUPS.index(r["replier_group"]) for r in rows])
    cellid = grp * 2 + same
    res = {"n_items": len(rows), "n_ratings": sum(len(v) for v in ratings.values()),
           "cell_counts": {f"{GROUPS[g]}|{'same' if s else 'cross'}": int(np.sum((grp == g) & (same == s)))
                           for g in range(4) for s in (0, 1)}, "scales": {}}

    def balanced(y, s):
        ds = [y[(grp == g) & (s == 1)].mean() - y[(grp == g) & (s == 0)].mean()
              for g in range(4) if np.any((grp == g) & (s == 1)) and np.any((grp == g) & (s == 0))]
        return float(np.mean(ds))

    cells = [np.flatnonzero(cellid == c) for c in range(8)]
    measures = SCALES + ["flag_le2"]
    for s_name in measures:
        y = (np.array([r["min_score"] for r in rows]) <= 2).astype(float) if s_name == "flag_le2" \
            else np.array([r[s_name] for r in rows])
        pooled = float(y[same == 1].mean() - y[same == 0].mean())
        bal = balanced(y, same)
        bp, bb = [], []
        for _ in range(N_BOOT):
            idx = np.concatenate([rng.choice(c, len(c)) for c in cells if len(c)])
            yy, ss, gg = y[idx], same[idx], grp[idx]
            bp.append(yy[ss == 1].mean() - yy[ss == 0].mean())
            ds = [yy[(gg == g) & (ss == 1)].mean() - yy[(gg == g) & (ss == 0)].mean()
                  for g in range(4) if np.any((gg == g) & (ss == 1)) and np.any((gg == g) & (ss == 0))]
            bb.append(np.mean(ds))
        null = []
        for _ in range(N_PERM):
            sp = same.copy()
            for g in range(4):
                m = np.flatnonzero(grp == g)
                sp[m] = rng.permutation(sp[m])
            null.append(balanced(y, sp))
        per_group = {}
        for g in range(4):
            a, b = y[(grp == g) & (same == 1)], y[(grp == g) & (same == 0)]
            if len(a) and len(b):
                bs = [rng.choice(a, len(a)).mean() - rng.choice(b, len(b)).mean() for _ in range(N_BOOT)]
                per_group[GROUPS[g]] = {"same": float(a.mean()), "cross": float(b.mean()), "n_same": len(a),
                                        "n_cross": len(b), "diff": float(a.mean() - b.mean()),
                                        "ci95": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]}
        res["scales"][s_name] = {
            "mean_same": float(y[same == 1].mean()), "mean_cross": float(y[same == 0].mean()),
            "sd": float(y.std()) or 1.0,
            "pooled_diff": pooled, "pooled_ci95": [float(np.percentile(bp, 2.5)), float(np.percentile(bp, 97.5))],
            "balanced_diff": bal, "balanced_ci95": [float(np.percentile(bb, 2.5)), float(np.percentile(bb, 97.5))],
            "balanced_perm_p": float((np.sum(np.abs(null) >= abs(bal)) + 1) / (len(null) + 1)),
            "per_replier_group": per_group}

    # inter-judge agreement on overlap items
    from scipy.stats import spearmanr
    ov = [it for it in items if len(ratings[it]) >= 2]
    agree = {"n_overlap_items": len(ov)}
    for s_name in SCALES:
        a = np.array([list(ratings[it].values())[0][s_name] for it in ov])
        b = np.array([list(ratings[it].values())[1][s_name] for it in ov])
        agree[s_name] = {"exact": float(np.mean(a == b)), "within1": float(np.mean(np.abs(a - b) <= 1)),
                         "mean_abs_diff": float(np.mean(np.abs(a - b))),
                         "spearman": float(spearmanr(a, b).statistic) if np.ptp(a) and np.ptp(b) else None}
    res["agreement"] = agree
    # judge means per judge (calibration differences)
    res["judge_means"] = {j: {s: float(np.mean([ratings[it][j][s] for it in items if j in ratings[it]]))
                              for s in SCALES} for j in sorted({j for v in ratings.values() for j in v})}
    # do the proxies track the judges? (item level, Spearman)
    res["proxy_vs_judge_spearman"] = {
        p: {s: float(spearmanr([r[f"proxy_{p}"] for r in rows], [r[s] for r in rows]).statistic) for s in SCALES}
        for p in ("relevance", "echo", "self_rep", "praise", "miscomm")}
    return res


def plot(results, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    BLUE, GREY, DARK = "#2a78d6", "#a8a69c", "#1c3f6e"
    judged = results.get("judged")
    fig, axes = plt.subplots(1, 2 if judged else 1, figsize=(13, 5.6), squeeze=False)
    ax = axes[0, 0]
    P = results["proxies"]["all"]
    names = {"confusion": "Confusion / clarification marker", "contradiction": "Contradiction / correction marker",
             "miscomm": "Either miscommunication marker", "relevance": "Low topical relevance (TF-IDF, sign flipped)",
             "echo": "Echoing A (trigram overlap)", "self_rep": "Self-repetition (vs own last 3)",
             "praise": "Praise-loop marker"}
    for i, p in enumerate(PROXIES):
        d = P[p]
        sgn = -1 if INCOHERENT_IF[p] == "lower" else 1
        sd = d["sd"]
        y = len(PROXIES) - 1 - i
        nv = sgn * d["naive_diff"] / sd
        nlo, nhi = sorted(sgn * np.array(d["naive_ci95"]) / sd)
        wv = sgn * d["within_replier_weighted"] / sd
        wlo, whi = sorted(sgn * np.array(d["within_replier_weighted_ci95"]) / sd)
        ax.plot([nlo, nhi], [y + 0.17] * 2, color=GREY, lw=2, solid_capstyle="round")
        ax.scatter([nv], [y + 0.17], color=GREY, s=40, zorder=3, edgecolor="white", linewidth=1.5)
        ax.plot([wlo, whi], [y - 0.13] * 2, color=BLUE, lw=2, solid_capstyle="round")
        ax.scatter([wv], [y - 0.13], color=BLUE, s=48, zorder=3, edgecolor="white", linewidth=1.5)
        ax.annotate(f"p={d['within_replier_weighted_perm_p']:.3f}", (max(whi, nhi), y - 0.13), xytext=(6, -3),
                    textcoords="offset points", fontsize=8, color="#5f5e5a")
    ax.set_yticks(range(len(PROXIES)))
    ax.set_yticklabels([names[p] for p in PROXIES][::-1], fontsize=9)
    ax.axvline(0, color="#888780", lw=1)
    ax.set_xlabel("same-lab minus cross-lab, in SDs of the measure\n(> 0 = same-lab replies look MORE incoherent)",
                  fontsize=9)
    ax.set_title(f"Automatic proxies, {results['n_exchanges']:,} exchanges", fontsize=10, loc="left")
    h = [plt.Line2D([], [], color=GREY, marker="o", lw=2, label="naive pooled (style-confounded)"),
         plt.Line2D([], [], color=BLUE, marker="o", lw=2, label="within replier (weighted), cluster-bootstrap 95% CI")]
    ax.legend(handles=h, fontsize=8, frameon=False, loc="lower left", bbox_to_anchor=(0, -0.42))
    if judged:
        ax = axes[0, 1]
        J = judged["scales"]
        lab = {"addresses": "(a) Addresses the message", "no_misunderstanding": "(b) No misunderstanding",
               "not_degenerate": "(c) Not degenerate", "overall": "(d) Overall coherence",
               "flag_le2": "Any judge score <= 2 (share, sign kept)"}
        keys = SCALES + ["flag_le2"]
        for i, s in enumerate(keys):
            d = J[s]
            sgn = 1 if s == "flag_le2" else -1   # higher rating = more coherent
            y = len(keys) - 1 - i
            pv = sgn * d["pooled_diff"]
            plo, phi = sorted(sgn * np.array(d["pooled_ci95"]))
            bv = sgn * d["balanced_diff"]
            blo, bhi = sorted(sgn * np.array(d["balanced_ci95"]))
            ax.plot([plo, phi], [y + 0.17] * 2, color=GREY, lw=2, solid_capstyle="round")
            ax.scatter([pv], [y + 0.17], color=GREY, s=40, zorder=3, edgecolor="white", linewidth=1.5)
            ax.plot([blo, bhi], [y - 0.13] * 2, color=DARK, lw=2, solid_capstyle="round")
            ax.scatter([bv], [y - 0.13], color=DARK, s=48, zorder=3, edgecolor="white", linewidth=1.5)
            ax.annotate(f"p={d['balanced_perm_p']:.3f}", (max(bhi, phi), y - 0.13), xytext=(6, -3),
                        textcoords="offset points", fontsize=8, color="#5f5e5a")
        ax.set_yticks(range(len(keys)))
        ax.set_yticklabels([lab[s] for s in keys][::-1], fontsize=9)
        ax.axvline(0, color="#888780", lw=1)
        ax.set_xlabel("cross-lab minus same-lab rating, 1-5 scale (flag row: same minus cross share)\n"
                      "(> 0 = same-lab replies judged MORE incoherent)", fontsize=9)
        ax.set_title(f"Blinded LLM judges, {judged['n_items']} exchanges", fontsize=10, loc="left")
        h = [plt.Line2D([], [], color=GREY, marker="o", lw=2, label="pooled"),
             plt.Line2D([], [], color=DARK, marker="o", lw=2, label="replier-lab balanced, stratified-bootstrap 95% CI")]
        ax.legend(handles=h, fontsize=8, frameon=False, loc="lower left", bbox_to_anchor=(0, -0.42))
    for a in axes.ravel():
        a.grid(axis="x", color="#eeede6", lw=0.6)
        a.set_axisbelow(True)
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
    fig.suptitle("Is same-lab agent communication more incoherent than cross-lab? (AI Village)", fontsize=11, x=0.01,
                 ha="left")
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight")
    print(f"wrote {path}")


# --- main -----------------------------------------------------------------------

def main():
    meta = load_metadata()
    agents = load_agents()
    msgs = load_chat(agents)
    mentions = VillageMentions(active_windows(msgs))
    gi = GoalIndex(load_goals())
    pool, subj, subjects, log = clean(msgs)
    ex, by_room = build_exchanges(pool, subj, mentions, gi, meta)
    print(f"{len(ex)} exchanges from {len(subj)} subject messages")
    rows = proxies(ex, subj, mentions)

    rep_names = sorted({b["speaker"] for _, b, *_ in ex})
    rep_idx = {r: k for k, r in enumerate(rep_names)}
    out_rows, rg_keys = [], {}
    for (a, b, kind, i, j), r in zip(ex, rows):
        g = gi.at(b["t"])
        la, lb = meta[a["speaker"]]["lab"], meta[b["speaker"]]["lab"]
        rgk = (b["speaker"], g["idx"] if g else -1)
        rg_keys.setdefault(rgk, len(rg_keys))
        out_rows.append({"t_A": a["t"].isoformat(), "t_B": b["t"].isoformat(), "room": a["room"],
                         "goal_idx": g["idx"] if g else -1, "stratum": kind,
                         "A": a["speaker"], "B": b["speaker"], "lab_A": la, "lab_B": lb,
                         "same_lab": int(la == lb),
                         "same_line": int(SAME_LINE.get(a["speaker"], a["speaker"]) == SAME_LINE.get(b["speaker"], "_")),
                         "gap_s": (b["t"] - a["t"]).total_seconds(), **r})
    with open(COH / "exchanges.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0]))
        w.writeheader()
        w.writerows(out_rows)
    print(f"wrote {COH / 'exchanges.csv'}")

    def arrays(sel):
        d = {p: np.array([r[p] for r, s in zip(out_rows, sel) if s], float) for p in PROXIES}
        d["same"] = np.array([r["same_lab"] for r, s in zip(out_rows, sel) if s])
        d["rep"] = np.array([rep_idx[r["B"]] for r, s in zip(out_rows, sel) if s])
        d["rep_goal"] = np.array([rg_keys[(r["B"], r["goal_idx"])] for r, s in zip(out_rows, sel) if s])
        d["rep_names"] = rep_names
        return d

    rng = np.random.default_rng(SEED)
    results = {"preregistered": __doc__.split("Run:")[0].strip(), "cleaning": log, "n_exchanges": len(out_rows),
               "pair_counts": {}, "proxies": {}}
    pc = collections.Counter((r["stratum"], r["lab_A"], r["lab_B"]) for r in out_rows)
    results["pair_counts"] = {f"{s}|{a}->{b}": n for (s, a, b), n in sorted(pc.items(), key=lambda x: -x[1])}
    results["same_line_exchanges"] = sum(r["same_line"] for r in out_rows)
    subsets = {
        "all": [True] * len(out_rows),
        "mention": [r["stratum"] == "mention" for r in out_rows],
        "adjacent": [r["stratum"] == "adjacent" for r in out_rows],
        "replier_Anthropic": [r["lab_B"] == "Anthropic" for r in out_rows],
        "replier_nonAnthropic": [r["lab_B"] != "Anthropic" for r in out_rows],
        # Post hoc check on the GPT-5.6 trio: same exact line vs any cross-lab partner
        "replier_GPT56_line_vs_cross": [r["B"] in SAME_LINE and (r["same_line"] or not r["same_lab"]) for r in out_rows],
    }
    cached = COH / "results_auto.json"
    if "--judged-only" in sys.argv and cached.exists():
        results = json.loads(cached.read_text())
        subsets = {}
    for name, sel in subsets.items():
        d = arrays(sel)
        if name == "replier_GPT56_line_vs_cross":
            d["same"] = np.array([r["same_line"] for r, s in zip(out_rows, sel) if s])
        results["proxies"][name] = analyse_proxies(d, rng)
        r0 = results["proxies"][name]
        print(f"[{name}] " + "  ".join(f"{p}: {r0[p]['within_replier_weighted']:+.4f} (p={r0[p]['within_replier_weighted_perm_p']:.3f}, naive {r0[p]['naive_diff']:+.4f})" for p in PROXIES))
    if subsets:
        (COH / "results_auto.json").write_text(json.dumps(results, indent=1, default=str))

    if not (COH / "judge_key.json").exists() or "--resample" in sys.argv:
        agent_names = sorted({m["speaker"] for m in pool})
        per = make_items(ex, rows, by_room, meta, mentions, agent_names)
        print("judge sample:", per)
    results["judge_sampling"] = json.loads((COH / "judge_key.json").read_text())["sampling"]
    results["judged"] = analyse_judged(out_rows, rng)
    (COH / "results.json").write_text(json.dumps(results, indent=1, default=str))
    print(f"wrote {COH / 'results.json'}")
    plot(results, COH / "same_vs_cross.png")
    return results


if __name__ == "__main__":
    main()
