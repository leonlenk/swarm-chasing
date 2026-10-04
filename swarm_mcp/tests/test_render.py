"""`swarm-mcp render timeline`: the self-contained HTML explorer (synthetic data only)."""

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
    data = p.payload
    assert data["meta"]["humans"] == 1 and data["meta"]["n_lanes"] == 3
    assert [ln["name"] for ln in data["lanes"]] == res["agents"]
    n_lane_rows = data["ctx"]["a"]
    assert sum(ln["shown"] for ln in data["lanes"]) == res["marks"] == n_lane_rows
    # nothing was sampled, so the human's message is embedded as thread context after the lane rows
    assert data["ctx"] == {"a": n_lane_rows, "n": 1, "complete": True, "excerpts": 0} and res["context_messages"] == 1
    assert len(data["t"]) == len(data["s"]) == len(data["au"]) == n_lane_rows + 1
    lane_ids = {data["idp"] + i for i in data["id"][:n_lane_rows]}
    assert "village:msg:m0002" in lane_ids and "village:msg:m0001" not in lane_ids  # m0001 is the human
    human = data["actors"][data["au"][n_lane_rows]]
    assert human["kind"] == "human" and data["idp"] + data["id"][n_lane_rows] == "village:msg:m0001"
    assert {c["name"] for c in data["channels"]} == {"general", "rest"}
    # lanes index a contiguous, time-sorted slice of the marks; au points back at the lane
    for li, ln in enumerate(data["lanes"]):
        ts = data["t"][ln["a"] : ln["b"]]
        assert ts == sorted(ts) and len(ts) == ln["n"]
        assert set(data["au"][ln["a"] : ln["b"]]) == {li}
        assert ln["kind"] == "agent"
    # the histogram bins are (bin, channel, count) triples covering every lane message
    for ln, dens in zip(data["lanes"], data["dens"]["lanes"], strict=True):
        assert sum(dens[2::3]) == ln["n"]
    # paper style and export helpers are inlined; the explorer script is present
    assert "--oi-vermillion" in page and "window.PaperKit" in page and "SwarmScope timeline explorer" in page
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
    assert page.count("</script>") == len(p.scripts) == 3
    data = p.payload  # and the text survives intact once JSON-decoded
    evil = data["s"][[data["idp"] + i for i in data["id"]].index("village:msg:evil")]
    assert evil.startswith(INJECTION)
    assert res["marks"] == 260


def test_channel_filter(store_path: Path, tmp_path: Path):
    res, page, p = _render(store_path, tmp_path / "t.html", channel="#REST")
    assert res["total_messages_in_filter"] == 1 and res["marks"] == 1
    assert res["agents"] == ["Claude Opus 4.5"]
    assert [c["name"] for c in p.payload["channels"]] == ["rest"]
    assert "channel #rest" in page and p.payload["meta"]["channel"] == "rest"
    with pytest.raises(ToolInputError, match="Unknown channel"):
        render_timeline(store_path, tmp_path / "x.html", channel="nope")
    with pytest.raises(ToolInputError, match="Unknown source"):
        render_timeline(store_path, tmp_path / "x.html", source="nope")


def test_date_filter(store_path: Path, tmp_path: Path):
    since = parse_time("2026-01-05", field="since")
    until = parse_time("2026-01-06", end=True, field="until")  # bare date includes that day
    res, _, p = _render(store_path, tmp_path / "t.html", since=since, until=until)
    n_lane_rows = p.payload["ctx"]["a"]
    ids = sorted(p.payload["idp"] + i for i in p.payload["id"][:n_lane_rows])
    assert ids == ["village:msg:m0002", "village:msg:m0003", "village:msg:m0004"]
    assert [p.payload["idp"] + i for i in p.payload["id"][n_lane_rows:]] == ["village:msg:m0001"]  # context
    assert res["total_messages_in_filter"] == 4  # + the human's m0001
    assert res["range"] == ["2026-01-05T13:00:00Z", "2026-01-06T09:00:00Z"]

    res, _, _ = _render(store_path, tmp_path / "e.html", since=parse_time("2027-01-01"))
    assert res["marks"] == 0 and res["agents"] == [] and res["total_messages_in_filter"] == 0


