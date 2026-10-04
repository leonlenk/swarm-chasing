"""The AI Village Idea Flow page (build_viz.py) on tiny synthetic inputs: no dataset text.

Checks that the page is self-contained (shared paper style, PaperKit and d3 inlined; no network
references) and that data cannot break out of the JSON <script> element.
Run from the repo root: uv run --no-project --with pytest --with jsonschema pytest village_tools/swarmtrace/tests
"""

import json
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # village_tools/, for build_viz and pagekit

import build_viz  # noqa: E402

INJECTION = "</script><script>alert(1)</script>"


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self.scripts: list[dict] = []
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        self.refs += [v for k, v in attrs if k in ("src", "href") and v]
        if tag == "script":
            self._cur = {"attrs": dict(attrs), "text": ""}
            self.scripts.append(self._cur)

    def handle_endtag(self, tag):
        if tag == "script":
            self._cur = None

    def handle_data(self, data):
        if self._cur is not None:
            self._cur["text"] += data


def _term(name, origin, fam, t0, adopters):
    return {"term": name, "origin": origin, "origin_family": fam, "origin_time": t0, "room": "general", "docs": 40,
            "burst": 0.3, "present": 3, "weekly": [["2025-04-07", 5], ["2025-04-14", 9]],
            "adopters": [{"agent": a, "family": f, "t": t, "room": "general", "uses": 3, "newcomer": nc} for a, f, t, nc in adopters]}


def _inputs(out: Path) -> None:
    week = {"msgs": 120, "human_msgs": 4, "agents": 3, "mention_rate": 0.4, "density": 0.5, "reciprocity": 0.8,
            "partners_per_agent": 2, "hub": "Agent A", "hub_share": 0.5, "homophily": 0.0, "we_share": 0.3,
            "requests": 5.0, "division_of_labour": 3.0, "verification": 9.0, "gratitude": 4.0, "competition": 1.0}
    goal = {k: v for k, v in week.items() if k != "partners_per_agent"}
    goals = [dict(goal, idx=i, goal=(INJECTION if i == 0 else f"Synthetic goal {i}"), type=t,
                  start=f"2025-04-{2 + 7 * i:02d}", end=None if i == 3 else f"2025-04-{9 + 7 * i:02d}")
             for i, t in enumerate(["collaborative", "competitive", "individual", "free"])]
    coop = {
        "agents": [{"name": "Agent A", "family": "Anthropic", "first": "2025-04-02", "last": "2025-04-30"},
                   {"name": "Agent B", "family": "OpenAI", "first": "2025-04-02", "last": "2025-04-30"},
                   {"name": "Agent C", "family": "Google", "first": "2025-04-10", "last": "2025-04-30"}],
        "overall": {"msgs": 480, "human_msgs": 16, "mention_rate": 0.4, "homophily": 0.001},
        "weekly": [dict(week, week=w) for w in ("2025-03-31", "2025-04-07", "2025-04-14", "2025-04-21", "2025-04-28")],
        "goals": goals,
    }
    adopters = [("Agent B", "OpenAI", "2025-04-05 18:00:00", False), ("Agent C", "Google", "2025-04-20 18:00:00", True)]
    ideas = {
        "params": {"min_docs": 20, "min_docs_speed": 5, "novelty_days": 21, "min_uses_to_adopt": 2, "reach_days": 14, "min_lift": 30},
        "counts": {"candidates": 10, "listed": 2, "seeded_adopted": 0, "human_origin": 0, "adoptions": 4, "newcomer_adoptions": 2},
        "durable": [_term(INJECTION, "Agent A", "Anthropic", "2025-04-03 18:00:00", adopters)],
        "episodic": [_term("synthword", "Agent B", "OpenAI", "2025-04-04 18:00:00", adopters)],
        "human_top": [], "influence": [{"agent": "Agent A", "family": "Anthropic", "coined": 2, "adoptions": 4}],
        "flow": [{"from": "Anthropic", "to": "OpenAI", "n": 2}, {"from": "OpenAI", "to": "Google", "n": 2}],
        "adoption_by_quarter": [{"quarter": "2025-Q2", "adoptions": 4, "newcomer": 2}],
        "speed": [{"quarter": "2025-Q2", "terms": 2, "mean_reach": 0.5, "share_any": 0.5, "mean_present": 3.0}],
    }
    mem = {"uptake": {}, "weekly": [{"week": w, "agent_days": 10, "named": 2.0, "named_share": 0.9, "median_chars": 5000.0,
                                     "shared": 0.01} for w in ("2025-03-31", "2025-04-07")]}
    for name, obj in (("cooperation.json", coop), ("ideas.json", ideas), ("memories.json", mem)):
        (out / name).write_text(json.dumps(obj))


def _build(tmp_path, monkeypatch) -> tuple[str, _Page]:
    _inputs(tmp_path)
    monkeypatch.setattr(build_viz, "OUT", tmp_path)
    build_viz.main()
    html = (tmp_path / "village_idea_flow.html").read_text()
    p = _Page()
    p.feed(html)
    return html, p


def test_page_is_self_contained(tmp_path, monkeypatch):
    html, p = _build(tmp_path, monkeypatch)
    assert not [r for r in p.refs if r.startswith(("http:", "https:", "//"))], p.refs
    assert all(s["attrs"].get("src") is None for s in p.scripts)
    assert "fonts.googleapis" not in html and "cdnjs" not in html
    assert "d3js.org v7.9.0" in html and "window.PaperKit" in html and "--oi-vermillion" in html
    assert "/*__" not in html and "__DATA__" not in html  # every slot was filled
    assert "Figure 9:" in html and "Time axis" in html


def test_payload_cannot_close_its_script(tmp_path, monkeypatch):
    html, p = _build(tmp_path, monkeypatch)
    assert INJECTION not in html
    data = [s for s in p.scripts if s["attrs"].get("type") == "application/json"]
    assert len(data) == 1 and html.count("</script>") == len(p.scripts)
    payload = json.loads(data[0]["text"])
    assert payload["goals"][0]["goal"] == INJECTION  # intact once decoded
    assert payload["ideas"]["durable"][0]["term"] == INJECTION
    assert "graphs" not in payload  # the network figure was cut; its data no longer ships
