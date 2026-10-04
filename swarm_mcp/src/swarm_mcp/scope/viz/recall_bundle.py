"""``swarm-mcp render recall``: the SwarmScope store and the live recordings as data for RECALL (``recall/``).

RECALL is the repo's temporal debugger UI. This writes everything it shows beyond its own AI Village slices, under
``<out>/scope/`` (default ``recall/public/data/scope/``, gitignored with the rest of ``public/data``):

  index.json               every store source (adapter, counts, span, blind spots) with the files below, and the
                           recorded Claude Code sessions. RECALL polls it to follow live sessions.
  <source>.explorer.json   the explorer payload (``timeline_html.build_timeline``, the same data as
                           ``render timeline``) for each source with messages: lanes, density, mentions, periods,
                           recaps, notable moments, agent arcs and metric series
  <corpus>.subtasks.json   the subtask payload (``subtasks_html.build_subtasks``, the same data as
                           ``render subtasks``) for each source whose records touch artifacts
  live/<session>.json      one recorded Claude Code session (its main agent and subagents), read from the
                           swarm-live recordings through the ``claude_code`` adapter. Not from the store: no DuckDB
                           lock is taken, and a session is as fresh as its recordings.

Ids are store evidence ids (``claude-code:msg:...``, ``village:msg:...``), so whatever RECALL shows can be cited with
``findings_record`` and re-read with ``core_get``. Text is masked with the ``Scrubber`` and capped, and is untrusted
agent output: RECALL renders it as text, never as markup.

``watch`` rewrites the live sessions (and ``index.json``) whenever the recordings change; the store files are
written once.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from swarm_mcp.toolkit import Scrubber

INDEX_VERSION = 1
SESSION_VERSION = 1
TEXT_CHARS = 4_000  # a message's text in a session file
OUTPUT_CHARS = 4_000  # a tool call's output (the recorder keeps at most 4,000)
COMMAND_CHARS = 1_500  # a tool call's command / summary (the recorder trims inputs to 1,500 per string)
RUNNING_SECS = 180  # a session counts as running when it recorded something this recently and has not stopped


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(v: Any) -> str | None:
    """Store datetimes are naive UTC; emit ISO 8601 with Z."""
    if v is None:
        return None
    if isinstance(v, datetime):
        d = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return str(v)


def _write_json(path: Path, obj: Any) -> int:
    """Write atomically (temp file + rename), so a polling reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)
    return len(data.encode("utf-8"))


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)


# --------------------------------------------------------------------------- store sources


def store_sources(db_path: Path) -> list[dict[str, Any]]:
    """One entry per ingested source: adapter, counts, time span and blind spots (as ``swarm-mcp info``)."""
    from swarm_mcp.scope import db

    with db.connect(db_path) as s:
        if not s.has_table("sources"):
            return []
        out = []
        for r in s.all("SELECT source, adapter, ingested_at, counts, meta FROM sources ORDER BY source"):
            counts = json.loads(r["counts"]) if isinstance(r["counts"], str) else (r["counts"] or {})
            meta = json.loads(r["meta"]) if isinstance(r["meta"], str) else (r["meta"] or {})
            span = (
                s.one(
                    """SELECT min(t) AS lo, max(t) AS hi FROM (
                       SELECT ts AS t FROM messages WHERE source = ? UNION ALL
                       SELECT ts AS t FROM actions WHERE source = ?) WHERE t IS NOT NULL""",
                    [r["source"], r["source"]],
                )
                or {}
            )
            out.append(
                {
                    "source": r["source"],
                    "adapter": r["adapter"],
                    "ingestedAt": _iso(r["ingested_at"]),
                    "counts": {k: int(v) for k, v in counts.items() if isinstance(v, (int, float))},
                    "span": [_iso(span.get("lo")), _iso(span.get("hi"))],
                    "notes": [str(n) for n in (meta.get("notes") or [])][:12],
                }
            )
        return out


