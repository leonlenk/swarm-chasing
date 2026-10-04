"""Turn Claude Code hook payloads and transcript JSONL into agents / actions / messages (standard library only).

Field names follow the Claude Code hooks reference: session_id, transcript_path, cwd, hook_event_name, agent_id,
agent_type, tool_name, tool_input, tool_use_id, tool_response, error, last_assistant_message. Other harnesses can
POST the same shape to the collector.

No analysis happens here: this only records what happened and links each subagent to the Agent/Task call that
spawned it. Analysis belongs in MCP modules that read the store.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
from datetime import datetime

from swarm_mcp.live.store import agent_key, dumps

MAX_OUT = 4000
DELEGATE_TOOLS = ("Agent", "Task")


def trim(o, n=1500):
    """Shorten long strings inside tool inputs (file contents etc.) while keeping valid JSON."""
    if isinstance(o, str):
        return o if len(o) <= n else o[:n] + f"... [{len(o) - n} more chars]"
    if isinstance(o, dict):
        return {k: trim(v, n) for k, v in o.items()}
    if isinstance(o, list):
        return [trim(v, n) for v in o[:50]]
    return o


def response_text(resp):
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        parts = [str(resp.get(k)) for k in ("stdout", "stderr", "output", "content", "result", "error") if resp.get(k)]
        return "\n".join(parts) if parts else str(resp)
    if isinstance(resp, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in resp)
    return str(resp)


def parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Ingestor:
    def __init__(self, store):
        self.s = store

    # ------------------------------------------------------------------ hooks
    def handle_hook(self, p, ts):
        s = self.s
        ev = p.get("hook_event_name", "?")
        sid = p.get("session_id") or "unknown"
        aid = p.get("agent_id")
        key = agent_key(sid, aid)
        cwd = p.get("cwd")
        s.ensure_agent(agent_key(sid), sid, cwd=cwd, ts=ts)  # every session has a main agent
        if aid:
            s.ensure_agent(key, sid, aid, p.get("agent_type"), cwd, ts)
        s.x(
            "INSERT INTO events(ts, session_id, agent_key, event, payload) VALUES (?,?,?,?,?)",
            (ts, sid, key, ev, dumps(p)[:20000]),
        )

        if ev == "UserPromptSubmit" and not aid:
            a = s.one("SELECT task FROM agents WHERE key=?", (key,))
            if a and not a["task"] and not (p.get("prompt") or "").lstrip().startswith("<"):  # skip system notices
                s.x("UPDATE agents SET task=? WHERE key=?", ((p.get("prompt") or "")[:2000], key))
            s.x("UPDATE agents SET status='running', stopped=NULL WHERE key=?", (key,))
        elif ev == "PreToolUse":
            self.add_action(p.get("tool_use_id"), key, sid, ts, p.get("tool_name", "?"), p.get("tool_input"), cwd)
        elif ev in ("PostToolUse", "PostToolUseFailure"):
            tid = p.get("tool_use_id")
            if not s.one("SELECT id FROM actions WHERE id=?", (tid,)):
                self.add_action(tid, key, sid, ts, p.get("tool_name", "?"), p.get("tool_input"), cwd)
            resp = p.get("tool_response") if ev == "PostToolUse" else (p.get("error") or p.get("tool_response"))
            self.finish_action(tid, ts, resp, failed=(ev == "PostToolUseFailure"))
        elif ev == "PermissionDenied":
            tid = p.get("tool_use_id")
            row = (
                s.one("SELECT id FROM actions WHERE id=?", (tid,))
                if tid
                else s.one(
                    "SELECT id FROM actions WHERE agent_key=? AND tool=? AND status='pending' ORDER BY ts DESC",
                    (key, p.get("tool_name", "?")),
                )
            )
            if row:
                s.x(
                    "UPDATE actions SET status='denied', ts_end=?, output=? WHERE id=?",
                    (ts, str(p.get("reason") or p.get("message") or "permission denied")[:MAX_OUT], row["id"]),
                )
        elif ev == "SubagentStart" and aid:
            self.link_subagent(sid, key, p.get("agent_type"), ts)
        elif ev == "SubagentStop" and aid:
            s.x("UPDATE agents SET status='stopped', stopped=? WHERE key=?", (ts, key))
            s.x("UPDATE actions SET status='no_result' WHERE agent_key=? AND status='pending'", (key,))
            tp = p.get("agent_transcript_path") or self.guess_subagent_transcript(p.get("transcript_path"), sid, aid)
            if tp:
                self.tail_transcript(tp, force_key=key)
            self.add_message(key, sid, ts, "final", p.get("last_assistant_message"))
        elif ev == "Stop" and not aid:
            s.x("UPDATE agents SET status='stopped', stopped=? WHERE key=?", (ts, key))
        elif ev == "SessionEnd":
            s.x("UPDATE agents SET status='stopped', stopped=COALESCE(stopped, ?) WHERE session_id=?", (ts, sid))

        if p.get("transcript_path"):
            self.tail_transcript(p["transcript_path"])
        if ev == "Stop" and not aid:
            self.add_message(key, sid, ts, "final", p.get("last_assistant_message"))

    def add_action(self, tid, key, sid, ts, tool, inp, cwd):
        if not tid or self.s.one("SELECT id FROM actions WHERE id=?", (tid,)):
            return
        self.s.x(
            "INSERT INTO actions(id, agent_key, session_id, ts, tool, input, status, cwd) VALUES (?,?,?,?,?,?,?,?)",
            (tid, key, sid, ts, tool, dumps(trim(inp or {})), "pending", cwd),
        )

    def finish_action(self, tid, ts, resp, failed=False):
        if not self.s.one("SELECT id FROM actions WHERE id=?", (tid,)):
            return
        bad = failed or (isinstance(resp, dict) and bool(resp.get("is_error") or resp.get("interrupted")))
        self.s.x(
            "UPDATE actions SET ts_end=?, status=?, output=? WHERE id=?",
            (ts, "error" if bad else "ok", response_text(resp)[:MAX_OUT], tid),
        )

    def link_subagent(self, sid, child_key, agent_type, ts):
        """Parent = the agent whose most recent unlinked Agent/Task call asked for this subagent's type."""
        cand = self.s.q(
            f"SELECT * FROM actions WHERE session_id=? AND tool IN ({','.join('?' * len(DELEGATE_TOOLS))}) "
            "AND linked_agent IS NULL AND ts <= ? ORDER BY ts DESC",
            (sid, *DELEGATE_TOOLS, ts + 2),
        )

        def wanted(c):
            return json.loads(c["input"] or "{}").get("subagent_type") or ""

        pick = next((c for c in cand if wanted(c) == (agent_type or "")), None) or (cand[0] if cand else None)
        parent = pick["agent_key"] if pick else agent_key(sid)
        task = ""
        if pick:
            inp = json.loads(pick["input"] or "{}")
            task = inp.get("description", "") + ": " + (inp.get("prompt") or "")
            self.s.x("UPDATE actions SET linked_agent=? WHERE id=?", (child_key, pick["id"]))
        self.s.x("UPDATE agents SET parent_key=?, task=COALESCE(task, ?) WHERE key=?", (parent, task[:2000], child_key))

    def add_message(self, key, sid, ts, source, text):
        if not text or not text.strip():
            return
        h = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()
        if self.s.one("SELECT id FROM messages WHERE agent_key=? AND hash=?", (key, h)):
            return
        self.s.x(
            "INSERT INTO messages(agent_key, session_id, ts, source, text, hash) VALUES (?,?,?,?,?,?)",
            (key, sid, ts, source, text, h),
        )

    # ------------------------------------------------------------ transcripts
    @staticmethod
    def guess_subagent_transcript(main_path, sid, aid):
        if not main_path:
            return None
        base = os.path.join(os.path.dirname(main_path), sid, "subagents")
        hits = glob.glob(os.path.join(base, f"*{aid}*.jsonl"))
        return hits[0] if hits else None

    def tail_transcript(self, path, force_key=None, import_tools=False, messages=True):
        """Read new lines of a Claude Code transcript. Adds assistant text as messages; with import_tools, also
        tool calls/results (used by `import`, where no hooks were recorded)."""
        if not os.path.exists(path):
            return
        okey = path if messages else path + "#tools"
        row = self.s.one("SELECT pos FROM offsets WHERE path=?", (okey,))
        pos = row["pos"] if row else 0
        if os.path.getsize(path) < pos:
            pos = 0
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read()
        end = data.rfind(b"\n") + 1
        if end == 0:
            return
        for raw in data[:end].splitlines():
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            self._transcript_line(r, force_key, import_tools, messages)
        self.s.x("INSERT OR REPLACE INTO offsets(path, pos) VALUES (?,?)", (okey, pos + end))

    def _transcript_line(self, r, force_key, import_tools, messages=True):
        t = r.get("type")
        if t not in ("assistant", "user"):
            return
        sid = r.get("sessionId") or r.get("session_id") or "unknown"
        aid = r.get("agentId") if r.get("isSidechain") else None
        key = force_key or agent_key(sid, aid)
        ts = parse_ts(r.get("timestamp")) or 0
        if not force_key:
            self.s.ensure_agent(agent_key(sid), sid, cwd=r.get("cwd"), ts=ts or None)
            if aid:
                self.s.ensure_agent(key, sid, aid, None, r.get("cwd"), ts or None)
        content = (r.get("message") or {}).get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for b in content or []:
            if not isinstance(b, dict):
                continue
            if messages and t == "assistant" and b.get("type") == "text":
                self.add_message(key, sid, ts, "transcript", b.get("text"))
            elif t == "user" and b.get("type") == "text" and not force_key:
                a = self.s.one("SELECT task FROM agents WHERE key=?", (key,))
                if a and not a["task"] and not b.get("text", "").startswith("<"):
                    self.s.x("UPDATE agents SET task=? WHERE key=?", (b["text"][:2000], key))
            elif import_tools and t == "assistant" and b.get("type") == "tool_use":
                self.add_action(b.get("id"), key, sid, ts, b.get("name", "?"), b.get("input"), r.get("cwd"))
            elif import_tools and t == "user" and b.get("type") == "tool_result":
                self.finish_action(b.get("tool_use_id"), ts, b.get("content"), failed=bool(b.get("is_error")))


