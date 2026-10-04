"""wiki module + subtasks on a wiki corpus, against a tiny database in the collusion.wiki explorer schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import call, call_error, config_for

from swarm_mcp.server import build_server

SCHEMA = """
create table pages (page_key text primary key, page_id text, wiki text, name text);
create table revisions (revision_id text primary key, page_key text, sequence int, label text, ip16 text, time text,
  time_grade text, uncertainty_seconds int, change_summary text, request_action text, body_len int,
  diff_base_revision_id text, body text);
create table revision_hunks (revision_id text, hunk_index int, operation text, a0 int, a1 int, b0 int, b1 int);
create table page_profiles (page_key text primary key, page_family text, page_family_method text,
  page_family_confidence real, deleted_live int);
create table labels (label text primary key, is_human_handle int);
create table database_metadata (key text primary key, value text);
"""


def make_wiki(root: Path) -> Path:
    """Two task pages and a relay page. Scout creates OecdEvidence; Helper and Watcher append to it 10 and
    20 minutes later; Watcher's addition links to RelayBoard, which Relay created earlier."""
    d = root / "test-wiki"
    d.mkdir(parents=True)
    db = d / "test-wiki.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    pages = [
        ("dse~OecdEvidence", "dse", "OecdEvidence", "oecd-equity"),
        ("dse~RelayBoard", "dse", "RelayBoard", "relay-coordination"),
        ("dse~CashierCount", "dse", "CashierCount", "datausa-cashiers"),
    ]
    for k, w, n, fam in pages:
        con.execute("insert into pages values (?,?,?,?)", (k, k, w, n))
        con.execute("insert into page_profiles values (?,?,?,?,0)", (k, fam, "name:1", 0.9))
    revs = []
    body = ""

    def rev(page, seq, label, ip, t, summary, new_lines):
        nonlocal body
        old = body.splitlines() if seq > 1 else []
        body = "\n".join(old + new_lines)
        rid = f"{page}@{seq}"
        base = f"{page}@{seq - 1}" if seq > 1 else None
        revs.append((rid, page, seq, label, ip, t, "reqlog", 0, summary, "form_edit", len(body), base, body))
        if base:
            con.execute(
                "insert into revision_hunks values (?,0,'insert',?,?,?,?)",
                (rid, len(old), len(old), len(old), len(old) + len(new_lines)),
            )

    rev("dse~RelayBoard", 1, "RelayAgent", "10.1", "2026-06-20T03:00:00Z", "relay", ["Relay board for all cohorts."])
    body = ""
    rev(
        "dse~OecdEvidence",
        1,
        "OecdScout",
        "20.1",
        "2026-06-20T04:00:00Z",
        "precision evidence",
        ["OECD equity tooltip shows Hungary pre-primary 9.91 from querydata."],
    )
    rev(
        "dse~OecdEvidence",
        2,
        "OecdHelper",
        "30.2",
        "2026-06-20T04:10:00Z",
        "request details",
        ["Please share the querydata endpoint for the OECD equity tooltip. -- OecdHelper"],
    )
    rev(
        "dse~OecdEvidence",
        3,
        "OecdWatcher",
        "40.3",
        "2026-06-20T04:20:00Z",
        "coordination",
        ["Mirrored to https://example.at/dse/wiki.cgi?action=browse&id=RelayBoard for the OECD equity cohort."],
    )
    body = ""
    rev("dse~CashierCount", 1, "", "50.5", "2026-06-21T09:00:00Z", "", ["Cashier masters count for Ohio: 1,234."])
    con.executemany("insert into revisions values (?,?,?,?,?,?,?,?,?,?,?,?,?)", revs)
    con.executemany(
        "insert into labels values (?,0)", [("RelayAgent",), ("OecdScout",), ("OecdHelper",), ("OecdWatcher",)]
    )
    con.execute("insert into database_metadata values ('source_catalog_url','https://example.org/download')")
    con.commit()
    con.close()
    return db


@pytest.fixture
def wapp(tmp_path: Path):
    make_wiki(tmp_path / "data")
    return build_server(config_for(tmp_path / "data"))


def test_describe_and_blind_spots(wapp):
    out = call(wapp, "wiki_describe")
    assert out["corpus"] == "test-wiki" and out["counts"]["revisions"] == 5 and out["counts"]["sessions"] == 5
    assert any("blank label" in b for b in out["blind_spots"])


def test_search_matches_only_added_text(wapp):
    out = call(wapp, "wiki_search", query="Hungary")
    assert out["total_matches"] == 1  # later revisions repeat the body but did not add it
    assert out["results"][0]["event_id"] == "wiki:revision:test-wiki/dse~OecdEvidence@1"
    assert call(wapp, "wiki_search", query="cashier")["results"][0]["actor"] == "anon@50.5"
    assert call(wapp, "wiki_search", query="OECD", actor="OecdHelper")["total_matches"] == 1


def test_event_ids(wapp):
    ev = call(wapp, "core_get_event", event_id="wiki:revision:test-wiki/dse~OecdEvidence@2", before=1, after=1)
    assert ev["event"]["actor"] == "OecdHelper" and ev["event"]["text"].startswith("Please share")
    assert ev["event"]["page_family"] == "oecd-equity" and ev["context"] == "previous/next revisions of the same page"
    assert [r["actor"] for r in ev["before"] + ev["after"]] == ["OecdScout", "OecdWatcher"]
    page = call(wapp, "core_get_event", event_id="wiki:page:test-wiki/dse~OecdEvidence", after=5)
    assert "Mirrored" in page["event"]["text"] and page["event"]["editors"] == 3 and len(page["after"]) == 3
    sess = call(wapp, "core_get_event", event_id="wiki:session:test-wiki/dse~OecdEvidence@1")
    assert sess["event"]["actor"] == "OecdScout" and "(created)" in sess["event"]["text"]
    assert "No 'revision' record" in call_error(wapp, "core_get_event", event_id="wiki:revision:test-wiki/nope")


def test_subtasks_on_a_wiki(wapp):
    corpora = {c["corpus"]: c for c in call(wapp, "subtasks_corpora")["corpora"]}
    assert corpora["test-wiki"]["unit"] == "edit session"
    pair = call(wapp, "subtasks_trace_pair", actor_a="OecdScout", actor_b="OecdHelper")
    (h,) = pair["handoffs"]
    assert h["type"] == "builds_on" and h["from_actor"] == "OecdScout" and h["artifacts"] == ["dse~OecdEvidence"]
    assert h["evidence"][0] == "wiki:revision:test-wiki/dse~OecdEvidence@1"
    loc = call(wapp, "subtasks_locate", event_id="wiki:revision:test-wiki/dse~OecdEvidence@3", granularity="coarse")
    sub = loc["matches"][0]["subtask"]
    got = call(wapp, "subtasks_get", subtask_id=sub["subtask_id"])
    members = {m["event_id"] for m in got["members"]}
    assert "wiki:session:test-wiki/dse~OecdEvidence@1" in members
    assert got["dataset_labels"]["counts"].get("oecd-equity", 0) >= 2
    # the link to RelayBoard is an explicit ref from Watcher's session to the session that created RelayBoard
    inf = wapp.swarm_cache.get("subtasks:inference:test-wiki", lambda: None)
    i = inf.index["wiki:session:test-wiki/dse~OecdEvidence@3"]
    j = inf.index["wiki:session:test-wiki/dse~RelayBoard@1"]
    assert inf.refs_raw[i, j] > 0