def write_store_bundles(
    db_path: Path,
    scope_dir: Path,
    *,
    scrub: Scrubber,
    sources: list[str] | None = None,
    top: int = 12,
    max_marks: int = 12_000,
    explorer: bool = True,
    subtasks: bool = True,
    progress: Callable[[str], None] = lambda _m: None,
) -> list[dict[str, Any]]:
    """Explorer and subtask payloads for every (or each named) store source; returns the index entries."""
    from swarm_mcp.modules.subtasks.sources import corpus_sources
    from swarm_mcp.scope import db
    from swarm_mcp.scope.viz.subtasks_html import build_subtasks
    from swarm_mcp.scope.viz.timeline_html import build_timeline

    entries = store_sources(db_path)
    if sources:
        unknown = set(sources) - {e["source"] for e in entries}
        if unknown:
            raise ValueError(f"unknown source(s): {', '.join(sorted(unknown))}")
        entries = [e for e in entries if e["source"] in sources]
    with db.connect(db_path) as s:
        corpora = set(corpus_sources(s))
    for e in entries:
        src = e["source"]
        e["explorer"] = e["subtasks"] = None
        errors: list[str] = []
        if explorer and e["counts"].get("messages"):
            progress(f"explorer: {src}")
            t = time.monotonic()
            try:
                payload, meta = build_timeline(db_path, source=src, top=top, max_marks=max_marks, scrub=scrub)
                rel = f"scope/{_slug(src)}.explorer.json"
                size = _write_json(scope_dir.parent / rel, payload)
                e["explorer"] = {"file": rel, "bytes": size, "lanes": len(payload["lanes"]), "sampled": meta["sampled"]}
                progress(f"  {rel}: {size:,} bytes in {time.monotonic() - t:.1f}s")
            except Exception as ex:  # noqa: BLE001 - one source failing must not sink the others
                errors.append(f"explorer: {type(ex).__name__}: {ex}")
        if subtasks and src in corpora:
            progress(f"subtasks: {src}")
            t = time.monotonic()
            try:
                payload, meta, _ = build_subtasks(db_path, corpus=src, scrub=scrub)
                rel = f"scope/{_slug(src)}.subtasks.json"
                size = _write_json(scope_dir.parent / rel, payload)
                e["subtasks"] = {"file": rel, "bytes": size, "units": meta["units"], "edges": meta["edges"]}
                progress(f"  {rel}: {size:,} bytes in {time.monotonic() - t:.1f}s")
            except Exception as ex:  # noqa: BLE001
                errors.append(f"subtasks: {type(ex).__name__}: {ex}")
        if errors:
            e["errors"] = errors
    return entries


# --------------------------------------------------------------------------- live sessions


def _agent_name(a: dict[str, Any], n_of_type: int) -> str:
    """Display name: the main agent by its working folder, a subagent by its type (numbered when repeated).
    Names are matched as @mentions by RECALL's adapter, so they are kept distinctive."""
    m = a["meta"]
    if m.get("kind") == "main":
        folder = Path(m.get("cwd") or "").name
        return f"Main agent · {folder}" if folder else "Main agent"
    kind = m.get("agent_type") or "subagent"
    return f"{kind} #{n_of_type}" if n_of_type > 1 else f"{kind} (subagent)"


