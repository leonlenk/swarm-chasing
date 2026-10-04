#!/usr/bin/env python3
"""Stop hook: refuse to end the turn while a recorded finding cites evidence ids that don't resolve.

Registered in .claude/settings.json (it imports swarm_mcp, so it runs in the uv env):

    uv run --project "$CLAUDE_PROJECT_DIR/swarm_mcp" --quiet python "$CLAUDE_PROJECT_DIR/hooks/require_evidence.py"

It runs ``swarm_mcp.scope.findings.check_findings`` on ``<findings dir>/findings.jsonl``
against the SwarmScope store (read-only).

Project root: $CLAUDE_PROJECT_DIR, else <this script's dir>/... The findings dir and the
store come from ``swarm_mcp.config.Config.load(cwd=<project root>)``: ``[data] findings`` and
``[data] db`` in <project root>/swarm.toml, defaulting to <project root>/findings and
<data dir>/swarmscope.duckdb (SWARM_DATA_DIR is honoured).

Exit codes (Claude Code Stop-hook semantics):
  0  allow the stop: all findings resolve, findings.jsonl is missing/empty, the store is
     missing (warning on stderr), or ``stop_hook_active`` is true (loop guard; a one-line
     note on stderr if findings are still bad)
  2  block the stop: a finding cites an unresolvable id or a line is corrupt; stderr
     explains which lines/ids and how to fix them (Claude sees it)
  1  unexpected error (non-blocking; the error is on stderr)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MAX_LISTED = 20  # cap on problems spelled out in the stderr report


def project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent


def read_input() -> dict:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - a garbled payload just means "no loop guard info"
        return {}


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


def main() -> int:
    data = read_input()
    root = project_root()
    loop_guard = bool(data.get("stop_hook_active"))

    from swarm_mcp.config import Config
    from swarm_mcp.scope.findings import check_findings

    config = Config.load(cwd=root)
    ffile = config.findings_path / "findings.jsonl"
    if not ffile.exists():
        return 0
    db_path = config.store_path
    if loop_guard:  # Claude is already continuing because of an earlier block: never block again
        try:
            result = check_findings(ffile, db_path)
            if not result.get("ok"):
                print(
                    f"require_evidence hook: findings still have problems ({result.get('message')}); "
                    "not blocking again (stop_hook_active).",
                    file=sys.stderr,
                )
        except Exception as e:  # noqa: BLE001
            print(f"require_evidence hook: check skipped ({type(e).__name__}: {e})", file=sys.stderr)
        return 0

    result = check_findings(ffile, db_path)
    if result.get("ok"):
        return 0
    if result.get("store_missing"):
        print(
            f"require_evidence hook: {result.get('message')} Findings in {ffile} were not verified; allowing the stop.",
            file=sys.stderr,
        )
        return 0
    print(explain(result), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:  # noqa: BLE001 - unexpected: non-blocking error
        print(f"require_evidence hook error: {type(e).__name__}: {e}", file=sys.stderr)
        code = 1
    sys.exit(code)
