"""`swarm-mcp render timeline`: the self-contained HTML swimlane (synthetic data only)."""

from __future__ import annotations

import json
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path

import duckdb
import pytest

from swarm_mcp.scope.viz.timeline_html import render_timeline
from swarm_mcp.toolkit import Scrubber, ToolInputError, parse_time

ALLOWED_CDNS = ("https://cdnjs.cloudflare.com/", "https://cdn.jsdelivr.net/")
INJECTION = "</script><script>alert(1)</script>"


class _Page(HTMLParser):
    """Collects script tags, src/href attributes and the embedded JSON payload."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.refs: list[str] = []
        self._in_data = False
        self._data: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.refs += [v for k, v in attrs if k in ("src", "href") and v]
        if tag == "script":
            self.scripts.append(a)
            self._in_data = a.get("id") == "data"

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_data = False

    def handle_data(self, data):
        if self._in_data:
            self._data.append(data)

    @property
    def payload(self) -> dict:
        return json.loads("".join(self._data))


def _render(store: Path, out: Path, **kw) -> tuple[dict, str, _Page]:
    res = render_timeline(store, out, **{"top": 12, "scrub": Scrubber(), "snippet_chars": 160, **kw})
    page = out.read_text(encoding="utf-8")
    p = _Page()
    p.feed(page)
    return res, page, p


def test_render_synthetic_store(store_path: Path, tmp_path: Path):
    out = tmp_path / "nested" / "dir" / "timeline.html"  # parent dirs are created
    res, page, p = _render(store_path, out)

    assert res["out"] == str(out) and out.exists()
    assert res["bytes"] == out.stat().st_size
    # o3 never posted; the human is excluded from lanes but counted
    assert set(res["agents"]) == {"Claude Opus 4.5", "GPT-5.2", "Gemini 2.5 Pro"}
    assert res["agents"][0] == "GPT-5.2"  # ordered by message count
    assert res["total_messages_in_filter"] == 260
    assert res["human_messages_excluded"] == 1
    assert res["marks"] == 259 and res["sampled"] is False
    assert res["range"] == ["2026-01-05T13:00:00Z", "2026-01-21T04:09:00Z"]

    assert "<title>SwarmScope timeline</title>" in page
    assert "1 human messages excluded" in page
    data = p.payload
    assert [ln["name"] for ln in data["lanes"]] == res["agents"]
    assert sum(ln["shown"] for ln in data["lanes"]) == res["marks"] == len(data["t"]) == len(data["s"])
    ids = {data["idp"] + i for i in data["id"]}
    assert "village:msg:m0002" in ids and "village:msg:m0001" not in ids  # m0001 is the human
    assert {c["name"] for c in data["channels"]} == {"general", "rest"}
    # lanes index a contiguous, time-sorted slice of the marks
    for ln in data["lanes"]:
        ts = data["t"][ln["a"] : ln["b"]]
        assert ts == sorted(ts) and len(ts) == ln["n"]
    # long text is truncated to snippet_chars (+ ellipsis)
    assert max(len(s) for s in data["s"]) <= 161
    # footer note
    assert "untrusted agent output" in page


def test_snippets_are_masked(store_path: Path, tmp_path: Path):
    _, page, p = _render(store_path, tmp_path / "t.html")
    assert "bob.smith@gmail.com" not in page
    assert "555 0134" not in page and "555-0199" not in page
    data = p.payload
    m4 = data["s"][[data["idp"] + i for i in data["id"]].index("village:msg:m0004")]
    assert "[email]" in m4 and "[phone]" in m4


def test_no_external_scripts(store_path: Path, tmp_path: Path):
    _, page, p = _render(store_path, tmp_path / "t.html")
    for s in p.scripts:
        src = s.get("src")
        assert src is None or src.startswith(ALLOWED_CDNS), src
    assert not re.search(r"<script[^>]*\bsrc=", page)  # in fact: none at all
    assert all(r.startswith(ALLOWED_CDNS) for r in p.refs)
    assert [s.get("type") for s in p.scripts].count("application/json") == 1


def test_script_injection_is_neutralised(store_path: Path, tmp_path: Path):
    db = tmp_path / "copy.duckdb"
    shutil.copy(store_path, db)
    con = duckdb.connect(str(db))  # read-write, on the COPY only
    try:
        con.execute(
            """INSERT INTO messages (evidence_id, source, channel, author_id, recipient_ids, ts, ts_quality, content, meta)
               SELECT 'village:msg:evil', source, 'general', author_id, [], TIMESTAMP '2026-01-09 12:00:00',
                      'exact', ?, '{}'
               FROM messages WHERE evidence_id = 'village:msg:m0002'""",
            [INJECTION + " & <b>bold</b>"],
        )
    finally:
        con.close()

    res, page, p = _render(db, tmp_path / "evil.html")
    assert "</script><script>alert(1)" not in page
    assert "<b>bold</b>" not in page
    # exactly one closing tag per script element: nothing in the data closed a tag early
    assert page.count("</script>") == len(p.scripts) == 2
    data = p.payload  # and the text survives intact once JSON-decoded
    evil = data["s"][[data["idp"] + i for i in data["id"]].index("village:msg:evil")]
    assert evil.startswith(INJECTION)
    assert res["marks"] == 260


def test_channel_filter(store_path: Path, tmp_path: Path):
    res, page, p = _render(store_path, tmp_path / "t.html", channel="#REST")
    assert res["total_messages_in_filter"] == 1 and res["marks"] == 1
    assert res["agents"] == ["Claude Opus 4.5"]
    assert [c["name"] for c in p.payload["channels"]] == ["rest"]
    assert "channel: #rest" in page
    with pytest.raises(ToolInputError, match="Unknown channel"):
        render_timeline(store_path, tmp_path / "x.html", channel="nope")
    with pytest.raises(ToolInputError, match="Unknown source"):
        render_timeline(store_path, tmp_path / "x.html", source="nope")


def test_date_filter(store_path: Path, tmp_path: Path):
    since = parse_time("2026-01-05", field="since")
    until = parse_time("2026-01-06", end=True, field="until")  # bare date includes that day
    res, _, p = _render(store_path, tmp_path / "t.html", since=since, until=until)
    ids = sorted(p.payload["idp"] + i for i in p.payload["id"])
    assert ids == ["village:msg:m0002", "village:msg:m0003", "village:msg:m0004"]
    assert res["total_messages_in_filter"] == 4  # + the human's m0001
    assert res["range"] == ["2026-01-05T13:00:00Z", "2026-01-06T09:00:00Z"]

    res, _, _ = _render(store_path, tmp_path / "e.html", since=parse_time("2027-01-01"))
    assert res["marks"] == 0 and res["agents"] == [] and res["total_messages_in_filter"] == 0


def test_max_marks_sampling(store_path: Path, tmp_path: Path):
    res, page, p = _render(store_path, tmp_path / "t.html", max_marks=50)
    assert res["sampled"] is True and 0 < res["marks"] <= 50
    assert "Sampled:" in page
    data = p.payload
    assert data["sampled"] is True
    # lane label counts stay the real totals; the density band covers every message
    by_name = {ln["name"]: ln for ln in data["lanes"]}
    assert by_name["GPT-5.2"]["n"] == 253 and by_name["GPT-5.2"]["shown"] < 253
    for ln, dens in zip(data["lanes"], data["dens"]["lanes"], strict=True):
        assert sum(dens[1::2]) == ln["n"]
    # deterministic
    res2, _, p2 = _render(store_path, tmp_path / "t2.html", max_marks=50)
    assert p2.payload["id"] == data["id"] and res2["marks"] == res["marks"]


def test_top_limits_lanes(store_path: Path, tmp_path: Path):
    res, _, p = _render(store_path, tmp_path / "t.html", top=1)
    assert res["agents"] == ["GPT-5.2"] and len(p.payload["lanes"]) == 1
    assert res["marks"] == 253 and res["messages_in_lanes"] == 253


def test_cli_render(store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    from swarm_mcp.cli import main

    monkeypatch.setenv("SWARM_DATA_DIR", str(store_path.parent))
    out = tmp_path / "t.html"
    with pytest.raises(SystemExit) as exc:
        main(["render", "timeline", "--db", str(store_path), "--out", str(out)])
    assert exc.value.code == 0
    assert out.exists()
    printed = json.loads(capsys.readouterr().out)
    assert printed["out"] == str(out) and printed["marks"] == 259
    assert "bob.smith@gmail.com" not in out.read_text(encoding="utf-8")