def live_sessions(recordings: Path, *, scrub: Scrubber, now: float | None = None) -> list[dict[str, Any]]:
    """Every recorded session as a RECALL session document, newest first, built from the ``claude_code``
    adapter's rows (the same evidence ids the store uses)."""
    from swarm_mcp.scope.adapters.claude_code import SOURCE, ClaudeCodeAdapter

    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for table, row in ClaudeCodeAdapter().load(recordings):
        rows[table].append(row)
    now = time.time() if now is None else now

    session_of_agent = {a["agent_id"]: a["meta"].get("session_id") for a in rows["agents"]}
    by_session: dict[str | None, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for a in rows["agents"]:
        by_session[a["meta"].get("session_id")]["agents"].append(a)
    for p in rows["periods"]:
        by_session[p["meta"].get("session_id")]["periods"].append(p)
    for x in rows["actions"]:
        by_session[session_of_agent.get(x["agent_id"])]["actions"].append(x)
    for m in rows["messages"]:
        sid = session_of_agent.get(m["author_id"]) or next(
            (session_of_agent[r] for r in m["recipient_ids"] if r in session_of_agent), None
        )
        by_session[sid]["messages"].append(m)

    docs = []
    for sid, t in by_session.items():
        if sid is None:  # records whose agent is unknown to the recordings
            continue
        agents = sorted(t["agents"], key=lambda a: (a["meta"].get("kind") != "main", a["first_seen"] or datetime.min))
        seen_type: dict[str, int] = defaultdict(int)
        names: dict[str, str] = {}
        out_agents = []
        for a in agents:
            m = a["meta"]
            if m.get("kind") != "main":
                seen_type[m.get("agent_type") or "subagent"] += 1
            names[a["agent_id"]] = _agent_name(a, seen_type[m.get("agent_type") or "subagent"])
            out_agents.append(
                {
                    "id": a["agent_id"],
                    "name": names[a["agent_id"]],
                    "kind": m.get("kind"),
                    "agentType": m.get("agent_type"),
                    "parent": m.get("parent"),
                    "task": scrub(m.get("task") or "")[:TEXT_CHARS],
                    "status": m.get("status"),
                    "first": _iso(a["first_seen"]),
                    "last": _iso(a["last_seen"]),
                }
            )
        periods = [
            {
                "id": p["evidence_id"],
                "agent": p["meta"].get("agent"),
                "kind": p["kind"],
                "label": scrub(p["label"] or "")[:300],
                "start": _iso(p["start_ts"]),
                "end": _iso(p["end_ts"]),
                "parent": p["meta"].get("parent"),
                "status": p["meta"].get("status"),
            }
            for p in sorted(t["periods"], key=lambda p: p["start_ts"] or datetime.min)
        ]
        messages = [
            {
                "id": m["evidence_id"],
                "author": m["author_id"],
                "to": m["recipient_ids"],
                "replyTo": m.get("reply_to"),
                "t": _iso(m["ts"]),
                "type": m["msg_type"],
                "text": scrub(m["content"] or "")[:TEXT_CHARS],
            }
            for m in sorted(t["messages"], key=lambda m: (m["ts"] or datetime.min, m["evidence_id"]))
        ]
        actions = []
        for x in sorted(t["actions"], key=lambda x: (x["ts"] or datetime.min, x["evidence_id"])):
            meta = x["meta"]
            inp = meta.get("input") if isinstance(meta.get("input"), dict) else {}
            cmd = inp.get("command") if isinstance(inp.get("command"), str) else None
            actions.append(
                {
                    "id": x["evidence_id"],
                    "agent": x["agent_id"],
                    "run": x["run_id"],
                    "t": _iso(x["ts"]),
                    "end": meta.get("ended"),
                    "tool": x["kind"],
                    "text": scrub(x["content"] or "")[:COMMAND_CHARS],
                    "command": scrub(cmd)[:COMMAND_CHARS] if cmd else None,
                    "output": scrub(meta.get("output") or "")[:OUTPUT_CHARS],
                    "status": meta.get("status"),
                    "spawned": meta.get("spawned"),
                }
            )
        times = [x for x in [*(a["first"] for a in out_agents), *(a["last"] for a in out_agents)] if x]
        times += [m["t"] for m in messages if m["t"]] + [x["t"] for x in actions if x["t"]]
        start, updated = (min(times), max(times)) if times else (None, None)
        main = next((a for a in out_agents if a["kind"] == "main"), out_agents[0] if out_agents else None)
        stopped = bool(main) and main["status"] == "stopped" and not any(a["status"] == "running" for a in out_agents)
        recent = updated is not None and now - _epoch(updated) < RUNNING_SECS
        cwd = next((a["meta"].get("cwd") for a in agents if a["meta"].get("cwd")), None)
        label = (main or {}).get("task") or ""
        label = " ".join(label.split())[:90] or f"Session {sid[:8]}"
        docs.append(
            {
                "v": SESSION_VERSION,
                "kind": "claude-code-session",
                "source": SOURCE,
                "session": sid,
                "synthetic": sid.startswith("demo-"),  # examples/live_demo.py: made-up work, labelled so in RECALL
                "label": label,
                "folder": Path(cwd).name if cwd else None,
                "status": "running" if (recent and not stopped) else "stopped",
                "start": start,
                "updated": updated,
                "agents": out_agents,
                "periods": periods,
                "messages": messages,
                "actions": actions,
                "notes": list(ClaudeCodeAdapter.notes),
            }
        )
    docs.sort(key=lambda d: d["updated"] or "", reverse=True)
    return docs


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def write_live(recordings: Path | None, scope_dir: Path, *, scrub: Scrubber, keep: int = 40) -> dict[str, Any]:
    """Write ``live/<session>.json`` for the ``keep`` most recently active sessions; returns the index's live block.
    Unchanged sessions keep their file (and its mtime)."""
    block: dict[str, Any] = {"recordings": str(recordings) if recordings else None, "sessions": []}
    if not recordings or not Path(recordings).exists():
        block["note"] = "no swarm-live recordings yet (the plugin's hooks create them, or `swarm-live import`)"
        return block
    live_dir = scope_dir / "live"
    docs = live_sessions(Path(recordings), scrub=scrub)[:keep]
    wanted = set()
    for d in docs:
        name = f"{_slug(d['session'])}.json"
        wanted.add(name)
        path = live_dir / name
        body = json.dumps(d, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if not path.exists() or path.read_text(encoding="utf-8") != body:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, path)
        subs = [a for a in d["agents"] if a["kind"] != "main"]
        block["sessions"].append(
            {
                "id": d["session"],
                "synthetic": d["synthetic"],
                "file": f"scope/live/{name}",
                "label": d["label"],
                "folder": d["folder"],
                "status": d["status"],
                "start": d["start"],
                "updated": d["updated"],
                "agents": len(d["agents"]),
                "subagents": len(subs),
                "actions": len(d["actions"]),
                "errors": sum(1 for x in d["actions"] if x["status"] == "error"),
                "messages": len(d["messages"]),
            }
        )
    if live_dir.exists():  # sessions that fell out of the window
        for f in live_dir.glob("*.json"):
            if f.name not in wanted:
                f.unlink()
    block["updatedAt"] = max((s["updated"] or "" for s in block["sessions"]), default=None)
    return block


# --------------------------------------------------------------------------- entry points


def write_index(scope_dir: Path, *, store: Path | None, sources: list[dict[str, Any]], live: dict[str, Any]) -> Path:
    path = scope_dir / "index.json"
    _write_json(
        path,
        {
            "v": INDEX_VERSION,
            "generatedAt": _now(),
            "store": store.name if store and store.exists() else None,
            "sources": sources,
            "live": live,
        },
    )
    return path


def _keep_skipped(entries: list[dict[str, Any]], scope_dir: Path, *, explorer: bool, subtasks: bool) -> None:
    """A run with --no-explorer / --no-subtasks keeps the previous index's payloads for those parts (when their
    files are still there), so a quick refresh never hides what an earlier full run wrote."""
    index = scope_dir / "index.json"
    if (explorer and subtasks) or not index.exists():
        return
    try:
        before = {e["source"]: e for e in json.loads(index.read_text(encoding="utf-8")).get("sources") or []}
    except ValueError:
        return
    for e in entries:
        old = before.get(e["source"]) or {}
        for part, ran in (("explorer", explorer), ("subtasks", subtasks)):
            ref = old.get(part)
            if not ran and ref and (scope_dir.parent / ref["file"]).exists():
                e[part] = ref


def render_recall(
    db_path: Path,
    out_dir: Path,
    *,
    recordings: Path | None,
    scrub: Scrubber,
    sources: list[str] | None = None,
    top: int = 12,
    max_marks: int = 12_000,
    explorer: bool = True,
    subtasks: bool = True,
    progress: Callable[[str], None] = lambda _m: None,
) -> dict[str, Any]:
    """Write the store bundles, the live sessions and the index under ``out_dir/scope``; returns a summary."""
    scope_dir = Path(out_dir) / "scope"
    t = time.monotonic()
    entries: list[dict[str, Any]] = []
    if db_path.exists():
        entries = write_store_bundles(
            db_path,
            scope_dir,
            scrub=scrub,
            sources=sources,
            top=top,
            max_marks=max_marks,
            explorer=explorer,
            subtasks=subtasks,
            progress=progress,
        )
    else:
        progress(f"no store at {db_path}: writing live sessions only")
    _keep_skipped(entries, scope_dir, explorer=explorer, subtasks=subtasks)
    live = write_live(recordings, scope_dir, scrub=scrub)
    index = write_index(scope_dir, store=db_path, sources=entries, live=live)
    return {
        "index": str(index),
        "sources": [
            {k: e.get(k) for k in ("source", "adapter", "explorer", "subtasks", "errors") if e.get(k) is not None}
            for e in entries
        ],
        "live_sessions": len(live["sessions"]),
        "recordings": live["recordings"],
        "seconds": round(time.monotonic() - t, 1),
    }


def watch(
    db_path: Path,
    out_dir: Path,
    *,
    recordings: Path,
    scrub: Scrubber,
    interval: float = 2.0,
    progress: Callable[[str], None] = print,
    stop: Callable[[], bool] = lambda: False,
) -> None:
    """Rewrite the live sessions and the index whenever the recordings (or their WAL) change. Store sources are
    kept from the existing index, so ``render recall`` once, then ``--watch`` while sessions run."""
    scope_dir = Path(out_dir) / "scope"
    index = scope_dir / "index.json"
    entries: list[dict[str, Any]] = []
    if index.exists():
        try:
            entries = json.loads(index.read_text(encoding="utf-8")).get("sources") or []
        except ValueError:
            entries = []
    last = None
    while not stop():
        stamp = tuple(
            (p.stat().st_mtime_ns, p.stat().st_size)
            for p in (recordings, recordings.with_name(recordings.name + "-wal"))
            if p.exists()
        )
        if stamp != last:
            last = stamp
            try:
                live = write_live(recordings, scope_dir, scrub=scrub)
                write_index(scope_dir, store=db_path, sources=entries, live=live)
                running = sum(1 for s in live["sessions"] if s["status"] == "running")
                progress(f"{_now()}  {len(live['sessions'])} sessions ({running} running) -> {index}")
            except Exception as e:  # noqa: BLE001 - a half-written recording: try again on the next change
                progress(f"{_now()}  skipped: {type(e).__name__}: {e}")
                last = None
        time.sleep(interval)
