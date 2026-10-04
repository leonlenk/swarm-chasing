"""SessionStart hook: start the swarm-live collector in the background if it isn't running, then forward the
SessionStart event to it. Standard library only and run by path (not as part of the package), so it works before
uv has built the project environment. Prints nothing: SessionStart hook output would be added to Claude's context.

The collector is started with ``uv run --project <swarm_mcp> swarm-live serve --exit-when-idle``. It is told
which Claude Code process each session runs in, so it exits by itself about a minute after the last session ends.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("SWARM_LIVE_PORT", "47831"))
HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[3]  # .../swarm_mcp (live -> modules -> swarm_mcp -> src -> project)

_spec = importlib.util.spec_from_file_location("swarm_live_procs", HERE / "procs.py")
procs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(procs)


def running():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=0.7) as r:
            return b"swarm-live" in r.read()
    except Exception:
        return False


def find_uv():
    exe = shutil.which("uv")
    if exe:
        return exe
    home = Path.home()
    for p in (home / ".local" / "bin", home / ".cargo" / "bin"):
        for name in ("uv.exe", "uv"):
            if (p / name).exists():
                return str(p / name)
    return None


def start():
    """Spawn the collector; False if it can't be started (no uv)."""
    data = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".swarm-live")
    os.makedirs(data, exist_ok=True)
    log = open(os.path.join(data, "collector.log"), "ab")
    uv = find_uv()
    if not uv:
        log.write(b"swarm-live: uv not found on PATH; the collector can't start\n")
        return False
    env = {**os.environ, "CLAUDE_PLUGIN_DATA": data, "SWARM_LIVE_PORT": str(PORT)}
    if os.environ.get("CLAUDE_PLUGIN_DATA"):  # same environment the plugin's MCP server uses, kept across updates
        env.setdefault("UV_PROJECT_ENVIRONMENT", os.path.join(data, "venv"))
    cmd = [
        uv,
        "run",
        "--quiet",
        "--project",
        str(PROJECT),
        "swarm-live",
        "serve",
        "--port",
        str(PORT),
        "--exit-when-idle",
    ]
    kw = dict(cwd=str(PROJECT), stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env)
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW (children share the hidden console)
        try:  # escape the hook's job object so the collector outlives the hook process
            subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(cmd, creationflags=flags, **kw)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kw)
    return True


# Waits are bounded by wall-clock deadlines, not retry counts: on Windows a refused connection takes ~2 s to fail,
# and the whole script must finish inside the hook's 20 s timeout.
DEADLINE = time.monotonic() + 17


def forward(raw, budget=3.0):
    """Record the SessionStart event itself (SessionStart can't use an http hook, so this script relays it)."""
    stop = min(time.monotonic() + budget, DEADLINE)
    while time.monotonic() < stop:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/hook", data=raw, method="POST", headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=1).read()
            return True
        except Exception:
            time.sleep(0.25)
    return False


def ensure_running(budget=12.0):
    """Start the collector if needed and wait for it. The first run may also build the uv environment, which can
    take longer than the budget; the collector still comes up and records the session's later events."""
    if running():
        return True
    if not start():
        return False
    stop = min(time.monotonic() + budget, DEADLINE)
    while time.monotonic() < stop:
        if running():
            return True
        time.sleep(0.15)
    return False


if __name__ == "__main__":
    payload = b""
    if not sys.stdin.isatty():
        try:
            payload = sys.stdin.buffer.read()
        except Exception:
            pass
    try:
        p = json.loads(payload.decode("utf-8"))
        p["_swarm_live_pid"] = procs.claude_pid()
        payload = json.dumps(p).encode("utf-8")
    except Exception:
        pass
    if ensure_running() and payload.strip() and not forward(payload):
        # the collector may have been shutting itself down just as this session started
        if ensure_running(budget=3.0):
            forward(payload)
