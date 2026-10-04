"""Findings: record/list/spotcheck tools, check_findings, the CLI, and the two Claude Code hooks.

All data is synthetic (conftest). Hooks run as subprocesses with sample hook JSON on stdin.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import A_OPUS, call, call_error, config_for

from swarm_mcp.scope import db
from swarm_mcp.scope import findings as lib
from swarm_mcp.scope.evidence import EvidenceError
from swarm_mcp.server import build_server
from swarm_mcp.toolkit import ToolInputError

REPO = Path(__file__).resolve().parents[2]
AUDIT_HOOK = REPO / "hooks" / "audit_log.py"
STOP_HOOK = REPO / "hooks" / "require_evidence.py"
GOOD_IDS = ["village:chat:m0003", f"village:agent:{A_OPUS}", "village:event:e0001", "village:goal:g1"]


@pytest.fixture
def fdir(data_dir: Path) -> Path:
    """The findings dir the `app` fixture uses (config_for: <tmp>/findings)."""
    return data_dir.parent / "findings"


def _lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if ln.strip()] if path.exists() else []


# --------------------------------------------------------------------------- tools


def test_module_loads_and_skips_without_store(app, tmp_path: Path):
    tools = {r.name: r for r in app.swarm_registry.records.values()}
    assert tools["findings"].status == "loaded"
    assert set(tools["findings"].tools) == {"findings_record", "findings_list"}

    empty = build_server(config_for(tmp_path / "nodata"))
    rec = empty.swarm_registry.records["findings"]
    assert rec.status == "skipped" and "swarm-mcp add data/ai-village" in rec.reasons[0]


def test_record_valid_finding_writes_jsonl_and_duckdb(app, fdir: Path, store_path: Path):
    out = call(
        app,
        "findings_record",
        claim="GPT-5.2 endorsed GiveDirectly early in the charity goal.",
        evidence_ids=["village:chat:m0003", "village:chat:m0003", f"village:agent:{A_OPUS}", "village:goal:g1"],
        confidence="high",
    )
    f = out["finding"]
    assert f["finding_id"].startswith("f-") and len(f["finding_id"]) == 14
    assert f["evidence_ids"] == ["village:chat:m0003", f"village:agent:{A_OPUS}", "village:goal:g1"]  # deduped
    assert f["claim"]["untrusted"] is True and "GiveDirectly" in f["claim"]["content"]
    assert f["confidence"] == "high" and f["author"] == "claude" and f["status"] == "open"
    assert f["created_at"].endswith("Z")
    assert out["db_synced"] is True and out["notes"] == []
    ev = {e["evidence_id"]: e for e in out["evidence"]}
    assert ev["village:chat:m0003"]["table"] == "messages"
    assert ev["village:chat:m0003"]["content"]["untrusted"] is True
    assert "GiveDirectly" in ev["village:chat:m0003"]["content"]["content"]
    assert ev[f"village:agent:{A_OPUS}"]["content"]["content"] == "Claude Opus 4.5"

    lines = _lines(fdir / "findings.jsonl")
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["finding_id"] == f["finding_id"] and row["claim"].startswith("GPT-5.2 endorsed")
    assert set(row) == {"finding_id", "created_at", "claim", "evidence_ids", "confidence", "author", "status"}

    with db.connect(store_path) as s:
        dbrow = s.one("SELECT * FROM findings WHERE finding_id = ?", [f["finding_id"]])
    assert dbrow is not None and list(dbrow["evidence_ids"]) == f["evidence_ids"]
    assert dbrow["confidence"] == "high"


def test_record_long_message_snippet_is_capped(app):
    out = call(app, "findings_record", claim="Opus wrote a long report.", evidence_ids=["village:chat:m0007"])
    c = out["evidence"][0]["content"]
    assert c["truncated"] is True and len(c["content"]) < 260


def test_record_rejects_fake_id_and_writes_nothing(app, fdir: Path, store_path: Path):
    err = call_error(
        app,
        "findings_record",
        claim="Something happened.",
        evidence_ids=["village:chat:m0003", "village:chat:does-not-exist"],
    )
    assert "village:chat:does-not-exist" in err and "does not resolve" in err
    assert "copied exactly from tool results" in err
    assert "village:chat:m0003'" not in err  # only bad ids are listed
    assert not (fdir / "findings.jsonl").exists()
    with db.connect(store_path) as s:
        assert s.scalar("SELECT count(*) FROM findings") == 0


def test_record_rejects_malformed_and_empty(app, fdir: Path):
    err = call_error(app, "findings_record", claim="x", evidence_ids=["m0003"])
    assert "m0003" in err and "Malformed" in err
    err = call_error(app, "findings_record", claim="x", evidence_ids=["village:tweet:1"])
    assert "Unknown evidence kind" in err
    err = call_error(app, "findings_record", claim="x", evidence_ids=[])
    assert "at least one evidence id" in err
    err = call_error(app, "findings_record", claim="   ", evidence_ids=["village:chat:m0003"])
    assert "claim must not be empty" in err
    assert not (fdir / "findings.jsonl").exists()


def test_record_finding_library_errors(store_path: Path, tmp_path: Path):
    with pytest.raises(EvidenceError, match="village:chat:nope"):
        lib.record_finding(tmp_path / "f", store_path, claim="c", evidence_ids=["village:chat:nope"])
    with pytest.raises(ToolInputError, match="confidence"):
        lib.record_finding(tmp_path / "f", store_path, claim="c", evidence_ids=GOOD_IDS[:1], confidence="sure")
    with pytest.raises(db.StoreMissing):
        lib.record_finding(tmp_path / "f", tmp_path / "missing.duckdb", claim="c", evidence_ids=GOOD_IDS[:1])
    assert not (tmp_path / "f").exists()


def test_record_survives_locked_store(store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def locked(*a, **kw):
        raise db.duckdb.IOException("Could not set lock on file: Conflicting lock is held in pid 1")

    monkeypatch.setattr(lib, "MIRROR_TIMEOUT", 0.2)
    real_open = db.open_connection
    monkeypatch.setattr(
        db, "open_connection", lambda p, read_only=True, timeout=10.0: real_open(p) if read_only else locked()
    )
    out = lib.record_finding(tmp_path / "f", store_path, claim="c", evidence_ids=GOOD_IDS[:1])
    assert out["db_synced"] is False and "source of truth" in out["notes"][0]
    assert len(_lines(tmp_path / "f" / "findings.jsonl")) == 1


def test_findings_list(app, fdir: Path):
    a = call(app, "findings_record", claim="first", evidence_ids=["village:chat:m0001"])["finding"]
    b = call(app, "findings_record", claim="second", evidence_ids=["village:chat:m0002"], confidence="low")["finding"]
    out = call(app, "findings_list")
    assert out["total_matching"] == 2 and out["returned"] == 2 and out["has_more"] is False
    assert [f["finding_id"] for f in out["findings"]] == [b["finding_id"], a["finding_id"]]  # newest first
    assert out["findings"][0]["claim"] == {"content": "second", "untrusted": True}
    assert out["findings"][0]["line"] == 2

    out = call(app, "findings_list", limit=1)
    assert out["returned"] == 1 and out["has_more"] is True and out["notes"]
    assert call(app, "findings_list", status="confirmed")["total_matching"] == 0

    with open(fdir / "findings.jsonl", "a") as fh:
        fh.write("{not json\n")
    out = call(app, "findings_list")
    assert out["total_matching"] == 2 and out["parse_errors"][0]["line"] == 3
    assert any("corrupt" in n for n in out["notes"])

    # appending after a hand edit without a trailing newline must not glue lines together
    with open(fdir / "findings.jsonl", "a") as fh:
        fh.write('{"note": "no newline"}')
    call(app, "findings_record", claim="third", evidence_ids=["village:chat:m0004"])
    assert len(_lines(fdir / "findings.jsonl")) == 5
    out = call(app, "findings_list")
    assert out["total_matching"] == 3 and [e["line"] for e in out["parse_errors"]] == [3, 4]
    assert "not a finding" in out["parse_errors"][1]["error"]


# --------------------------------------------------------------------------- spotcheck


def test_spotcheck_library_stable_order(store_path: Path):
    with db.connect(store_path) as s:
        one = [r["evidence_id"] for r in lib.spotcheck_sample(s, kind="messages", n=10, seed=3)]
    with db.connect(store_path) as s:
        two = [r["evidence_id"] for r in lib.spotcheck_sample(s, kind="messages", n=10, seed=3)]
        acts = lib.spotcheck_sample(s, kind="actions", n=10, seed=0)
        with pytest.raises(ToolInputError, match="channel"):
            lib.spotcheck_sample(s, kind="actions", channel="general")
    assert one == two
    assert {a["evidence_id"] for a in acts} == {"village:event:e0001", "village:event:e0003", "village:event:e0004"}


def test_findings_list_sample(app):
    for i in range(6):
        call(
            app, "findings_record", claim=f"claim {i}", evidence_ids=[f"village:chat:m{100 + i:04d}", "village:goal:g3"]
        )
    a = call(app, "findings_list", sample=3, seed=1)
    b = call(app, "findings_list", sample=3, seed=1)
    ids = [i["finding"]["finding_id"] for i in a["items"]]
    assert ids == [i["finding"]["finding_id"] for i in b["items"]] and len(set(ids)) == 3
    item = a["items"][0]
    assert item["finding"]["claim"]["untrusted"] is True
    assert [e["evidence_id"] for e in item["evidence"]][1] == "village:goal:g3"
    assert item["evidence"][0]["content"]["content"].startswith("filler message")
    seeds = {tuple(i["finding"]["finding_id"] for i in call(app, "findings_list", sample=3, seed=s)["items"])
             for s in range(6)}  # fmt: skip
    assert len(seeds) > 1
    assert call(app, "findings_list", sample=3, status="rejected")["returned"] == 0
    many = call(app, "findings_list", sample=50)
    assert many["returned"] == 6 and many["notes"]


# --------------------------------------------------------------------------- check_findings + CLI


def _write_findings(path: Path, rows: list[dict | str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))
    return path


def _finding(fid: str, ids: list[str]) -> dict:
    return {
        "finding_id": fid,
        "created_at": "2026-10-03T12:00:00Z",
        "claim": "a claim",
        "evidence_ids": ids,
        "confidence": "medium",
        "author": "test",
        "status": "open",
    }


def test_check_findings_ok_and_missing(store_path: Path, tmp_path: Path):
    missing = lib.check_findings(tmp_path / "none.jsonl", store_path)
    assert missing["ok"] is True and missing["checked"] == 0
    empty = _write_findings(tmp_path / "empty.jsonl", [])
    assert lib.check_findings(empty, store_path)["ok"] is True

    good = _write_findings(tmp_path / "good.jsonl", [_finding("f-1", GOOD_IDS), _finding("f-2", GOOD_IDS[:1])])
    res = lib.check_findings(good, store_path)
    assert res["ok"] is True and res["checked"] == 2 and res["problems"] == [] and res["parse_errors"] == []

    nostore = lib.check_findings(good, tmp_path / "nope.duckdb")
    assert nostore["ok"] is False and nostore["store_missing"] is True and "swarm-mcp ingest" in nostore["message"]


def test_check_findings_corrupt(store_path: Path, tmp_path: Path):
    bad = _write_findings(
        tmp_path / "bad.jsonl",
        [
            _finding("f-good", GOOD_IDS),
            _finding("f-fake", ["village:chat:m0001", "village:chat:does-not-exist", "nonsense"]),
            "this is not json",
            "",
            _finding("f-noev", []),
            "[1, 2]",
        ],
    )
    res = lib.check_findings(bad, store_path)
    assert res["ok"] is False and res["store_missing"] is False and res["checked"] == 3
    by_id = {p["finding_id"]: p for p in res["problems"]}
    assert set(by_id) == {"f-fake", "f-noev"}
    assert by_id["f-fake"]["line"] == 2
    assert set(by_id["f-fake"]["bad_evidence"]) == {"village:chat:does-not-exist", "nonsense"}
    assert "does not resolve" in by_id["f-fake"]["bad_evidence"]["village:chat:does-not-exist"]
    assert "Malformed" in by_id["f-fake"]["bad_evidence"]["nonsense"]
    assert by_id["f-noev"]["line"] == 5 and "evidence_ids" in by_id["f-noev"]["error"]
    assert [e["line"] for e in res["parse_errors"]] == [3, 6]


def test_cli_check_findings_exit_codes(store_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture):
    from swarm_mcp.cli import main

    good = _write_findings(tmp_path / "good.jsonl", [_finding("f-1", GOOD_IDS)])
    with pytest.raises(SystemExit) as e:
        main(["check-findings", "--findings", str(good), "--db", str(store_path)])
    assert e.value.code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    bad = _write_findings(tmp_path / "bad.jsonl", [_finding("f-2", ["village:chat:does-not-exist"])])
    with pytest.raises(SystemExit) as e:
        main(["check-findings", "--findings", str(bad), "--db", str(store_path)])
    assert e.value.code == 1
    assert "village:chat:does-not-exist" in json.loads(capsys.readouterr().out)["problems"][0]["bad_evidence"]


# --------------------------------------------------------------------------- hooks


def _hook_env(root: Path, store_path: Path, fdir: Path) -> dict[str, str]:
    """A project at ``root`` whose swarm.toml points at the store and findings dir."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "swarm.toml").write_text(f'[data]\ndb = "{store_path.as_posix()}"\nfindings = "{fdir.as_posix()}"\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM", "CLAUDE_"))}
    env.update(CLAUDE_PROJECT_DIR=str(root))
    return env


