"""How well do inferred subtasks match the collusion.wiki publishers' page_family labels?

    uv run --directory swarm_mcp swarm-mcp add --adapter wiki data/collusion-wiki
    uv run --directory swarm_mcp python examples/wiki_eval.py

page_family is the publishers' own heuristic task label per page (e.g. 'oecd-equity',
'datausa-clothing-workforce'), not ground truth, and it was partly derived from page names and bodies,
which the title/code signals also read. So this is a consistency check, not an accuracy score. Only
sessions whose pages carry one specific task family are scored; generic or unresolved families
(relay/coordination, URL lists, probes, unclassified) are left out.

Baselines: 'same page' (sessions grouped by the page they edited most: what the files signal sees most
directly) and 'shuffled' (the combined clusters with members permuted: what chance agreement looks like).
"""

from __future__ import annotations

import collections
import os
import random
from pathlib import Path

import numpy as np

from swarm_mcp.config import Config
from swarm_mcp.modules.subtasks.infer import LEVELS, METHODS, _ari, infer
from swarm_mcp.modules.subtasks.sources import load_corpus
from swarm_mcp.scope import db

GENERIC = {
    "source-cache-url-list",
    "relay-coordination",
    "source-or-unclassified",
    "off_store_unclassified",
    "loop-chain-infrastructure",
    "probe-test",
    "unknown",
}


def purity(pred: list[int], gold: list[str]) -> float:
    by = collections.defaultdict(collections.Counter)
    for p, g in zip(pred, gold, strict=True):
        by[p][g] += 1
    return sum(c.most_common(1)[0][1] for c in by.values()) / len(gold)


def completeness(pred: list[int], gold: list[str]) -> float:
    """Share of each family that lands in its single biggest cluster (1 = never split)."""
    by = collections.defaultdict(collections.Counter)
    for p, g in zip(pred, gold, strict=True):
        by[g][p] += 1
    return sum(c.most_common(1)[0][1] for c in by.values()) / len(gold)


def main() -> None:
    os.environ.setdefault("SWARM_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data"))
    name = os.environ.get("CORPUS", "collusion-wiki")
    with db.connect(Config.from_env().store_path) as s:
        c = load_corpus(s, name)
    inf = infer(name, c.units, c.chat, dup_min=c.dup_min, artifact_meta=c.artifact_meta)
    keep, gold = [], []
    for i, u in enumerate(inf.units):
        fams = {f for f in u.tags if f not in GENERIC}
        if len(fams) == 1:
            keep.append(i)
            gold.append(fams.pop())
    print(
        f"{name}: {len(inf.units)} sessions; scored {len(keep)} with one specific page_family "
        f"({len(set(gold))} families)\n"
    )
    print(f"{'method':10s} {'level':7s} {'ARI':>6s} {'purity':>7s} {'complete':>9s} {'clusters':>9s}")
    rows = []
    for m in METHODS:
        for lvl in LEVELS:
            lab = inf.label_of[m][lvl][keep]
            rows.append(
                (
                    m,
                    lvl,
                    _ari(lab, np.array([hash(g) for g in gold])),
                    purity(list(lab), gold),
                    completeness(list(lab), gold),
                    len(set(lab.tolist())),
                )
            )
    main_page = []
    for i in keep:
        pages = collections.Counter(a.changes[0].artifact for a in inf.units[i].actions if a.changes)
        main_page.append(hash(pages.most_common(1)[0][0]))
    rows.append(
        (
            "same page",
            "-",
            _ari(np.array(main_page), np.array([hash(g) for g in gold])),
            purity(main_page, gold),
            completeness(main_page, gold),
            len(set(main_page)),
        )
    )
    shuffled = list(inf.label_of["combined"]["medium"][keep])
    random.Random(0).shuffle(shuffled)
    rows.append(
        (
            "shuffled",
            "medium",
            _ari(np.array(shuffled), np.array([hash(g) for g in gold])),
            purity(shuffled, gold),
            completeness(shuffled, gold),
            len(set(shuffled)),
        )
    )
    for m, lvl, a, p, c, n in rows:
        print(f"{m:10s} {lvl:7s} {a:6.2f} {p:7.2f} {c:9.2f} {n:9d}")


if __name__ == "__main__":
    main()
