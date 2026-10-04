"""Claude Code adapter: sessions recorded by the swarm-live hooks (``swarm_mcp.live``).

    swarm-live import ~/.claude/projects/<project-dir>      # or let the plugin's hooks record live sessions
    swarm-mcp add ~/.swarm-live/swarm-live.db --adapter claude-code

The input is the recordings SQLite file written by the collector or ``swarm-live import`` (see
``live/store.py``), not the transcripts themselves. Source: ``claude-code``.

Mapping (``<key>`` = ``<session_id>:main`` or ``<session_id>:<agent_id>``):
  main sessions, subagents -> agents    (claude-code:agent:<key>; meta: session_id, kind main/subagent,
                                           agent_type, parent agent id, cwd, task, status)
  each agent's run         -> periods   (kind 'session' or 'subagent_run'; claude-code:period:<key>;
                                           label = first line of its task; meta: agent, parent period)
  user prompts             -> messages  (msg_type 'prompt'; claude-code:msg:prompt-<n>;
                                           human:user -> the main agent)
  Agent/Task calls         -> messages  (msg_type 'delegation'; claude-code:msg:task-<tool_use_id>;
                                           parent -> the subagent it spawned; content = the prompt)
  agent text               -> messages  (msg_type 'final' or 'transcript'; claude-code:msg:<n>; a subagent's
                                           final answer goes to its parent, replying to its delegation; a main
                                           agent's final answer goes to human:user)
  tool calls               -> actions   (kind = tool name; claude-code:event:<tool_use_id>; run_id = the agent's
                                           period; content = command / path / pattern / prompt; meta: status,
                                           input, output, ended, spawned)
  files named in file tools -> artifacts + touches (Read -> read, Edit/MultiEdit/NotebookEdit -> modify,
                                           Write -> create the first time the file is touched, else modify;
                                           only calls that succeeded)
Channels are one per session: ``session-<first 8 chars of the session id>``.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from swarm_mcp.live.store import connect_ro

SOURCE = "claude-code"
HUMAN = "human:user"
DELEGATE_TOOLS = ("Agent", "Task")
FILE_OPS = {"Read": "read", "NotebookRead": "read", "Edit": "modify", "MultiEdit": "modify", "NotebookEdit": "modify"}
NOTES = [
    "only what the hooks (or an imported transcript) saw: work done outside Claude Code's tools, or by tools "
    "that bypass hooks, is missing",
    "tool inputs are trimmed to 1,500 chars per string and outputs to 4,000 chars",
    "a call's status is 'error' only when Claude Code reported a failure (PostToolUseFailure or is_error); a "
    "command that ran but printed failures is 'ok'",
    "files are touched only by Read/Write/Edit-style tools; files changed by shell commands have no touches",
    "imported transcripts pair subagents with Agent calls by order, which can mislink parallel subagents",
]


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None) if ts else None


def _ts(ts: float | None) -> dict[str, Any]:
    """``ts`` and ``ts_quality`` fields for a record."""
    return {"ts": _dt(ts), "ts_quality": "exact" if ts else "missing"}


def _json(raw: str | None) -> Any:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return raw


def summary(tool: str, inp: Any) -> str:
    """One line for a tool call: the command, path, pattern or prompt it was given."""
    if not isinstance(inp, dict):
        return f"{tool} {inp}".strip()
    for k in ("command", "file_path", "notebook_path", "pattern", "url", "query", "description", "prompt", "path"):
        if inp.get(k):
            return f"{tool}: {inp[k]}"
    return f"{tool} {json.dumps(inp, ensure_ascii=False)}" if inp else tool


def _first_line(text: str | None, n: int = 120) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= n else line[: n - 1] + "…"


class ClaudeCodeAdapter:
    name = "claude_code"
    source = SOURCE
    notes = NOTES

    def inspect(self, path: Path) -> dict[str, Any]:
        with closing(connect_ro(Path(path))) as c:
            n = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("agents", "actions", "messages")}
            sessions = c.execute("SELECT count(DISTINCT session_id) FROM agents").fetchone()[0]
        return {"adapter": self.name, "path": str(path), "source": SOURCE, "sessions": sessions, **n}

    def load(self, path: Path, **_: Any) -> Iterator[tuple[str, dict[str, Any]]]:
        with closing(connect_ro(Path(path))) as c:
            agents = [dict(r) for r in c.execute("SELECT * FROM agents ORDER BY started, key")]
            actions = [dict(r) for r in c.execute("SELECT * FROM actions ORDER BY ts, id")]
            messages = [dict(r) for r in c.execute("SELECT * FROM messages ORDER BY ts, id")]
            prompts = [
                dict(r)
                for r in c.execute("SELECT id, ts, agent_key, payload FROM events WHERE event='UserPromptSubmit'")
            ]
            last = {r[0]: r[1] for r in c.execute("SELECT agent_key, max(ts) FROM events GROUP BY agent_key")}
        yield from self._rows(agents, actions, messages, prompts, last)

    def _rows(self, agents, actions, messages, prompts, last) -> Iterator[tuple[str, dict[str, Any]]]:
        def aid(key: str) -> str:
            return f"{SOURCE}:agent:{key}"

        def pid(key: str) -> str:
            return f"{SOURCE}:period:{key}"

        def channel(session_id: str) -> str:
            return f"session-{(session_id or 'unknown')[:8]}"

        by_key = {a["key"]: a for a in agents}

        def known(key: str | None) -> str | None:
            return key if key in by_key else None

        # ---- agents and their runs
        for a in agents:
            parent = known(a["parent_key"])
            kind = "subagent" if a["agent_id"] else "main"
            seen = [t for t in (a["started"], a["stopped"], last.get(a["key"])) if t]
            end = max(seen) if seen else None
            yield (
                "agents",
                {
                    "agent_id": aid(a["key"]),
                    "source": SOURCE,
                    "display_name": a["label"] or a["key"],
                    "aliases": sorted({x for x in (a["agent_type"], a["key"]) if x}),
                    "first_seen": _dt(a["started"]),
                    "last_seen": _dt(end),
                    "meta": {
                        "kind": kind,
                        "session_id": a["session_id"],
                        "agent_type": a["agent_type"],
                        "parent": aid(parent) if parent else None,
                        "cwd": a["cwd"],
                        "task": a["task"],
                        "status": a["status"],
                    },
                },
            )
            yield (
                "periods",
                {
                    "evidence_id": pid(a["key"]),
                    "source": SOURCE,
                    "kind": "subagent_run" if a["agent_id"] else "session",
                    "label": _first_line(a["task"]) or a["label"] or a["key"],
                    "start_ts": _dt(a["started"]),
                    "end_ts": _dt(end),
                    "meta": {
                        "agent": aid(a["key"]),
                        "session_id": a["session_id"],
                        "parent": pid(parent) if parent else None,
                        "cwd": a["cwd"],
                        "status": a["status"],
                    },
                },
            )

        def session_of(key: str) -> str:
            return by_key[key]["session_id"] if key in by_key else key.split(":", 1)[0]

        # ---- user prompts
        for p in prompts:
            payload = _json(p["payload"])
            text = payload.get("prompt") if isinstance(payload, dict) else None
            yield (
                "messages",
                {
                    "evidence_id": f"{SOURCE}:msg:prompt-{p['id']}",
                    "source": SOURCE,
                    "channel": channel(session_of(p["agent_key"])),
                    "author_id": HUMAN,
                    "recipient_ids": [aid(p["agent_key"])] if known(p["agent_key"]) else [],
                    **_ts(p["ts"]),
                    "msg_type": "prompt",
                    "content": text or "",
                },
            )

        # ---- tool calls, delegations, file touches
        delegation_of: dict[str, str] = {}  # subagent key -> its delegation message id
        seq: dict[str, int] = {}
        touched: set[str] = set()
        for x in actions:
            key = x["agent_key"]
            inp = _json(x["input"])
            eid = f"{SOURCE}:event:{x['id']}"
            seq[key] = seq.get(key, -1) + 1
            child = known(x["linked_agent"])
            yield (
                "actions",
                {
                    "evidence_id": eid,
                    "source": SOURCE,
                    "agent_id": aid(key),
                    "run_id": pid(key) if known(key) else None,
                    "seq": seq[key],
                    **_ts(x["ts"]),
                    "kind": x["tool"] or "?",
                    "content": summary(x["tool"] or "?", inp),
                    "meta": {
                        "status": x["status"],
                        "input": inp,
                        "output": x["output"],
                        "ended": _dt(x["ts_end"]).isoformat() + "Z" if x["ts_end"] else None,
                        "spawned": aid(child) if child else None,
                        "cwd": x["cwd"],
                    },
                },
            )
            if x["tool"] in DELEGATE_TOOLS and child:
                mid = f"{SOURCE}:msg:task-{x['id']}"
                delegation_of[child] = mid
                prompt = inp.get("prompt") if isinstance(inp, dict) else None
                yield (
                    "messages",
                    {
                        "evidence_id": mid,
                        "source": SOURCE,
                        "channel": channel(session_of(key)),
                        "author_id": aid(key),
                        "recipient_ids": [aid(child)],
                        **_ts(x["ts"]),
                        "msg_type": "delegation",
                        "content": prompt or summary(x["tool"], inp),
                        "meta": {
                            "action": eid,
                            "subagent_type": inp.get("subagent_type") if isinstance(inp, dict) else None,
                        },
                    },
                )
            op = FILE_OPS.get(x["tool"])
            if x["tool"] == "Write":
                op = "write"
            fp = (inp.get("file_path") or inp.get("notebook_path")) if isinstance(inp, dict) else None
            if op and fp and x["status"] == "ok":
                norm = str(fp).replace("\\", "/")
                art = f"{SOURCE}:artifact:{norm}"
                if op == "write":
                    op = "modify" if art in touched else "create"
                if art not in touched:
                    touched.add(art)
                    cwd = (x["cwd"] or "").replace("\\", "/").rstrip("/")
                    name = norm[len(cwd) + 1 :] if cwd and norm.lower().startswith(cwd.lower() + "/") else norm
                    yield "artifacts", {"artifact_id": art, "source": SOURCE, "kind": "file", "name": name, "meta": {}}
                yield (
                    "touches",
                    {
                        "touch_id": f"{eid}|{op}|{art}",
                        "source": SOURCE,
                        "record_id": eid,
                        "artifact_id": art,
                        "op": op,
                        "ts": _dt(x["ts"]),
                    },
                )

        # ---- agent text
        for m in messages:
            key = m["agent_key"]
            a = by_key.get(key)
            parent = known(a["parent_key"]) if a else None
            recipients: list[str] = []
            reply_to = None
            if m["source"] == "final":
                if a and a["agent_id"] and parent:
                    recipients, reply_to = [aid(parent)], delegation_of.get(key)
                elif a and not a["agent_id"]:
                    recipients = [HUMAN]
            yield (
                "messages",
                {
                    "evidence_id": f"{SOURCE}:msg:{m['id']}",
                    "source": SOURCE,
                    "channel": channel(session_of(key)),
                    "author_id": aid(key),
                    "recipient_ids": recipients,
                    "reply_to": reply_to,
                    **_ts(m["ts"]),
                    "msg_type": m["source"],
                    "content": m["text"] or "",
                },
            )
