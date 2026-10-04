"""The wiki adapter and subtasks on a wiki source, against a tiny database in the collusion.wiki explorer schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import call, config_for

from swarm_mcp.scope.ingest import ingest
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
    data = tmp_path / "data"
    make_wiki(data)
    ingest("wiki", data / "test-wiki", data / "swarmscope.duckdb", progress=lambda _m: None)
    return build_server(config_for(data))


def test_ingest_and_blind_spots(wapp):
    src = call(wapp, "core_info")["sources"][0]
    assert src["source"] == "test-wiki" and src["adapter"] == "wiki"
    assert src["row_counts"]["messages"] == 5 and src["row_counts"]["artifacts"] == 3
    assert any("self-chosen" in n for n in src["ingest_meta"]["notes"])


def test_search_matches_only_added_text(wapp):
    out = call(wapp, "scope_search", query="Hungary")
    assert out["total"] == 1  # later revisions repeat the body but did not add it
    assert out["results"][0]["evidence_id"] == "test-wiki:msg:dse~OecdEvidence@1"
    assert call(wapp, "scope_search", query="cashier")["total"] == 1


def test_records_and_artifacts(wapp):
    rev = call(wapp, "core_get", ids="test-wiki:msg:dse~OecdEvidence@2", before=1, after=1)
    assert rev["author"] == "OecdHelper" and rev["content"]["content"].startswith("Please share")
    assert rev["channel"] == "dse:OecdEvidence" and rev["reply_to"] == "test-wiki:msg:dse~OecdEvidence@1"
    assert rev["msg_type"] == "revision" and rev["meta"]["page_family"] == "oecd-equity"
    assert rev["artifacts"] == [{"artifact_id": "test-wiki:artifact:dse~OecdEvidence", "op": "modify"}]
    watcher = call(wapp, "core_get", ids="test-wiki:msg:dse~OecdEvidence@3")
    assert {"artifact_id": "test-wiki:artifact:dse~RelayBoard", "op": "mention"} in watcher["artifacts"]
    page = call(wapp, "core_get", ids="test-wiki:artifact:dse~OecdEvidence")
    assert page["touches_by_op"] == {"create": 1, "modify": 2} and page["meta"]["category"] == "oecd-equity"
    anon = call(wapp, "core_get", ids="test-wiki:agent:anon@50.5")
    assert anon["meta"]["kind"] == "blank_label"


def test_subtasks_on_a_wiki(wapp):
    corpora = {c["corpus"]: c for c in call(wapp, "subtasks_corpora")["corpora"]}
    assert corpora["test-wiki"]["grouping_periods"] == 0  # no periods: units are sessions
    pair = call(wapp, "subtasks_trace_pair", actor_a="OecdScout", actor_b="OecdHelper")
    (h,) = pair["handoffs"]
    assert h["type"] == "builds_on" and h["from_actor"] == "OecdScout"
    assert h["artifacts"] == ["test-wiki:artifact:dse~OecdEvidence"]
    assert h["evidence"][0] == "test-wiki:msg:dse~OecdEvidence@1"
    loc = call(wapp, "subtasks_locate", event_id="test-wiki:msg:dse~OecdEvidence@3", granularity="coarse")
    sub = loc["matches"][0]["subtask"]
    got = call(wapp, "subtasks_get", subtask_id=sub["subtask_id"])
    assert got["unit"] == "session" and "test-wiki:msg:dse~OecdEvidence@1" in {m["event_id"] for m in got["members"]}
    assert got["dataset_labels"]["counts"].get("oecd-equity", 0) >= 2
    # Watcher linked RelayBoard: an explicit ref from Watcher's session to the session that created that page
    _, inf = wapp.swarm_cache.get("subtasks:inference:test-wiki", lambda: None)
    i = inf.index["test-wiki:msg:dse~OecdEvidence@3"]
    j = inf.index["test-wiki:msg:dse~RelayBoard@1"]
    assert inf.refs_raw[i, j] > 0