def _run(script: Path, stdin: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)], input=stdin, env=env, capture_output=True, text=True, timeout=60
    )


def _hook_json(event: str, **extra) -> str:
    base = {
        "session_id": "sess-1",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp",
        "permission_mode": "default",
        "hook_event_name": event,
    }
    return json.dumps({**base, **extra})


def test_audit_log_hook(tmp_path: Path, store_path: Path):
    fdir = tmp_path / "audit-findings"
    env = _hook_env(tmp_path, store_path, fdir)
    response = [{"type": "text", "text": '{"total_matches": 1}'}]
    payload = _hook_json(
        "PostToolUse",
        tool_name="mcp__swarm__scope_search",
        tool_input={"query": "charity", "limit": 5},
        tool_use_id="toolu_01",
        tool_response=response,
    )
    r = _run(AUDIT_HOOK, payload, env)
    assert r.returncode == 0 and r.stdout == ""
    lines = _lines(fdir / "audit.jsonl")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    canon = json.dumps(response, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert entry["result_sha256"] == hashlib.sha256(canon.encode()).hexdigest()
    assert entry["result_chars"] == len(canon)
    assert entry["tool"] == "mcp__swarm__scope_search" and entry["args"] == {"query": "charity", "limit": 5}
    assert entry["session_id"] == "sess-1" and entry["tool_use_id"] == "toolu_01" and entry["ts"].endswith("Z")
    assert "is_error" not in entry

    # dict and string responses; an explicit error flag is recorded
    err_payload = json.loads(payload) | {"tool_response": {"isError": True, "content": []}}
    assert _run(AUDIT_HOOK, json.dumps(err_payload), env).returncode == 0
    str_payload = json.loads(payload) | {"tool_response": "plain text"}
    assert _run(AUDIT_HOOK, json.dumps(str_payload), env).returncode == 0
    entries = [json.loads(x) for x in _lines(fdir / "audit.jsonl")]
    assert entries[1]["is_error"] is True and "is_error" not in entries[2]

    # other tools are ignored; garbage never fails
    bash = _hook_json("PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_response={"stdout": ""})
    for stdin in (bash, "garbage {{{", "", "[1,2,3]"):
        r = _run(AUDIT_HOOK, stdin, env)
        assert r.returncode == 0 and r.stdout == ""
    assert len(_lines(fdir / "audit.jsonl")) == 3

    # an unwritable findings dir still exits 0 (diagnostic on stderr only)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a dir")
    r = _run(AUDIT_HOOK, payload, _hook_env(tmp_path / "blocked", store_path, blocker / "sub"))
    assert r.returncode == 0 and r.stdout == "" and "audit_log hook" in r.stderr


def _block_reason(r: subprocess.CompletedProcess) -> str:
    """The reason of a Stop-hook block (JSON decision on stdout, exit 0)."""
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["decision"] == "block"
    return out["reason"]


needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="the Stop hook runs its check through uv")


@needs_uv
def test_require_evidence_hook(tmp_path: Path, store_path: Path):
    fdir = tmp_path / "stop-findings"
    env = _hook_env(tmp_path, store_path, fdir)
    stop = _hook_json("Stop", stop_hook_active=False, last_assistant_message="done")
    stop_again = _hook_json("Stop", stop_hook_active=True, last_assistant_message="done")

    r = _run(STOP_HOOK, stop, env)  # no findings.jsonl yet
    assert r.returncode == 0 and r.stdout == ""

    ffile = _write_findings(fdir / "findings.jsonl", [_finding("f-ok", GOOD_IDS)])
    r = _run(STOP_HOOK, stop, env)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""

    _write_findings(ffile, [_finding("f-ok", GOOD_IDS), _finding("f-fake", ["village:chat:does-not-exist"]), "oops"])
    reason = _block_reason(_run(STOP_HOOK, stop, env))
    assert "f-fake" in reason and "line 2" in reason and "village:chat:does-not-exist" in reason
    assert "line 3" in reason and "corrupt" in reason and "findings_record" in reason

    r = _run(STOP_HOOK, stop_again, env)  # loop guard
    assert r.returncode == 0 and r.stdout == "" and "stop_hook_active" in r.stderr

    nostore = _hook_env(tmp_path / "nostore", tmp_path / "no-store.duckdb", fdir)
    r = _run(STOP_HOOK, stop, nostore)
    assert r.returncode == 0 and r.stdout == "" and "not found" in r.stderr

    for garbage in ("not json", "", "[1, 2]"):  # stop_hook_active unknown: never block
        r = _run(STOP_HOOK, garbage, env)
        assert r.returncode == 0 and r.stdout == "" and "not a JSON object" in r.stderr

    r = _run(STOP_HOOK, stop, {**env, "PATH": ""})  # no uv: allow, with a note
    assert r.returncode == 0 and r.stdout == "" and "uv not found" in r.stderr


def _settings_command(event: str) -> str:
    settings = json.loads((REPO / ".claude" / "settings.json").read_text())
    return settings["hooks"][event][0]["hooks"][0]["command"]


def _run_command(command: str, stdin: str, project: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM", "CLAUDE_"))}
    env["CLAUDE_PROJECT_DIR"] = str(project)
    return subprocess.run(["sh", "-c", command], input=stdin, env=env, capture_output=True, text=True, timeout=60)


@needs_uv
def test_stop_hook_never_blocks_on_broken_infrastructure(tmp_path: Path, store_path: Path):
    """Regression: a merge-conflicted pyproject.toml made `uv run` exit 2, which Claude Code read as
    "block the stop" even with stop_hook_active set, so the session could never end."""
    project = tmp_path / "project"
    (project / "hooks").mkdir(parents=True)
    (project / "swarm_mcp").mkdir()
    shutil.copy(STOP_HOOK, project / "hooks" / STOP_HOOK.name)
    shutil.copy(AUDIT_HOOK, project / "hooks" / AUDIT_HOOK.name)
    (project / "swarm_mcp" / "pyproject.toml").write_text(
        '<<<<<<< HEAD\n[project]\nname = "a"\n=======\n[project]\nname = "b"\n>>>>>>> other\n'
    )
    fdir = tmp_path / "f"
    _hook_env(project, store_path, fdir)
    _write_findings(fdir / "findings.jsonl", [_finding("f-fake", ["village:chat:does-not-exist"])])
    stop_cmd = _settings_command("Stop")

    for active in (False, True):
        r = _run_command(stop_cmd, _hook_json("Stop", stop_hook_active=active), project)
        assert r.returncode == 0 and r.stdout == "", (active, r.returncode, r.stdout, r.stderr)
    assert "could not run" in _run_command(stop_cmd, _hook_json("Stop", stop_hook_active=False), project).stderr

    # the hook script itself is missing: python3 exits 2, the command maps it to a non-blocking 1
    (project / "hooks" / STOP_HOOK.name).unlink()
    r = _run_command(stop_cmd, _hook_json("Stop", stop_hook_active=False), project)
    assert r.returncode == 1 and r.stdout == ""

    # PostToolUse: a missing or broken audit script never blocks and never errors
    audit_cmd = _settings_command("PostToolUse")
    payload = _hook_json("PostToolUse", tool_name="mcp__swarm__scope_search", tool_input={}, tool_response=[])
    (project / "hooks" / AUDIT_HOOK.name).write_text("<<<<<<< HEAD\nsyntax error\n")
    assert _run_command(audit_cmd, payload, project).returncode == 0
    (project / "hooks" / AUDIT_HOOK.name).unlink()
    assert _run_command(audit_cmd, payload, project).returncode == 0
