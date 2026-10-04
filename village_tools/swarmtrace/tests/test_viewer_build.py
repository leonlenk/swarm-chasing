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


def _malformed(tmp_path: Path) -> dict[str, Path]:
    """One file per malformed case the build used to crash on, keyed by a label."""
    def variant(**changes):
        t = toy_trace()
        for k, v in changes.items():
            t[k] = v
        return t
    cases = {
        "agent-name-list": variant(agents=[{"name": ["alpha"], "lab": "LabA", "joined": None, "left": None}]),
        "agent-name-dict": variant(agents=[{"name": {"n": "alpha"}, "lab": "LabA", "joined": None, "left": None}]),
        "kind-list": variant(kind=["belief"]),
        "kind-dict": variant(kind={"k": "belief"}),
        "event-agent-list": variant(events=[dict(toy_trace()["events"][0], agent=["alpha"])]),
        "agents-dict": variant(agents={"alpha": 1}),
        "agents-int": variant(agents=7),
        "events-int": variant(events=7),
        "events-strings": variant(events=["e1"]),
    }
    out = {}
    for label, t in cases.items():
        out[label] = tmp_path / f"{label}.json"
        out[label].write_text(json.dumps(t))
    out["bad-utf8"] = tmp_path / "bad-utf8.json"
    out["bad-utf8"].write_bytes(b'{"version": 0, "id": "\xff\xfe"}')
    out["nan"] = tmp_path / "nan.json"
    out["nan"].write_text(json.dumps(variant(metrics={"Agents": float("nan")})))
    return out


def _good(tmp_path: Path, tid="toy-idea") -> Path:
    t = toy_trace()
    t["id"] = tid
    p = tmp_path / f"{tid}.json"
    p.write_text(json.dumps(t))
    return p


def _payload(out: Path) -> dict:
    page = _Page()
    page.feed(out.read_text(encoding="utf-8"))
    return json.loads(next(s for s in page.scripts if s["attrs"].get("id") == "trace-data")["text"])


def test_malformed_traces_are_skipped_with_a_reason(tmp_path, capsys):
    bad = _malformed(tmp_path)
    good = _good(tmp_path)
    out = tmp_path / "page.html"
    assert btv.main([str(good), *map(str, bad.values()), "-o", str(out)]) == 0
    assert [t["id"] for t in _payload(out)["traces"]] == ["toy-idea"]
    err = capsys.readouterr().err
    for label, path in bad.items():
        assert any(line.startswith("skip:") and path.name in line for line in err.splitlines()), label


def test_malformed_index_files_are_skipped(tmp_path, capsys):
    (tmp_path / "ok").mkdir()
    good = _good(tmp_path / "ok", "toy-ok")
    (tmp_path / "ok" / "index.json").write_text(json.dumps({"traces": [{"file": good.name}]}))
    for name, idx in {"top-list": [good.name], "strings": {"traces": [good.name]}, "file-list": {"traces": [{"file": [1]}]},
                      "traces-dict": {"traces": {"a": 1}}}.items():
        d = tmp_path / name
        d.mkdir()
        (d / "index.json").write_text(json.dumps(idx))
    (tmp_path / "bad-utf8").mkdir()
    (tmp_path / "bad-utf8" / "index.json").write_bytes(b"\xff\xfe{}")
    out = tmp_path / "page.html"
    dirs = ["ok", "top-list", "strings", "file-list", "traces-dict", "bad-utf8"]
    assert btv.main([*(str(tmp_path / d) for d in dirs), "-o", str(out)]) == 0
    assert [t["id"] for t in _payload(out)["traces"]] == ["toy-ok"]
    err = capsys.readouterr().err
    for d in dirs[1:]:
        assert f"{tmp_path / d / 'index.json'}" in err, d


def test_duplicate_trace_ids_are_skipped(tmp_path, capsys):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, b = _good(tmp_path / "a"), _good(tmp_path / "b")
    out = tmp_path / "page.html"
    assert btv.main([str(a), str(b), "-o", str(out)]) == 0
    assert len(_payload(out)["traces"]) == 1
    assert "already loaded" in capsys.readouterr().err


def test_only_malformed_inputs_fail_cleanly(tmp_path, capsys):
    bad = _malformed(tmp_path)
    assert btv.main([*map(str, bad.values()), "-o", str(tmp_path / "page.html")]) == 1
    assert "No valid traces found" in capsys.readouterr().err


def test_redaction_uses_swarmtrace_scrub(tmp_path):
    t = toy_trace()
    t["events"][0]["snippet"] = "連絡はbob@example.comまで, ops@agentvillage.org, call 555-123-4567"
    t["quotes"][0]["text"] = "メールcat@keep.example.org確認"
    src = tmp_path / "toy.json"
    src.write_text(json.dumps(t))
    out = tmp_path / "page.html"
    assert btv.main([str(src), "-o", str(out), "--keep-email-domain", "keep.example.org"]) == 0
    tr = _payload(out)["traces"][0]
    snip = tr["events"][0]["snippet"]
    assert snip.startswith("連絡は[email]まで")            # CJK next to the address survives
    assert "ops@agentvillage.org" in snip                 # the agents' mailboxes are kept, as on export
    assert "555-123-4567" not in snip and "[phone]" in snip
    assert tr["quotes"][0]["text"] == "メールcat@keep.example.org確認"  # --keep-email-domain holds next to CJK
    assert btv.main([str(src), "-o", str(out), "--keep-emails"]) == 0
    assert _payload(out)["traces"][0]["events"][0]["snippet"] == t["events"][0]["snippet"]


def test_empty_input_points_at_the_exporter(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    assert btv.main([str(tmp_path / "empty"), "-o", str(tmp_path / "page.html")]) == 1
    err = capsys.readouterr().err
    assert "trace_export.py" in err and "mock_trace" not in err
