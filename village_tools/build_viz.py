"""Bundle out/*.json into one self-contained page: out/village_idea_flow.html."""

import collections
import datetime as dt
import json
import statistics

from common import OUT, ROOT

GROUPS = ["Anthropic", "OpenAI", "Google", "Other labs", "Human"]
TYPES = ["collaborative", "competitive", "individual", "free"]


def group(family):
    return family if family in ("Anthropic", "OpenAI", "Google", "Human") else "Other labs"


def load(name):
    return json.loads((OUT / name).read_text())


def main():
    coop, ideas, mem = load("cooperation.json"), load("ideas.json"), load("memories.json")
    agent_group = {a["name"]: group(a["family"]) for a in coop["agents"]}

    def graph(g):
        return {"nodes": [{"id": n["id"], "g": agent_group[n["id"]], "msgs": n["msgs"]} for n in g["nodes"]],
                "edges": [[e["s"], e["t"], e["w"]] for e in g["edges"]]}

    goal_keys = ("idx", "goal", "type", "start", "end", "msgs", "human_msgs", "agents", "mention_rate", "reciprocity",
                 "density", "hub", "hub_share", "homophily", "we_share", "requests", "division_of_labour",
                 "verification", "gratitude", "competition")
    goals = [{k: g[k] for k in goal_keys} for g in coop["goals"]]
    graphs = {"all": graph(coop["overall_graph"])} | {str(g["idx"]): graph(g["graph"]) for g in coop["goals"]}

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
            "agent_msgs": coop["overall"]["msgs"], "human_msgs": coop["overall"]["human_msgs"],
            "agents": len(coop["agents"]), "goals": len(goals),
            "mention_rate": coop["overall"]["mention_rate"],
            "generated": dt.date.today().isoformat(),
            "params": ideas["params"],
        },
        "groups": GROUPS,
        "agentGroup": agent_group,
        "overall": {k: v for k, v in coop["overall"].items()},
        "weekly": coop["weekly"],
        "goals": goals,
        "graphs": graphs,
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
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = (ROOT / "viz_template.html").read_text().replace("__DATA__", data)
    path = OUT / "village_idea_flow.html"
    path.write_text(html)
    print(f"wrote {path} ({path.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