def test_max_marks_sampling(store_path: Path, tmp_path: Path):
    res, page, p = _render(store_path, tmp_path / "t.html", max_marks=50)
    assert res["sampled"] is True and 0 < res["marks"] <= 50
    data = p.payload
    assert data["sampled"] is True and data["meta"]["sampled"] is True and data["meta"]["marks"] == res["marks"]
    # a sampled page carries no general thread context (the reader says so), only the excerpts the
    # linked panels point at (the busiest threads, the messages behind notable moments)
    ctx = data["ctx"]
    assert ctx["a"] == res["marks"] and ctx["complete"] is False and ctx["n"] == ctx["excerpts"]
    assert len(data["t"]) == res["marks"] + ctx["n"]
    # lane label counts stay the real totals; the histogram bins cover every message
    by_name = {ln["name"]: ln for ln in data["lanes"]}
    assert by_name["GPT-5.2"]["n"] == 253 and by_name["GPT-5.2"]["shown"] < 253
    for ln, dens in zip(data["lanes"], data["dens"]["lanes"], strict=True):
        assert sum(dens[2::3]) == ln["n"]
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
    assert printed["day_one"] == "2026-01-05" and printed["periods"] == 3  # Village days derived from the goals
    page = out.read_text(encoding="utf-8")
    assert "bob.smith@gmail.com" not in page
    p = _Page()
    p.feed(page)
    assert p.payload["x"]["moments"] is not None and p.payload["x"]["arcs"]  # the linked panels are on by default

    lean = tmp_path / "lean.html"
    with pytest.raises(SystemExit) as exc:
        main(["render", "timeline", "--db", str(store_path), "--out", str(lean), "--no-explore"])
    assert exc.value.code == 0
    capsys.readouterr()
    p = _Page()
    p.feed(lean.read_text(encoding="utf-8"))
    assert p.payload["x"] == {} and lean.stat().st_size < out.stat().st_size


def test_village_days_periods_and_annotations(store_path: Path, tmp_path: Path):
    # day 1 is derived from the first village goal (2026-01-05 12:00 UTC is 04:00 in Pacific time)
    res, page, p = _render(store_path, tmp_path / "t.html")
    data = p.payload
    assert res["day_one"] == "2026-01-05"
    assert data["days"] == {"day_one": "2026-01-05", "tz": "America/Los_Angeles", "basis": "first village goal"}
    assert data["meta"]["days"] == "Days 1\u201316"  # 2026-01-21 04:09 UTC is still 20 Jan in Pacific time
    assert [pp["label"] for pp in data["periods"]] == [
        "Collaboratively choose a charity",
        "Compete to build the best game!",
        "Holiday: do whatever you like!",
    ]
    assert data["periods"][-1]["e"] is None and all(pp["kind"] == "village_goal" for pp in data["periods"])

    res, _, p = _render(store_path, tmp_path / "o.html", day_one="2025-12-30")
    assert p.payload["days"]["day_one"] == "2025-12-30" and p.payload["days"]["basis"] == "given"
    res, _, p = _render(store_path, tmp_path / "f.html", day_one=False)
    assert p.payload["days"] is None and res["day_one"] is None

    notes = [
        {"t": "2026-01-10T00:00:00Z", "label": "</script><b>planted</b>"},
        {"t": 1768435200000, "end": 1768521600000, "label": "gap"},
    ]
    _, page, p = _render(store_path, tmp_path / "a.html", annotations=notes)
    assert [n["label"] for n in p.payload["notes"]] == ["</script><b>planted</b>", "gap"]
    assert p.payload["notes"][0]["s"] == 1768003200000 and p.payload["notes"][1]["e"] == 1768521600000
    assert "<b>planted</b>" not in page and page.count("</script>") == len(p.scripts)


