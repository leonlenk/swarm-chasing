#!/usr/bin/env python3
"""Stop hook: refuse to end the turn while a recorded finding cites evidence ids that don't resolve.

Registered in .claude/settings.json and run with plain ``python3`` (this entry point is stdlib only):

    python3 "$CLAUDE_PROJECT_DIR/hooks/require_evidence.py" || exit 1

It works in two stages so that nothing outside the evidence check itself can block a stop:

1. Hook (this process). Reads the hook payload from stdin. If ``stop_hook_active`` is true
   (Claude is already continuing because of an earlier block), or the payload is not a JSON
   object, it allows the stop at once. Otherwise it runs stage 2 in the swarm_mcp env:

       uv run --project <this repo>/swarm_mcp --frozen --quiet python <this file> --check

2. Check (``--check``, imports swarm_mcp). Runs ``swarm_mcp.scope.findings.check_findings`` on
   ``<findings dir>/findings.jsonl`` against the SwarmScope store (read-only) and prints one
   result line on stdout.

The hook blocks only when stage 2 returns a well-formed result that names bad findings. It
blocks with Claude Code's JSON output, ``{"decision": "block", "reason": ...}`` on stdout, and
exit code 0. It never exits 2. Anything unexpected allows the stop with a note on stderr: uv
missing or failing to start (an unparseable or merge-conflicted pyproject.toml, a lock or
network error), an import error, a timeout, or garbled output. The ``|| exit 1`` in
settings.json turns a failure to start python3 or to open this file (exit 2) into a
non-blocking error.

Project root: $CLAUDE_PROJECT_DIR, else <this script's dir>/... The findings dir and the store
come from ``swarm_mcp.config.Config.load(cwd=<project root>)``: ``[data] findings`` and
``[data] db`` in <project root>/swarm.toml, defaulting to <project root>/findings and
<data dir>/swarmscope.duckdb (SWARM_DATA_DIR is honoured).

Which findings count: the last line per finding_id, minus rejected or retracted ones
(``check_findings``). A finding that the audit log (``audit.jsonl``, written by
``audit_log.py``) ties to a different session than the payload's ``session_id`` never blocks
this session; it is reported on stderr instead. Findings with no session on record, and
corrupt lines, still block.

The stop is allowed (stdout empty) when all findings resolve, findings.jsonl is missing or
empty, or the store is missing (warning on stderr).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MAX_LISTED = 20  # cap on problems spelled out in the block reason
CHECK_TIMEOUT = 45  # seconds for stage 2; settings.json gives the whole hook 60
RESULT_PREFIX = "@@require_evidence-result@@ "
SWARM_MCP_DIR = Path(__file__).resolve().parent.parent / "swarm_mcp"


def project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent


def note(msg: str) -> None:
    try:
        print(f"require_evidence hook: {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001 - a closed stderr must not fail the hook
        pass


def explain(result: dict) -> str:
    lines = [
        f"SwarmScope findings check failed: {result.get('message', '')}",
        f"File: {result.get('findings_file')}  Store: {result.get('db_path')}",
    ]
    problems = result.get("problems") or []
    for p in problems[:MAX_LISTED]:
        lines.append(f"- finding {p.get('finding_id')} (line {p.get('line')}):")
        if p.get("error"):
            lines.append(f"    {p['error']}")
        for eid, reason in (p.get("bad_evidence") or {}).items():
            lines.append(f"    evidence id {eid!r}: {reason}")
    for e in (result.get("parse_errors") or [])[:MAX_LISTED]:
        lines.append(f"- line {e.get('line')}: corrupt ({e.get('error')})")
    hidden = max(0, len(problems) - MAX_LISTED) + max(0, len(result.get("parse_errors") or []) - MAX_LISTED)
    if hidden:
        lines.append(f"- ... and {hidden} more (run `swarm-mcp info` for the list)")
    lines += [
        "How to fix:",
        (
            "  - For a finding with bad evidence ids: re-run findings_record with ids copied exactly from "
            "tool results (e.g. the evidence_id field of search results; drop any id you cannot find), "
            "then delete the old line from findings.jsonl."
        ),
        "  - For a corrupt line: fix it so it is one JSON object per line, or remove it.",
        "  - Verify with: uv run --directory swarm_mcp swarm-mcp info",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- stage 2: the check


def finding_sessions(audit_file: Path) -> dict[str, str]:
    """finding_id -> session_id, from the ``finding_ids`` of audit.jsonl entries."""
    owners: dict[str, str] = {}
    try:
        with open(audit_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"finding_ids"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                sid = entry.get("session_id") if isinstance(entry, dict) else None
                if isinstance(sid, str) and sid:
                    for fid in entry.get("finding_ids") or []:
                        owners[str(fid)] = sid
    except OSError:
        pass
    return owners


def check_main() -> int:
    """Run in the swarm_mcp env: print one ``RESULT_PREFIX + json`` line and return 0."""
    from swarm_mcp.config import Config
    from swarm_mcp.scope.findings import check_findings

    payload = read_payload() or {}
    config = Config.load(cwd=project_root())
    ffile = config.findings_path / "findings.jsonl"
    if ffile.exists():
        result = check_findings(ffile, config.store_path)
    else:
        result = {"ok": True, "message": f"No findings to check ({ffile} does not exist).", "findings_file": str(ffile)}
    session = payload.get("session_id")
    if result.get("problems") and isinstance(session, str) and session:
        owners = finding_sessions(config.findings_path / "audit.jsonl")
        mine = []
        for p in result["problems"]:
            owner = owners.get(str(p.get("finding_id")))
            if owner and owner != session:
                result.setdefault("other_sessions", []).append(p.get("finding_id"))
            else:
                mine.append(p)
        result["problems"] = mine
        if result.get("other_sessions"):
            n_bad = len(mine) + len(result.get("parse_errors") or [])
            result["message"] = f"{ffile}: {n_bad} problem(s) in findings from this session or with no session on record."
    sys.stdout.write(RESULT_PREFIX + json.dumps(result, default=str) + "\n")
    sys.stdout.flush()
    return 0


# --------------------------------------------------------------------------- stage 1: the hook


def read_payload() -> dict | None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _tail(text: str, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else "..." + text[-limit:]


def run_check(payload: dict) -> dict | None:
    """Stage 2 in a subprocess. The parsed result, or None (after a note) if it could not run."""
    uv = shutil.which("uv")
    if uv is None:
        note("uv not found on PATH; findings were not checked. Allowing the stop.")
        return None
    script = str(Path(__file__).resolve())
    cmd = [uv, "run", "--project", str(SWARM_MCP_DIR), "--frozen", "--quiet", "python", script, "--check"]
    try:
        proc = subprocess.run(cmd, input=json.dumps(payload), capture_output=True, text=True, timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        note(f"the findings check timed out after {CHECK_TIMEOUT}s. Allowing the stop.")
        return None
    except Exception as e:  # noqa: BLE001
        note(f"could not start the findings check ({type(e).__name__}: {e}). Allowing the stop.")
        return None
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith(RESULT_PREFIX)]
    if proc.returncode != 0 or not lines:
        note(
            f"the findings check could not run (exit {proc.returncode}: {_tail(proc.stderr) or 'no output'}). "
            "Findings were not checked. Allowing the stop."
        )
        return None
    try:
        result = json.loads(lines[-1][len(RESULT_PREFIX) :])
    except ValueError:
        result = None
    if not isinstance(result, dict):
        note("the findings check returned garbled output. Allowing the stop.")
        return None
    return result


def hook_main() -> None:
    payload = read_payload()
    if payload is None:
        note("the hook payload is not a JSON object, so stop_hook_active is unknown. Allowing the stop.")
        return
    if payload.get("stop_hook_active"):  # Claude is already continuing because of an earlier block
        note("stop_hook_active is set; not checking or blocking again.")
        return
    result = run_check(payload)
    if result is None or result.get("ok"):
        return
    if result.get("store_missing"):
        note(f"{result.get('message')} Findings in {result.get('findings_file')} were not verified; allowing the stop.")
        return
    others = result.get("other_sessions") or []
    if others:
        note(
            f"{len(others)} finding(s) recorded in other sessions cite evidence that doesn't resolve "
            f"({', '.join(map(str, others[:5]))}); not blocking this session. Run `swarm-mcp info`."
        )
        if not (result.get("problems") or result.get("parse_errors")):
            return
    if not (result.get("problems") or result.get("parse_errors")):
        note(f"the findings check failed without naming a finding ({result.get('message')}). Allowing the stop.")
        return
    sys.stdout.write(json.dumps({"decision": "block", "reason": explain(result)}) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        sys.exit(check_main())
    try:
        hook_main()
    except BaseException as e:  # noqa: BLE001 - even KeyboardInterrupt/SystemExit must not block the stop
        note(f"unexpected error ({type(e).__name__}: {e}). Allowing the stop.")
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)
