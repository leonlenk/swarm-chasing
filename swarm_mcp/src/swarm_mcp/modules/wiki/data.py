"""Load a wiki edit-history database (the collusion.wiki explorer SQLite schema) into memory.

Expected tables: ``pages``, ``revisions`` (one row per stored revision with full body, editor ``label``,
IPv4 /16 ``ip16`` and timestamps), ``revision_hunks`` (line-level diff to the previous revision) and,
optionally, ``page_profiles`` (the publishers' heuristic ``page_family`` per page), ``labels`` and
``database_metadata``. Any ``<data>/<name>/*.db`` with these tables is picked up.

What each revision *added* is kept (from the hunks), because a wiki body repeats everything before it:
searching or comparing full bodies would match the same old text on every later revision.

Identity: the editor label is self-chosen and may be shared by many agent instances, and the same
instance may use many labels. ``actor`` is the label, or ``anon@<ip16>`` for blank labels; nothing here
claims a label is one agent.

Sessions: an actor's consecutive revisions with gaps of at most ``SESSION_GAP`` (any pages). They are the
work units used by ``subtasks``; a session id is its first revision id.
"""

from __future__ import annotations

import collections
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from swarm_mcp.toolkit import TS_FORMAT

log = logging.getLogger("swarm_mcp.wiki")

SESSION_GAP = 30 * 60  # seconds
REQUIRED_TABLES = {"pages", "revisions", "revision_hunks"}


def find_wikis(data_dir: Path, override: str | None = None) -> dict[str, Path]:
    """``<data>/<name>/*.db`` files with the expected tables, keyed by folder name (or file stem)."""
    files = [Path(override).expanduser()] if override else sorted(data_dir.glob("*/*.db"))
    out: dict[str, Path] = {}
    for f in files:
        if not f.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            names = {r[0] for r in con.execute("select name from sqlite_master where type in ('table','view')")}
            con.close()
        except sqlite3.DatabaseError:
            continue
        if REQUIRED_TABLES <= names:
            name = f.parent.name if f.parent != data_dir else f.stem
            out.setdefault(name, f)
    return out


def _ts(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).strftime(TS_FORMAT)


def _secs(ts: str) -> float:
    return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc).timestamp()


@dataclass
class Revision:
    rid: str
    page_key: str
    seq: int
    label: str
    ip16: str
    time: str  # dataset format, UTC
    time_grade: str
    uncertainty: int  # seconds
    summary: str
    action: str
    body_len: int
    created: bool  # first stored revision of its page
    added: list[str]  # lines this revision added or replaced

    @property
    def actor(self) -> str:
        return self.label or f"anon@{self.ip16}"


@dataclass
class Page:
    key: str
    wiki: str
    name: str
    family: str | None = None
    family_method: str | None = None
    family_confidence: float | None = None
    deleted_live: bool | None = None
    revisions: list[str] = field(default_factory=list)  # rids by sequence


@dataclass
class Session:
    sid: str  # first revision id
    actor: str
    revisions: list[str]
    start: str
    end: str


@dataclass
class Wiki:
    name: str
    path: Path
    pages: dict[str, Page]
    revisions: dict[str, Revision]
    order: list[str]  # rids by time
    sessions: dict[str, Session]
    session_of: dict[str, str]
    human_labels: set[str]
    metadata: dict[str, str]

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)

    def body(self, rid: str) -> str:
        con = self.connect()
        try:
            row = con.execute("select body from revisions where revision_id = ?", (rid,)).fetchone()
        finally:
            con.close()
        return row[0] if row else ""


def load_wiki(name: str, path: Path) -> Wiki:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    tables = {r[0] for r in con.execute("select name from sqlite_master")}
    pages = {k: Page(k, w, n) for k, w, n in con.execute("select page_key, wiki, name from pages")}
    if "page_profiles" in tables:
        for k, fam, meth, conf, dl in con.execute(
            "select page_key, page_family, page_family_method, page_family_confidence, deleted_live from page_profiles"
        ):
            if k in pages:
                p = pages[k]
                p.family, p.family_method, p.family_confidence, p.deleted_live = fam, meth, conf, bool(dl)
    hunks: dict[str, list[tuple[str, int, int]]] = collections.defaultdict(list)
    for rid, op, b0, b1 in con.execute(
        "select revision_id, operation, b0, b1 from revision_hunks where operation in ('insert','replace') "
        "order by revision_id, hunk_index"
    ):
        hunks[rid].append((op, b0, b1))
    revisions: dict[str, Revision] = {}
    for row in con.execute(
        "select revision_id, page_key, sequence, label, ip16, time, time_grade, uncertainty_seconds, "
        "coalesce(change_summary,''), coalesce(request_action,''), body_len, diff_base_revision_id, body "
        "from revisions"
    ):
        rid, pk, seq, label, ip16, t, grade, unc, summary, action, blen, base, body = row
        lines = body.splitlines()
        if base is None:
            added = lines
        else:
            added = [x for _, b0, b1 in hunks.get(rid, []) for x in lines[b0:b1]]
        revisions[rid] = Revision(
            rid, pk, seq, label or "", ip16, _ts(t), grade, unc, summary, action, blen, seq == 1, added
        )
        pages[pk].revisions.append(rid)
    for p in pages.values():
        p.revisions.sort(key=lambda r: revisions[r].seq)
    human = set()
    if "labels" in tables:
        human = {r[0] for r in con.execute("select label from labels where is_human_handle = 1")}
    meta = dict(con.execute("select key, value from database_metadata")) if "database_metadata" in tables else {}
    con.close()

    order = sorted(revisions, key=lambda r: (revisions[r].time, r))
    by_actor: dict[str, list[str]] = collections.defaultdict(list)
    for rid in order:
        by_actor[revisions[rid].actor].append(rid)
    sessions: dict[str, Session] = {}
    session_of: dict[str, str] = {}
    for actor, rids in by_actor.items():
        cur: list[str] = []
        for rid in rids:
            if cur and _secs(revisions[rid].time) - _secs(revisions[cur[-1]].time) > SESSION_GAP:
                sessions[cur[0]] = Session(cur[0], actor, cur, revisions[cur[0]].time, revisions[cur[-1]].time)
                cur = []
            cur.append(rid)
        if cur:
            sessions[cur[0]] = Session(cur[0], actor, cur, revisions[cur[0]].time, revisions[cur[-1]].time)
    for s in sessions.values():
        for rid in s.revisions:
            session_of[rid] = s.sid
    log.info("wiki %s: %d pages, %d revisions, %d sessions", name, len(pages), len(revisions), len(sessions))
    return Wiki(name, path, pages, revisions, order, sessions, session_of, human, meta)