def test_mentions_and_actions_cover_every_message(store_path: Path, tmp_path: Path):
    for max_marks in (30_000, 40):  # unsampled and sampled pages carry the same mention bins
        _, _, p = _render(store_path, tmp_path / f"m{max_marks}.html", max_marks=max_marks)
        data = p.payload
        lane_ids = [ln["id"] for ln in data["lanes"]]
        con = duckdb.connect(str(store_path), read_only=True)
        try:
            expected = con.execute(
                """SELECT count(*) FROM (SELECT author_id, unnest(recipient_ids) AS dst FROM messages WHERE ts IS NOT NULL)
                   WHERE list_contains(?, author_id) AND list_contains(?, dst) AND dst <> author_id""",
                [lane_ids, lane_ids],
            ).fetchone()[0]
            actions = con.execute(
                "SELECT count(*) FROM actions WHERE ts IS NOT NULL AND list_contains(?, agent_id)", [lane_ids]
            ).fetchone()[0]
        finally:
            con.close()
        assert expected > 0
        ment = data["ment"]
        assert len(ment) % 4 == 0 and sum(ment[3::4]) == expected
        assert all(0 <= ment[k] < len(lane_ids) for k in range(4, len(ment), 4) for k in (k - 3, k - 2))
        assert sum(sum(a[1::2]) for a in data["acts"]) == actions
        if max_marks == 30_000:  # every lane message is on the page, so per-message recipients add up too
            assert sum(len(r) - 1 for r in data["rc"] if r[0] < data["ctx"]["a"]) == expected


def test_actor_kinds_and_labs(store_path: Path, tmp_path: Path):
    from swarm_mcp.scope.viz.timeline_html import _actor_kind, _lab_group

    assert _actor_kind("human:abc") == "human"
    assert _actor_kind("external:someone") == "external" and _actor_kind("unknown") == "external"
    assert _actor_kind("village:agent:x") == "agent"
    # with the store's agents, membership decides (as in analysis/graph.py): unmatched actors are external
    agents = frozenset({"village:agent:x"})
    assert _actor_kind("village:agent:x", agents) == "agent" and _actor_kind("village:agent:y", agents) == "external"
    assert _actor_kind("human:abc", agents) == "human"
    assert [_lab_group(x) for x in ("Anthropic", "Google DeepMind", "OpenAI", "xAI", None)] == [
        "Anthropic",
        "Google",
        "OpenAI",
        "Other",
        None,
    ]
    _, _, p = _render(store_path, tmp_path / "t.html")
    assert all(a["kind"] in ("agent", "human", "external") for a in p.payload["actors"])


def test_pagekit_days_and_json():
    from datetime import datetime, timezone

    from swarm_mcp.scope.viz import pagekit

    spec = pagekit.day_spec("2025-04-02")
    # AI Village convention: day N is the Pacific date N-1 days after 2025-04-02
    assert pagekit.day_number(datetime(2025, 4, 2, 17, 0), spec) == 1
    assert pagekit.day_number(datetime(2025, 4, 3, 2, 0), spec) == 1  # 19:00 the evening before in Pacific time
    assert pagekit.day_number(datetime(2026, 2, 10, 18, 1), spec) == 315
    assert pagekit.day_start_utc(315, spec) == datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc)
    assert pagekit.day_range_label(datetime(2026, 6, 22, 17), datetime(2026, 6, 25, 21), spec) == "Days 447–450"
    assert pagekit.village_days(None, False) is None
    s = pagekit.json_for_script({"x": "</script> &"})
    assert "</" not in s and " " not in s and "&" not in s
    assert pagekit.fill("__A__ __B__", {"A": "__B__", "B": "b"}) == "__B__ b"  # one pass: no re-scan


