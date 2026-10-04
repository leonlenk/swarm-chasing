"""Local HTTP collector: receives Claude Code hook events and writes them to the store.

Listens on 127.0.0.1 only. Hook POSTs are queued and processed by one worker thread, so a hook call returns
immediately and never slows the agent down. This is a separate process from the MCP server (which is a stdio
child of each Claude Code session); both use the same SQLite file.

Endpoints: ``POST /hook`` (a hook payload), ``GET /health``.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from swarm_mcp.modules.live import procs
from swarm_mcp.modules.live.ingest import Ingestor
from swarm_mcp.modules.live.store import Store, default_db

DEFAULT_PORT = int(os.environ.get("SWARM_LIVE_PORT", "47831"))
HEALTH_APP = "swarm-live"
EXIT_GRACE = 60  # seconds with no live session before an auto-started collector exits
UNKNOWN_PID_IDLE = 1800  # a session whose Claude Code PID is unknown counts as live until this long without events


class App:
    def __init__(self, db):
        self.store = Store(db)
        self.ing = Ingestor(self.store)
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.live: dict[str, int | None] = {}  # session_id -> Claude Code PID (None if unknown), for open sessions
        self.last_event = time.time()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        while True:
            payload, ts = self.q.get()
            try:
                self.ing.handle_hook(payload, ts)
            except Exception as e:  # never die on one bad event
                print("ingest error:", repr(e), flush=True)
            self.q.task_done()

    def track(self, p, pid=None):
        """Keep the set of live sessions up to date from one hook payload."""
        sid = p.get("session_id")
        with self.lock:
            self.last_event = time.time()
            if not sid:
                return
            if p.get("hook_event_name") == "SessionEnd":
                self.live.pop(sid, None)
            elif pid:
                self.live[sid] = pid
            else:
                self.live.setdefault(sid, None)

    def busy(self):
        """True while any session is live (its Claude Code process still runs)."""
        with self.lock:
            for sid, pid in list(self.live.items()):
                if pid and not procs.alive(pid):
                    del self.live[sid]  # Claude Code exited without SessionEnd (window closed, crash, kill)
            recent = time.time() - self.last_event < UNKNOWN_PID_IDLE
            return any(pid or recent for pid in self.live.values())

    def exit_when_idle(self, srv):
        quiet_since = None
        while True:
            time.sleep(10)
            if self.busy():
                quiet_since = None
                continue
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since >= EXIT_GRACE:
                print("swarm-live: no live sessions; exiting", flush=True)
                self.q.join()  # finish ingesting queued events first
                srv.shutdown()
                return


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                return json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return {}

        def do_POST(self):
            if self.path.split("?")[0] == "/hook":
                p = self._body()
                ts = p.pop("_swarm_live_ts", None) or time.time()  # replays may set the event time
                app.track(p, p.pop("_swarm_live_pid", None))  # the PID comes from launch.py on SessionStart
                app.q.put((p, ts))
                return self._send(200)  # empty 2xx = success, no effect on the agent
            self._send(404)

        def do_GET(self):
            if self.path.split("?")[0] == "/health":
                body = {"ok": True, "app": HEALTH_APP, "db": app.store.path}
                return self._send(200, json.dumps(body).encode("utf-8"))
            self._send(404)

    return H


def serve(port=DEFAULT_PORT, db=None, quiet=False, exit_when_idle=False):
    """exit_when_idle (used when launch.py auto-starts the collector): stop once no session is live."""
    app = App(db or default_db())
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    srv.daemon_threads = True
    if not quiet:
        print(f"swarm-live: collecting on http://127.0.0.1:{port}/hook  db {app.store.path}", flush=True)
    if exit_when_idle:
        threading.Thread(target=app.exit_when_idle, args=(srv,), daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
