"""DuckDB connection and query helpers for the SwarmScope store.

Path: ``[data] db`` in swarm.toml, or ``<data dir>/swarmscope.duckdb`` (default
``data/swarmscope.duckdb``); see ``Config.store_path``.

Connections are short-lived on purpose. DuckDB lets one process hold a
read-write handle *or* many processes hold read-only handles, so the MCP
server opens a connection per tool call (read-only unless it writes a finding)
and closes it straight away. That keeps the CLI (ingest/render/check-findings)
and the Stop hook usable while the server is running. Lock conflicts are
retried briefly.
"""

from __future__ import annotations

import contextlib
import difflib
import re
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

import duckdb

from swarm_mcp.scope import schema
from swarm_mcp.toolkit import ToolInputError

HUMAN = "human"
_HUMAN_WORDS = {"human", "humans", "user", "users"}


class StoreMissing(ToolInputError):
    """The DuckDB store does not exist yet."""


def missing_store_hint(path: Path) -> str:
    return f"SwarmScope store not found at {path}. Build it with: swarm-mcp add data/ai-village"


def _is_lock_error(e: Exception) -> bool:
    """Transient contention: another process holds the file lock, or (same process) another
    connection is open with a different read_only setting. Both clear when that connection closes."""
    msg = str(e).lower()
    if "different configuration" in msg:
        return True
    return "lock" in msg and ("conflicting" in msg or "could not set" in msg)


def open_connection(path: Path, *, read_only: bool = True, timeout: float = 10.0) -> duckdb.DuckDBPyConnection:
    """Open the store, retrying on lock conflicts for up to ``timeout`` seconds.

    Read-only opens of a missing file raise ``StoreMissing``. Read-write opens
    create the file and the schema.
    """
    path = Path(path)
    if read_only and not path.exists():
        raise StoreMissing(missing_store_hint(path))
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    delay = 0.05
    while True:
        try:
            con = duckdb.connect(str(path), read_only=read_only)
            break
        except duckdb.Error as e:
            if not _is_lock_error(e) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    if not read_only:
        for stmt in schema.ddl():
            con.execute(stmt)
    return con


class Store:
    """A thin wrapper around a DuckDB connection with dict-returning helpers."""

    def __init__(self, con: duckdb.DuckDBPyConnection, path: Path | None = None):
        self.con = con
        self.path = path

    # -- raw queries ---------------------------------------------------------
    def all(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        cur = self.con.execute(sql, list(params or []))
        cols = [d[0] for d in cur.description or []]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.con.execute(sql, list(params or [])).fetchone()
        return row[0] if row else None

    def has_table(self, table: str) -> bool:
        return bool(self.scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]))

    # -- agents --------------------------------------------------------------
    def agents(self, source: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT agent_id, source, display_name, aliases FROM agents"
        return self.all(
            sql + (" WHERE source = ?" if source else "") + " ORDER BY display_name", [source] if source else []
        )

    def resolve_agent(self, query: str, source: str | None = None) -> dict[str, Any]:
        """Find one agent by agent_id, display name or alias (case/space/hyphen-insensitive).

        Raises ToolInputError with suggestions when unknown or ambiguous."""
        q = (query or "").strip().lstrip("@")
        if not q:
            raise ToolInputError("agent must not be empty")
        rows = self.agents(source)
        key = norm(q)
        exact = [r for r in rows if r["agent_id"] == q or q.endswith(":" + r["agent_id"].rsplit(":", 1)[-1])]
        if not exact:
            exact = [r for r in rows if norm(r["display_name"]) == key]
        if not exact:
            exact = [r for r in rows if any(norm(a) == key for a in (r["aliases"] or []))]
        if not exact:
            exact = [r for r in rows if key and key in norm(r["display_name"])]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            names = ", ".join(sorted(r["display_name"] for r in exact))
            raise ToolInputError(f"agent {query!r} is ambiguous; did you mean one of: {names}?")
        close = difflib.get_close_matches(q, [r["display_name"] for r in rows], n=5, cutoff=0.4)
        hint = f" Close matches: {', '.join(close)}." if close else ""
        raise ToolInputError(f"Unknown agent {query!r}.{hint} Use scope_agents to list agent names.")

    def author_filter(self, query: str | None, source: str | None = None) -> tuple[str, list[Any], str] | None:
        """SQL predicate on ``author_id`` for an author query: an agent name/alias/id,
        'human' (all humans) or a 'human:<id>' author id. Returns (sql, params, label)."""
        if query is None or not query.strip():
            return None
        q = query.strip()
        if q.lower() in _HUMAN_WORDS:
            return "author_id LIKE 'human:%'", [], HUMAN
        if q.startswith("human:"):
            return "author_id = ?", [q], q
        a = self.resolve_agent(q, source)
        return "author_id = ?", [a["agent_id"]], a["display_name"]

    def display_names(self) -> dict[str, str]:
        """agent_id -> display name (humans are rendered as 'human:<first 8 chars>')."""
        return {r["agent_id"]: r["display_name"] for r in self.all("SELECT agent_id, display_name FROM agents")}

    # -- channels ------------------------------------------------------------
    def resolve_channel(self, channel: str | None, source: str | None = None) -> str | None:
        if channel is None or not channel.strip():
            return None
        q = channel.strip().lstrip("#")
        sql = "SELECT DISTINCT channel FROM messages WHERE channel IS NOT NULL" + (" AND source = ?" if source else "")
        names = [r["channel"] for r in self.all(sql, [source] if source else [])]
        for n in names:
            if n == q or n.lower() == q.lower():
                return n
        raise ToolInputError(f"Unknown channel {channel!r}. Channels: {', '.join(sorted(names))}")


@contextlib.contextmanager
def connect(path: Path, *, read_only: bool = True, timeout: float = 10.0) -> Iterator[Store]:
    """``with connect(path) as store: ...`` - opens, yields a Store, always closes."""
    con = open_connection(path, read_only=read_only, timeout=timeout)
    try:
        yield Store(con, Path(path))
    finally:
        con.close()


def norm(s: str) -> str:
    """Normalize a name for lookup: lowercase, drop spaces/hyphens/underscores/brackets."""
    return re.sub(r"[\s\-_\[\]()]+", "", (s or "").lower())


def label_for(author_id: str | None, names: dict[str, str]) -> str:
    """Human-friendly label for an author id."""
    if not author_id:
        return "?"
    if author_id in names:
        return names[author_id]
    if author_id.startswith("human:"):
        return "human:" + author_id[6:14]
    return author_id


def date_filters(column: str, since: str | None, until: str | None) -> tuple[list[str], list[Any]]:
    """WHERE fragments for an already-parsed (toolkit.parse_time) [since, until) window."""
    where: list[str] = []
    params: list[Any] = []
    if since:
        where.append(f"{column} >= CAST(? AS TIMESTAMP)")
        params.append(since)
    if until:
        where.append(f"{column} < CAST(? AS TIMESTAMP)")
        params.append(until)
    return where, params
