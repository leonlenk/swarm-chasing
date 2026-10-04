"""Bundle out/*.json into one self-contained page: out/village_idea_flow.html.

The page is assembled by pagekit: the shared paper style (paper.css), PaperKit (figure export)
and a vendored d3 are inlined, so it opens offline from file:// and makes no network requests.
"""

import collections
import datetime as dt
import json
import statistics

import pagekit
from common import OUT, ROOT

GROUPS = ["Anthropic", "OpenAI", "Google", "Other labs", "Human"]
TYPES = ["collaborative", "competitive", "individual", "free"]


def group(family):
    return family if family in ("Anthropic", "OpenAI", "Google", "Human") else "Other labs"


INPUTS = {"cooperation.json": "cooperation.py", "ideas.json": "ideas.py", "memories.json": "memories.py"}


def load(name):
    return json.loads((OUT / name).read_text())


def missing_inputs():
    """'run python3 <script>' lines for each input JSON not yet written to out/."""
    return [f"  out/{n} is missing: run `python3 {s}`" for n, s in INPUTS.items() if not (OUT / n).exists()]


def main():
    miss = missing_inputs()
    if miss:
        raise SystemExit("build_viz needs the analysis outputs first (from village_tools/):\n" + "\n".join(miss))
    coop, ideas, mem = load("cooperation.json"), load("ideas.json"), load("memories.json")
    goal_keys = ("idx", "goal", "type", "start", "end", "msgs", "human_msgs", "agents", "mention_rate", "reciprocity",
                 "density", "hub", "hub_share", "homophily", "we_share", "requests", "division_of_labour",
                 "verification", "gratitude", "competition")
    goals = [{k: g[k] for k in goal_keys} for g in coop["goals"]]

    # Goal-type summary: each goal counts once, so a long goal doesn't swamp the type.
    metrics = ("mention_rate", "reciprocity", "we_share", "division_of_labour", "requests", "competition")
    type_summary = []
    for t in TYPES:
        gs = [g for g in goals if g["type"] == t]
        type_summary.append({"type": t, "goals": len(gs),
                             **{m: statistics.mean(g[m] for g in gs if g[m] is not None) for m in metrics}})

    memory_uptake = {term: len(v) for term, v in mem["uptake"].items()}

    def term(r):
        return {"term": r["term"], "origin": r["origin"], "og": group(r["origin_family"]), "t0": r["origin_time"],
                "room": r["room"], "docs": r["docs"], "burst": r["burst"], "present": r["present"],
                "mem": memory_uptake.get(r["term"], 0), "weekly": r["weekly"],
                "adopters": [{"a": a["agent"], "g": group(a["family"]), "t": a["t"], "n": a["uses"],
                              "nc": a["newcomer"]} for a in r["adopters"]]}

    flow = collections.Counter()
    for f in ideas["flow"]:
        flow[(group(f["from"]), group(f["to"]))] += f["n"]

    c = ideas["counts"]
    payload = {
        "meta": {
            "start": coop["weekly"][0]["week"], "end": coop["weekly"][-1]["week"],
            "data_start": min(a["first"] for a in coop["agents"]), "data_end": max(a["last"] for a in coop["agents"]),
            "agent_msgs": coop["overall"]["msgs"], "human_msgs": coop["overall"]["human_msgs"],
            "agents": len(coop["agents"]), "goals": len(goals),
            "mention_rate": coop["overall"]["mention_rate"],
            "generated": dt.date.today().isoformat(),
            "params": ideas["params"],
        },
        "groups": GROUPS,
        "overall": {k: v for k, v in coop["overall"].items()},
        "weekly": coop["weekly"],
        "goals": goals,
        "typeSummary": type_summary,
        "ideas": {
            "counts": c,
            "newcomerShare": c["newcomer_adoptions"] / c["adoptions"],
            "durable": [term(r) for r in ideas["durable"]],
            "episodic": [term(r) for r in ideas["episodic"]],
            "influence": [{**x, "g": group(x["family"])} for x in ideas["influence"][:12]],
            "flow": [{"from": a, "to": b, "n": n} for (a, b), n in flow.items()],
            "speed": ideas["speed"],
            "adoptionByQuarter": ideas["adoption_by_quarter"],
            "humanTop": ideas["human_top"][:10],
        },
        "memory": {"weekly": mem["weekly"]},
    }
    # JSON inside <script type="application/json">: escape <, > and & so no data can close the tag
    data = (json.dumps(payload, separators=(",", ":"))
            .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e"))
    html = pagekit.build((ROOT / "viz_template.html").read_text(), {"__DATA__": data})
    path = OUT / "village_idea_flow.html"
    path.write_text(html)
    print(f"wrote {path} ({path.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
