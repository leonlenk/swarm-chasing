"""Conformance check: each failure is reported with counts and masked examples; exit codes 0/1."""

from __future__ import annotations

import json

import pytest
from setup_datasets import make_csv_chat, make_nested_jsonl

from swarm_mcp.setup import cli
from swarm_mcp.setup.check import format_report, run_check

GOOD_CSV = {
    "source": "irc",
    "agents": {"derive_from_actors": True},
    "records": [
        {
            "from": "chatlog_export.csv",
            "kind": "msg",
            "local_id": "MsgNo",
            "time": {"field": "sent_epoch_ms", "format": "epoch_ms"},
            "actor": "from_nick",
            "location": "ChannelName",
            "text": "said",
            "reply_to": "parent_no",
        }
    ],
}


def _spec(**changes):
    spec = json.loads(json.dumps(GOOD_CSV))
    spec["records"][0].update(changes)
    return spec


def _problem(report, code):
    return next(p for p in report["problems"] if p["code"] == code)


@pytest.fixture
def csv_root(tmp_path):
    return make_csv_chat(tmp_path / "b")


def test_good_mapping_passes(csv_root):
    r = run_check(GOOD_CSV, csv_root)
    assert r["status"] == "pass" and r["errors"] == 0
    assert r["counts"]["records"] == 250 and r["counts"]["agents"] == 5


def test_missing_field_suggests_close_match(csv_root):
    r = run_check(_spec(text="sayd"), csv_root)
    p = _problem(r, "field_missing")
    assert r["status"] == "fail" and p["severity"] == "error"
    assert "'sayd'" in p["message"] and "said" in p["message"]


def test_wrong_epoch_unit_is_out_of_range(csv_root):
    r = run_check(_spec(time={"field": "sent_epoch_ms", "format": "epoch_s"}), csv_root)
    p = _problem(r, "time_out_of_range")
    assert p["severity"] == "error" and p["count"] == 250 and len(p["examples"]) == 5


def test_unparseable_times(csv_root):
    r = run_check(_spec(time={"field": "said", "format": "iso"}), csv_root)
    assert _problem(r, "time_unparseable")["count"] == 250


def test_duplicate_ids(csv_root):
    r = run_check(_spec(local_id="ChannelName"), csv_root)
    p = _problem(r, "id_duplicate")
    assert p["severity"] == "error" and p["count"] == 247


def test_empty_text_and_missing_ids(csv_root):
    r = run_check(_spec(text="parent_no", local_id="parent_no"), csv_root)
    assert _problem(r, "id_missing")["severity"] == "error"  # 75% of rows have no parent_no
    assert r["counts"]["rows_dropped"] > 150


def test_unmatched_actors_are_counted_and_masked(tmp_path):
    root = make_nested_jsonl(tmp_path / "a")
    spec = {
        "source": "crew",
        "agents": {"from": "participants.jsonl.gz", "id": "pid", "display_name": "profile.display_label"},
        "records": [
            {
                "from": "utterances.jsonl.gz",
                "kind": "utt",
                "local_id": "uttId",
                "time": "meta.stampedAt",
                "actor": {"field": "payload.body", "match": "id"},
                "text": "payload.body",
            }
        ],
    }
    r = run_check(spec, root)
    p = _problem(r, "actor_unmatched")
    assert p["severity"] == "error" and r["rates"]["actor_unmatched"] == 1.0
    assert all(len(e) < 160 for e in p["examples"])


def test_examples_mask_emails(tmp_path):
    (tmp_path / "m.csv").write_text("id,who,ts,body\n1,alice@corp.example,2026-01-05T10:00:00Z,hi\n")
    spec = {
        "source": "m",
        "agents": {"from": "m.csv", "id": "id"},
        "records": [{"from": "m.csv", "kind": "m", "local_id": "id", "time": "ts", "actor": "who", "text": "body"}],
    }
    r = run_check(spec, tmp_path)
    ex = _problem(r, "actor_unmatched")["examples"][0]
    assert "[email]" in ex and "corp.example" not in ex
    assert "[email]" in format_report(r)


def test_schema_errors_and_missing_tables(csv_root):
    r = run_check({"source": "X", "records": []}, csv_root)
    assert _problem(r, "spec_invalid")["count"] >= 2
    r = run_check(_spec(**{"from": "nope/*.csv"}), csv_root)
    assert _problem(r, "table_missing")["severity"] == "error"


def test_cli_exit_codes(csv_root, tmp_path, capsys):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(GOOD_CSV))
    bad.write_text(json.dumps(_spec(text="sayd")))
    for path, code in ((good, 0), (bad, 1)):
        with pytest.raises(SystemExit) as e:
            cli.main(["check", str(path), str(csv_root)])
        assert e.value.code == code
    assert "FAIL" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["check", str(tmp_path / "missing.json"), str(csv_root)])
    assert e.value.code == 2


def _padded_ids(root):
    root.mkdir(parents=True)
    rows = [{"id": f"{i} ", "ts": f"2024-01-0{i}T00:00:00Z", "who": "a", "body": f"hello {i}", "re": f" {i - 1}"}
            for i in range(1, 6)]  # fmt: skip
    (root / "msgs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    spec = {
        "source": "pad",
        "agents": {"derive_from_actors": True},
        "records": [{"from": "msgs.jsonl", "kind": "msg", "local_id": "id", "time": "ts", "actor": "who",
                     "text": "body", "reply_to": "re"}],
    }  # fmt: skip
    return spec


def test_ids_with_stray_whitespace_are_stripped_and_resolve(tmp_path):
    """Regression: an id "42 " was stored with the space, but citations are parsed (stripped), so the
    id never resolved while the check still passed."""
    from swarm_mcp.scope import db, evidence
    from swarm_mcp.scope.ingest import ingest_mapped

    spec = _padded_ids(tmp_path / "pad")
    r = run_check(spec, tmp_path / "pad")
    assert r["status"] == "pass", format_report(r)
    (tmp_path / "pad.json").write_text(json.dumps(spec))
    ingest_mapped(tmp_path / "pad.json", tmp_path / "pad", tmp_path / "s.duckdb")
    with db.connect(tmp_path / "s.duckdb") as s:
        ids = [row["evidence_id"] for row in s.all("SELECT evidence_id FROM messages ORDER BY evidence_id")]
        assert ids == [f"pad:msg:{i}" for i in range(1, 6)]
        assert evidence.resolve(s, "pad:msg:3")["record"]["reply_to"] == "pad:msg:2"


def test_check_flags_ids_that_do_not_round_trip(tmp_path, monkeypatch):
    from swarm_mcp.setup import mapping

    monkeypatch.setattr(mapping, "_scalar", lambda v: None if v in (None, "") else str(v))  # the old, unstripped
    r = run_check(_padded_ids(tmp_path / "pad"), tmp_path / "pad")
    p = _problem(r, "id_unparseable")
    assert r["status"] == "fail" and p["count"] == 5 and "stray whitespace" in p["message"]
