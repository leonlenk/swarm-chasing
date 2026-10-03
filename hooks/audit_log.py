#!/usr/bin/env python3
"""PostToolUse hook: append one audit line per swarm MCP tool call.

Registered in .claude/settings.json for matcher ``mcp__swarm__.*``. For every
tool named ``mcp__swarm__*`` it appends to ``<findings dir>/audit.jsonl``:

    {ts, session_id, tool_use_id, tool, args, result_sha256, result_chars, is_error?}

``result_sha256`` is the sha256 of the canonical JSON (sort_keys, compact
separators) of ``tool_response`` and ``result_chars`` is that JSON's length, so
the log proves what a tool returned without storing dataset text.
``is_error`` is only present when the response carries an explicit flag.

Findings dir: $SWARMSCOPE_FINDINGS_DIR, else $CLAUDE_PROJECT_DIR/findings,
else <this script's dir>/../findings.

Stdlib only. It must never break a tool call: every error is caught, stdout
stays empty, diagnostics go to stderr, and the exit code is always 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "mcp__swarm__"


def project_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent


def findings_dir() -> Path:
    raw = os.environ.get("SWARMSCOPE_FINDINGS_DIR")
    if raw:
        p = Path(raw).expanduser()
        if p.is_absolute():
            return p
        # same rule as swarm_mcp.config.resolve_data_dir: cwd if it exists there, else the project root
        return (Path.cwd() / p) if (Path.cwd() / p).exists() else project_root() / p
    return project_root() / "findings"


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def detect_error(resp: object) -> bool | None:
    """True/False only when the response says so explicitly; None when unknown."""
    if isinstance(resp, dict):
        for key in ("isError", "is_error"):
            if isinstance(resp.get(key), bool):
                return resp[key]
        return None
    if isinstance(resp, list):
        flags = [detect_error(b) for b in resp if isinstance(b, dict)]
        flags = [f for f in flags if f is not None]
        return any(flags) if flags else None
    return None


def build_entry(data: dict) -> dict | None:
    tool = data.get("tool_name")
    if not isinstance(tool, str) or not tool.startswith(PREFIX):
        return None
    resp = data.get("tool_response")
    blob = canonical(resp)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "session_id": data.get("session_id"),
        "tool_use_id": data.get("tool_use_id"),
        "tool": tool,
        "args": data.get("tool_input"),
        "result_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
        "result_chars": len(blob),
    }
    err = detect_error(resp)
    if err is not None:
        entry["is_error"] = err
    return entry


def append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        try:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except Exception:  # noqa: BLE001 - no locking available; O_APPEND still keeps lines whole
            pass
        f.write(line + "\n")
        f.flush()


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else None
        if not isinstance(data, dict):
            return 0
        entry = build_entry(data)
        if entry is None:
            return 0
        append(findings_dir() / "audit.jsonl", canonical(entry))
    except Exception as e:  # noqa: BLE001 - never break a tool call
        try:
            print(f"audit_log hook: {type(e).__name__}: {e}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001 - even KeyboardInterrupt/SystemExit must not fail the tool call
        pass
    finally:
        try:
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)