def import_path(store, path):
    """Import finished Claude Code sessions (a transcript .jsonl or a ~/.claude/projects/<project> directory)."""
    ing = Ingestor(store)
    files = [path] if str(path).endswith(".jsonl") else sorted(glob.glob(os.path.join(path, "*.jsonl")))
    for n, f in enumerate(files, 1):
        print(
            f"[{n}/{len(files)}] importing {os.path.basename(f)} ({os.path.getsize(f) // 1024} KB)",
            file=sys.stderr,
            flush=True,
        )
        # Pass 1: tool calls (main + subagents) and the agent tree; pass 2: messages.
        ing.tail_transcript(f, import_tools=True, messages=False)
        sid = os.path.splitext(os.path.basename(f))[0]
        subs = sorted(glob.glob(os.path.join(os.path.dirname(f), sid, "subagents", "*.jsonl")))
        delegs = store.q(
            f"SELECT * FROM actions WHERE session_id=? AND tool IN ({','.join('?' * len(DELEGATE_TOOLS))}) ORDER BY ts",
            (sid, *DELEGATE_TOOLS),
        )
        for i, sub in enumerate(subs):
            aid = os.path.splitext(os.path.basename(sub))[0].replace("agent-", "")
            key = agent_key(sid, aid)
            store.ensure_agent(key, sid, aid, None)
            ing.tail_transcript(sub, force_key=key, import_tools=True, messages=False)
            if i < len(delegs):  # pair the i-th Agent call with the i-th subagent transcript
                inp = json.loads(delegs[i]["input"] or "{}")
                store.x(
                    "UPDATE agents SET parent_key=?, agent_type=?, task=? WHERE key=?",
                    (delegs[i]["agent_key"], inp.get("subagent_type"), inp.get("prompt", "")[:2000], key),
                )
                store.x("UPDATE actions SET linked_agent=? WHERE id=?", (key, delegs[i]["id"]))
        for sub in subs:
            aid = os.path.splitext(os.path.basename(sub))[0].replace("agent-", "")
            ing.tail_transcript(sub, force_key=agent_key(sid, aid))
        ing.tail_transcript(f)
        store.x("UPDATE agents SET status='stopped' WHERE session_id=?", (sid,))
    return len(files)
