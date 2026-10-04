"""The idea-spread viewer page (build_trace_viz.py) on a tiny synthetic trace: no dataset text.

Run from the repo root: uv run --no-project --with pytest --with jsonschema pytest village_tools/swarmtrace/tests
"""

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

import build_trace_viz as btv

INJECTION = "</script><script>alert(1)</script>"


class _Page(HTMLParser):
    """Collects every start tag's src/href and each <script> element (attributes and text)."""

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


def toy_trace() -> dict:
    return {
        "version": 0, "id": "toy-idea", "title": "Toy idea", "kind": "belief",
        "statement": "Bots believe the kettle is sentient.", "source": "Hand-written test fixture.",
        "start": "2031-01-01T00:00:00Z", "end": "2031-01-10T00:00:00Z",
        "agents": [{"name": "alpha", "lab": "LabA", "group": None, "joined": None, "left": None},
                   {"name": "beta", "lab": "LabB", "group": "newcomer", "joined": None, "left": None}],
        "events": [{"id": "e1", "t": "2031-01-02T10:00:00Z", "agent": "alpha", "channel": "chat", "stance": "originates",
                    "conf": 3, "room": "general", "snippet": INJECTION + " write to kettle.owner@example.org & <b>x</b>"},
                   {"id": "e2", "t": "2031-01-03T10:00:00Z", "agent": "beta", "channel": "memory", "stance": "rejects",
                    "conf": 2, "room": None, "snippet": "the kettle is just a kettle"}],
        "exposures": [{"t": "2031-01-02T10:00:00Z", "agent": "beta", "source": "alpha", "via": "room", "event": "e1"}],
        "adoptions": [], "edges": [], "persistence": [],
        "annotations": [{"t": "2031-01-05T00:00:00Z", "label": "Operator note", "kind": "intervention"}],
        "quotes": [{"t": "2031-01-02T10:00:00Z", "agent": "alpha", "text": "The kettle hums back.", "note": None}],
        "metrics": {"Agents": 2},
    }


def build(tmp_path: Path, *extra: str) -> tuple[str, dict, _Page]:
    src = tmp_path / "toy.json"
    src.write_text(json.dumps(toy_trace()))
    out = tmp_path / "page.html"
    assert btv.main([str(src), "-o", str(out), *extra]) == 0
    html = out.read_text(encoding="utf-8")
    page = _Page()
    page.feed(html)
    data = [s for s in page.scripts if s["attrs"].get("id") == "trace-data"]
    assert len(data) == 1
    return html, json.loads(data[0]["text"]), page


def test_page_is_self_contained(tmp_path):
    html, payload, page = build(tmp_path)
    assert page.refs == []  # no stylesheet links, no script src, no web fonts
    assert all("src" not in s["attrs"] for s in page.scripts)
    assert "fonts.googleapis" not in html and "cdnjs.cloudflare.com" not in html
    # the shared paper style, PaperKit and d3 are inlined; every template token was filled
    assert "--oi-vermillion" in html and "window.PaperKit" in html and "d3js.org v7.9.0" in html
    for tok in ("/*__PAPER_CSS__*/", "/*__PAPERKIT_JS__*/", "/*__D3_JS__*/", "__TRACE_PAYLOAD__"):
        assert tok not in html
    assert [t["id"] for t in payload["traces"]] == ["toy-idea"]


def test_snippet_cannot_close_the_script_tag(tmp_path):
    html, payload, page = build(tmp_path)
    assert INJECTION not in html and "<b>x</b>" not in html
    # exactly one closing tag per script element: data, d3, PaperKit, the page script
    assert html.count("</script>") == len(page.scripts) == 4
    snip = payload["traces"][0]["events"][0]["snippet"]
    assert snip.startswith(INJECTION)  # intact once JSON-decoded; the page only writes it with textContent


def test_emails_in_snippets_are_redacted(tmp_path):
    html, payload, _ = build(tmp_path)
    assert "kettle.owner@example.org" not in html
    assert "[email]" in payload["traces"][0]["events"][0]["snippet"]
    html, payload, _ = build(tmp_path, "--keep-email-domain", "example.org")
    assert "kettle.owner@example.org" in payload["traces"][0]["events"][0]["snippet"]


def test_day_spec_only_when_given(tmp_path):
    _, payload, _ = build(tmp_path)  # explicit inputs: off unless --day-one is passed
    assert payload["days"] is None
    _, payload, _ = build(tmp_path, "--day-one", "2031-01-01")
    assert payload["days"] == {"day_one": "2031-01-01", "tz": "America/Los_Angeles"}
    _, payload, _ = build(tmp_path, "--day-one", "2031-01-01", "--day-tz", "UTC")
    assert payload["days"] == {"day_one": "2031-01-01", "tz": "UTC"}
    _, payload, _ = build(tmp_path, "--day-one", "off")
    assert payload["days"] is None


def test_day_spec_defaults():
    # the bundled (no-input) build numbers AI Village days: day 1 = 2025-04-02, Pacific time
    assert btv.day_spec(None, btv.AI_VILLAGE_TZ, default_on=True) == {"day_one": "2025-04-02", "tz": "America/Los_Angeles"}
    assert btv.day_spec(None, btv.AI_VILLAGE_TZ, default_on=False) is None
    with pytest.raises(ValueError):
        btv.day_spec("April 2nd", "UTC", default_on=False)
