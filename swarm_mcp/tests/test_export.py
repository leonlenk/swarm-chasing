"""Export pipeline: files, manifest (counts, never values), check(), CLI and filters.

All records are synthetic; fake secrets are assembled at runtime from pieces.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from swarm_mcp.events import event_record
from swarm_mcp.export import AGENTS_FILE, EVENTS_FILE, MANIFEST_FILE, ExportError, check, export, main, select
from swarm_mcp.redact import Redactor

GH_TOKEN = "gh" + "p_" + "a1B2c3D4e5" * 4
API_VALUE = "Zx9Q" + "w8Er7Ty6Ui5Op4As"
PASSWORD = "hunter" + "22"
EMAIL = "bob.smith" + "@" + "gmail.com"
PHONE = "+1 415 555 0134"
SECRETS = (GH_TOKEN, API_VALUE, PASSWORD, EMAIL, "555 0134", "carol" + "@" + "example.com")


def records() -> list[dict]:
    return [
        event_record(
            "village:chat:m1",
            time="2026-01-05T13:00:00Z",
            actor="GPT-5.2",
            actor_type="agent",
            location="general",
            text=f"mail {EMAIL} or help@agentvillage.org, call {PHONE}",
        ),
        event_record(
            "village:chat:m2",
            time="2026-01-06T09:30:00Z",
            actor="Claude Opus 4.5",
            actor_type="agent",
            location="general",
            text=f"pushed with {GH_TOKEN}; version 1.234.5 on 2026-01-15",
            api_key=API_VALUE,
            meta={"login": f"password: {PASSWORD}", "count": 3},
        ),
        event_record(
            "village:event:e1",
            time="2026-01-07T00:00:00Z",
            actor="human:u1",
            actor_type="human",
            location="rest",
            text="nothing sensitive, commit 3f9a1c2b4d5e6f708192a3b4c5d6e7f8091a2b3c",
        ),
        event_record(
            "git:commit:rpg-game@abc123",
            time="2026-01-08T00:00:00Z",
            actor="Gemini 2.5 Pro",
            text="clone git@github.com:org/rpg-game.git",
        ),
    ]


AGENTS = [
    {"id": "a1", "name": "GPT-5.2", "contact": "carol" + "@" + "example.com"},
    {"id": "a2", "name": "Claude Opus 4.5", "contact": None},
]


@pytest.fixture
def exported(tmp_path: Path) -> tuple[Path, dict]:
    out = tmp_path / "exp"
    manifest = export(
        iter(records()),
        out,
        Redactor(allow_email_domains=["agentvillage.org"]),
        agents=AGENTS,
        filters_desc={"source": ["village", "git"], "note": f"asked by {EMAIL}"},
    )
    return out, manifest


def lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_export_writes_redacted_records(exported):
    out, _ = exported
    events = lines(out / EVENTS_FILE)
    assert [e["event_id"] for e in events] == [r["event_id"] for r in records()]
    m1, m2, e1, c1 = events
    assert m1["text"] == "mail [email] or help@agentvillage.org, call [phone]"
    assert m2["text"] == "pushed with [credential]; version 1.234.5 on 2026-01-15"
    assert m2["api_key"] == "[credential]" and m2["meta"] == {"login": "password: [credential]", "count": 3}
    assert e1["text"] == records()[2]["text"]  # hex ids and dates survive
    assert c1["text"] == "clone git@github.com:org/rpg-game.git"
    assert list(m1)[:8] == ["event_id", "source", "kind", "time", "actor", "actor_type", "location", "text"]
    agents = lines(out / AGENTS_FILE)
    assert agents == [
        {"id": "a1", "name": "GPT-5.2", "contact": "[email]"},
        {"id": "a2", "name": "Claude Opus 4.5", "contact": None},
    ]


def test_manifest_has_counts_not_values(exported):
    out, manifest = exported
    on_disk = json.loads((out / MANIFEST_FILE).read_text())
    assert on_disk == manifest
    assert manifest["format"] == "swarmscope-export" and manifest["tool_version"]
    assert manifest["created_at"].endswith("Z")
    rec = manifest["records"]
    assert rec["events"] == 4 and rec["agents"] == 2
    assert rec["by_source"] == {"git": 1, "village": 3}
    assert rec["by_source_kind"] == {"git": {"commit": 1}, "village": {"chat": 2, "event": 1}}
    assert rec["time_range"] == {"first": "2026-01-05T13:00:00Z", "last": "2026-01-08T00:00:00Z"}
    red = manifest["redaction"]
    # m1: email + phone; m2: token, api_key, password; agents: 1 email; filters: 1 email
    assert red["counts"] == {"credential": 3, "email": 3, "phone": 1}
    assert red["records_changed"] == 2 and red["allow_email_domains"] == ["agentvillage.org"]
    assert manifest["filters"] == {"source": ["village", "git"], "note": "asked by [email]"}
    # per-file content hashes
    for name in (EVENTS_FILE, AGENTS_FILE):
        data = (out / name).read_bytes()
        assert manifest["files"][name] == {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "lines": data.count(b"\n"),
        }
    # no sensitive value appears anywhere in the export
    blob = "".join(p.read_text() for p in out.iterdir())
    for secret in SECRETS:
        assert secret not in blob, secret


def test_check_passes_on_clean_export(exported):
    out, _ = exported
    report = check(out)
    assert report.ok and bool(report), report.to_dict()
    assert report.files_scanned == [AGENTS_FILE, EVENTS_FILE, MANIFEST_FILE]
    assert report.honoured_email_domains == ["agentvillage.org"] and "ip" in report.rules
    assert report.lines_scanned > 6


def test_check_fails_on_tampered_export_without_leaking(exported):
    out, _ = exported
    with (out / EVENTS_FILE).open("a") as f:
        f.write(json.dumps({"event_id": "village:chat:x", "text": f"ping {EMAIL}", "token": GH_TOKEN}) + "\n")
    report = check(out)
    assert not report.ok
    found = {(f["file"], f["line"], f["field"], f["type"]) for f in report.findings}
    assert found == {(EVENTS_FILE, 5, "text", "email"), (EVENTS_FILE, 5, "token", "credential")}
    problems = {p["problem"].split(" (")[0] for p in report.problems}
    assert "sha256 does not match manifest" in problems
    assert any("line count 5" in p["problem"] for p in report.problems)
    dumped = json.dumps(report.to_dict())
    for secret in SECRETS:
        assert secret not in dumped


def test_check_finds_pii_next_to_non_ascii_letters(exported):
    out, _ = exported
    with (out / EVENTS_FILE).open("a") as f:
        f.write(json.dumps({"event_id": "village:chat:y", "text": "連絡はbob@example.comまで、電話+81 90 1234 5678です"}) + "\n")
    report = check(out)
    assert {(f["field"], f["type"]) for f in report.findings} == {("text", "email"), ("text", "phone")}


def test_check_detects_benign_tampering_and_unlisted_files(exported):
    out, _ = exported
    p = out / AGENTS_FILE
    p.write_text(p.read_text().replace("GPT-5.2", "GPT-5.3"))
    (out / "notes.txt").write_text("call (415) 555-0199\n")
    report = check(out)
    assert not report.ok
    assert {(x["file"], x["problem"].split(" (")[0]) for x in report.problems} >= {
        (AGENTS_FILE, "sha256 does not match manifest"),
        ("notes.txt", "not listed in manifest"),
    }
    assert report.findings == [{"file": "notes.txt", "line": 1, "field": "<text>", "type": "phone", "count": 1}]


def test_check_missing_manifest_and_dir(tmp_path):
    assert not check(tmp_path / "nope").ok
    (tmp_path / "e").mkdir()
    (tmp_path / "e" / EVENTS_FILE).write_text("{}\n")
    r = check(tmp_path / "e")
    assert not r.ok and {"file": MANIFEST_FILE, "problem": "missing"} in r.problems


def test_check_is_stricter_than_default_export(tmp_path):
    recs = [event_record("village:chat:1", time="2026-01-05T00:00:00Z", actor="a", text="server 10.0.0.5")]
    export(recs, tmp_path / "a", Redactor())
    r = check(tmp_path / "a")
    assert not r.ok and r.counts == {"ip": 1}
    export(recs, tmp_path / "b", Redactor(["default", "ip"]))
    assert check(tmp_path / "b").ok


def test_allowlist_can_be_ignored_by_check(exported):
    out, _ = exported
    strict = check(out, honour_allowlist=False)
    assert not strict.ok and strict.counts == {"email": 1}
    assert strict.findings[0]["file"] == EVENTS_FILE and strict.findings[0]["line"] == 1


def test_reexport_without_agents_removes_stale_file(exported):
    out, _ = exported
    manifest = export(records()[:1], out, Redactor(allow_email_domains=["agentvillage.org"]))
    assert not (out / AGENTS_FILE).exists() and AGENTS_FILE not in manifest["files"]
    assert manifest["records"]["agents"] is None and check(out).ok


def test_bad_records_are_rejected(tmp_path):
    with pytest.raises(ExportError, match="record 2: Malformed event_id"):
        export([records()[0], {"text": "no id"}], tmp_path / "x", Redactor())
    with pytest.raises(ExportError, match="record 1: expected a JSON object"):
        export(["nope"], tmp_path / "y", Redactor())
    # a failed re-export over a good one leaves no manifest, so check() fails
    export(records(), tmp_path / "z", Redactor())
    with pytest.raises(ExportError):
        export([records()[0], {"text": "no id"}], tmp_path / "z", Redactor())
    assert not (tmp_path / "z" / MANIFEST_FILE).exists() and not check(tmp_path / "z").ok


def test_select_filters():
    recs = records()
    assert [r["event_id"] for r in select(recs, sources=["git"])] == ["git:commit:rpg-game@abc123"]
    assert len(list(select(recs, kinds=["chat"]))) == 2
    assert len(list(select(recs, kinds=["village:event"]))) == 1
    assert [r["actor"] for r in select(recs, actors=["GPT-5.2"])] == ["GPT-5.2"]
    # half-open; a bare-date upper bound includes that day
    got = [r["event_id"] for r in select(recs, since="2026-01-06", until="2026-01-07")]
    assert got == ["village:chat:m2", "village:event:e1"]
    assert len(list(select(recs, since="2026-01-06T09:30:00Z", until="2026-01-07T00:00:00Z"))) == 1


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_cli_export_and_check(tmp_path, capsys):
    src = write_jsonl(tmp_path / "records.jsonl", records())
    agents = write_jsonl(tmp_path / "agents.jsonl", AGENTS)
    out = tmp_path / "share"
    code = main(
        ["--in", str(src), "--out", str(out), "--agents", str(agents), "--allow-email-domain", "agentvillage.org",
         "--source", "village", "--since", "2026-01-05", "--filters-desc", "charity week chat", "--check"]
    )  # fmt: skip
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    assert result["check"]["ok"] and result["records"]["by_source"] == {"village": 3}
    manifest = json.loads((out / MANIFEST_FILE).read_text())
    assert manifest["filters"] == {"source": ["village"], "since": "2026-01-05", "description": "charity week chat"}

    with (out / EVENTS_FILE).open("a") as f:
        f.write(json.dumps({"event_id": "village:chat:x", "text": EMAIL}) + "\n")
    assert main(["--check", "--out", str(out)]) == 1
    report = json.loads(capsys.readouterr().out)["check"]
    assert {"file": EVENTS_FILE, "line": 4, "field": "text", "type": "email", "count": 1} in report["findings"]
    assert EMAIL not in json.dumps(report)


def test_cli_usage_errors(tmp_path, capsys):
    assert main(["--out", str(tmp_path / "o")]) == 2
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"event_id": "village:chat:1"}\nnot json\n')
    assert main(["--in", str(bad), "--out", str(tmp_path / "o")]) == 2
    assert "bad.jsonl:2: not valid JSON" in capsys.readouterr().err
    assert main(["--in", str(bad), "--out", str(tmp_path / "o"), "--rules", "nope"]) == 2


def test_large_export_is_fast(tmp_path):
    n = 30_000

    def gen():
        for i in range(n):
            text = f"message {i}: shipped v1.{i}.0 on 2026-01-15"
            if i % 100 == 0:
                text += f" mail user{i}@example.com"
            yield event_record(
                f"village:chat:m{i}",
                time=f"2026-01-15T00:{i // 1000 % 60:02d}:00Z",
                actor="A",
                location="general",
                text=text,
            )

    t = time.perf_counter()
    manifest = export(gen(), tmp_path / "big", Redactor())
    report = check(tmp_path / "big")
    assert time.perf_counter() - t < 20
    assert manifest["records"]["events"] == n and manifest["redaction"]["counts"] == {"email": n // 100}
    assert report.ok