def test_linked_panels_payload(store_path: Path, tmp_path: Path):
    _, page, p = _render(store_path, tmp_path / "t.html")
    x = p.payload["x"]
    assert "errors" not in x, x.get("errors")
    lane_ids = [ln["id"] for ln in p.payload["lanes"]]
    # a recap per goal on the page, keyed by period id, plus one for the whole render
    assert set(x["recaps"]) == {pp["id"] for pp in p.payload["periods"]}
    assert set(x["recap_all"]) >= {"totals", "terms", "bursts", "baseline"}
    for rc in x["recaps"].values():
        assert len(rc["terms"]) <= 10 and len(rc["bursts"]) <= 3
        assert all(len(b["ids"]) <= 25 and b["s"] <= b["e"] for b in rc["bursts"])
    # one arc per lane, keyed by lane index; bins are [start ms, messages, actions]
    assert set(x["arcs"]) == {str(i) for i in range(len(lane_ids))}
    for i, arc in x["arcs"].items():
        assert sum(b[1] for b in arc["bins"]) == p.payload["lanes"][int(i)]["n"]
    assert all({"kind", "t", "why", "ids"} <= set(m) for m in x["moments"])
    assert all(s["groups"] and s["starts"] for s in x["series"])
    assert set(x["agent_rates"]["lanes"]) <= {str(i) for i in range(len(lane_ids))}
    # every id the panels point at that falls inside the render is on the page
    on_page = {p.payload["idp"] + i for i in p.payload["id"]}
    for rc in x["recaps"].values():
        for b in rc["bursts"]:
            assert set(b["ids"]) <= on_page

    _, _, p = _render(store_path, tmp_path / "n.html", explore=False)
    assert p.payload["x"] == {}


def test_excerpts_respect_the_filters(store_path: Path, tmp_path: Path):
    # a sampled page embeds the threads the panels point at, but only inside the render's filters
    _, _, p = _render(store_path, tmp_path / "c.html", channel="general", max_marks=40)
    data = p.payload
    rest = [i for i, c in enumerate(data["channels"]) if c["name"] == "rest"]
    assert not rest or all(data["c"][j] != rest[0] for j in range(len(data["t"])))
    assert data["ctx"]["n"] == data["ctx"]["excerpts"] > 0
    assert len(data["t"]) == data["ctx"]["a"] + data["ctx"]["n"]


def test_sweep_series(store_path: Path, tmp_path: Path):
    con = duckdb.connect(str(store_path), read_only=True)
    try:
        ids = [r[0] for r in con.execute("SELECT evidence_id FROM messages ORDER BY ts LIMIT 40").fetchall()]
    finally:
        con.close()
    sweep = tmp_path / "s1.jsonl"
    rows = [{"type": "meta", "sweep_id": "s1", "rubric": "synthetic rubric"}]
    rows += [{"type": "verdict", "event_id": e, "verdict": "yes" if k % 3 == 0 else "no"} for k, e in enumerate(ids)]
    rows += [{"type": "summary"}]
    sweep.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    _, _, p = _render(store_path, tmp_path / "w.html", sweeps=[sweep])
    labels = [s["label"] for s in p.payload["x"]["series"]]
    assert any("s1" in lbl and "judged yes" in lbl for lbl in labels), labels


def test_external_actors_are_not_agents(store_path: Path, tmp_path: Path):
    db = tmp_path / "ext.duckdb"
    shutil.copy(store_path, db)
    con = duckdb.connect(str(db))  # read-write, on the COPY only
    try:
        con.execute(
            """INSERT INTO messages (evidence_id, source, channel, author_id, recipient_ids, ts, ts_quality, content, meta)
               SELECT replace(evidence_id, 'm0002', 'ext1'), source, channel, 'external:visitor', [], ts, 'exact',
                      'a synthetic note from a visitor', '{}'
               FROM messages WHERE evidence_id LIKE '%:m0002'"""
        )
    finally:
        con.close()
    res, _, p = _render(db, tmp_path / "e.html")
    meta = p.payload["meta"]
    assert "external:visitor" not in [ln["id"] for ln in p.payload["lanes"]]  # never a lane
    assert meta["external"] == 1 and meta["humans"] == 2  # meta.humans counts every non-agent author
    kinds = {a["id"]: a["kind"] for a in p.payload["actors"]}
    assert kinds["external:visitor"] == "external" and all(
        v != "external" for k, v in kinds.items() if k != "external:visitor"
    )
