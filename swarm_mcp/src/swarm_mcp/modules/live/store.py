"""SQLite store for recorded Claude Code sessions (standard library only).

One writer (the collector's ingest worker, or ``swarm-live import``) owns a ``Store``; the MCP tools only
read, through ``connect_ro``. WAL mode lets both run at once.

Tables:
    events    every hook payload as received (id, ts, session_id, agent_key, event, payload JSON)
    agents    one row per main session or subagent; key = '<session_id>:main' or '<session_id>:<agent_id>'
    actions   one row per tool call; id = Claude Code's tool_use_id
    messages  agent text (final answers and transcript text), de-duplicated per agent
    offsets   how far each transcript file has been read
Times are Unix epoch seconds (UTC).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from urllib.parse import quote

DB_NAME = "swarm-live.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL, session_id TEXT, agent_key TEXT, event TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS agents (
  key TEXT PRIMARY KEY, session_id TEXT, agent_id TEXT, agent_type TEXT, label TEXT,
  parent_key TEXT, task TEXT, cwd TEXT, started REAL, stopped REAL, status TEXT);
CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY, agent_key TEXT, session_id TEXT, ts REAL, ts_end REAL, tool TEXT,
  input TEXT, status TEXT, output TEXT, linked_agent TEXT, cwd TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, agent_key TEXT, session_id TEXT, ts REAL, source TEXT, text TEXT, hash TEXT,
  UNIQUE(agent_key, hash));
CREATE TABLE IF NOT EXISTS offsets (path TEXT PRIMARY KEY, pos INTEGER);
CREATE INDEX IF NOT EXISTS ix_events_agent ON events(agent_key, ts);
CREATE INDEX IF NOT EXISTS ix_actions_agent ON actions(agent_key, ts);
CREATE INDEX IF NOT EXISTS ix_messages_agent ON messages(agent_key, ts);
"""


def default_db() -> Path:
    """``SWARM_LIVE_DB``, else the plugin's data dir (kept across plugin updates), else ``~/.swarm-live``."""
    if os.environ.get("SWARM_LIVE_DB"):
        return Path(os.environ["SWARM_LIVE_DB"]).expanduser()
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".swarm-live")
    return Path(base) / DB_NAME


def connect_ro(path: Path) -> sqlite3.Connection:
    """A read-only connection (raises sqlite3.OperationalError if the file doesn't exist)."""
    c = sqlite3.connect(f"file:{quote(Path(path).as_posix(), safe='/:')}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


class Store:
    def __init__(self, path: Path | str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = str(path)
        self._local = threading.local()
        c = self.conn()
        c.executescript(SCHEMA)
        c.commit()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            self._local.c = c
        return c

    def q(self, sql, args=()):
        return [dict(r) for r in self.conn().execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.conn().execute(sql, args).fetchone()
        return dict(r) if r else None

    def x(self, sql, args=()):
        cur = self.conn().execute(sql, args)
        self.conn().commit()
        return cur

    # --- agents -------------------------------------------------------------
    def ensure_agent(self, key, session_id, agent_id=None, agent_type=None, cwd=None, ts=None):
        a = self.one("SELECT * FROM agents WHERE key=?", (key,))
        if a:
            if agent_type and not a["agent_type"]:
                self.x(
                    "UPDATE agents SET agent_type=?, label=? WHERE key=?",
                    (agent_type, label_for(agent_type, agent_id, session_id), key),
                )
            if cwd and not a["cwd"]:
                self.x("UPDATE agents SET cwd=? WHERE key=?", (cwd, key))
            if ts and not a["started"]:
                self.x("UPDATE agents SET started=? WHERE key=?", (ts, key))
            return a
        self.x(
            "INSERT INTO agents(key, session_id, agent_id, agent_type, label, cwd, started, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (key, session_id, agent_id, agent_type, label_for(agent_type, agent_id, session_id), cwd, ts, "running"),
        )
        return self.one("SELECT * FROM agents WHERE key=?", (key,))


def agent_key(session_id, agent_id=None):
    return f"{session_id}:{agent_id}" if agent_id else f"{session_id}:main"


def label_for(agent_type, agent_id, session_id):
    sid = (session_id or "")[:6]
    if agent_id:
        return f"{agent_type or 'subagent'}#{agent_id[:5]} ({sid})"
    return f"{agent_type or 'main'} ({sid})"


def dumps(o):
    return json.dumps(o, ensure_ascii=False, default=str)
