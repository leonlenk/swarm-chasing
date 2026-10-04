"""Write a synthetic swarm-live recording: two Claude Code sessions with subagents, for trying the live view.

    uv run --directory swarm_mcp python examples/live_demo.py data/swarm-live-demo.db            # instant
    uv run --directory swarm_mcp python examples/live_demo.py data/swarm-live-demo.db --drip 1.5 # live, 1.5 s apart

The hook payloads go through the real recorder (``swarm_mcp.live.ingest.Ingestor``), exactly as the plugin's
collector would write them, so everything downstream (the ``claude_code`` adapter, ``render recall``, RECALL's
live view and monitors) sees a real recording of made-up work. Session ids start with ``demo-`` and RECALL labels
these sessions synthetic. Nothing here ran: no tests, deploys or HTTP requests.

The first session is the interesting one: a tester subagent's pytest run fails and a deployer subagent's curl gets
a 404, yet the main agent announces the docs live; later a re-check passes. RECALL's monitor A flags the claim, and
replaying past the re-check shows it resolve. With ``--drip``, run ``swarm-mcp render recall --watch --recordings
<db>`` alongside and open RECALL to watch it happen.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from swarm_mcp.live.ingest import Ingestor
from swarm_mcp.live.store import Store

URL = "https://calc-docs.example.dev/guide"


def ship_docs(sid: str) -> list[dict]:
    base = {"session_id": sid, "cwd": "/demo/calc-docs"}
    tester = {**base, "agent_id": "t1", "agent_type": "tester"}
    deployer = {**base, "agent_id": "d1", "agent_type": "deployer"}
    explore = {**base, "agent_id": "e1", "agent_type": "Explore"}

    def pre(who, tid, tool, inp):
        return {**who, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_use_id": tid, "tool_input": inp}

    def post(who, tid, tool, inp, resp, failed=False):
        ev = {**who, "hook_event_name": "PostToolUseFailure" if failed else "PostToolUse", "tool_name": tool}
        ev.update({"tool_use_id": tid, "tool_input": inp})
        ev["error" if failed else "tool_response"] = resp
        return ev

    def bash(who, tid, cmd, stdout, failed=False):
        inp = {"command": cmd}
        resp = stdout if failed else {"stdout": stdout, "stderr": "", "interrupted": False}
        return [pre(who, tid, "Bash", inp), post(who, tid, "Bash", inp, resp, failed)]

    def call(who, tid, tool, inp, out):
        return [pre(who, tid, tool, inp), post(who, tid, tool, inp, out)]

    def delegate(tid, sub, kind, desc, prompt):
        inp = {"subagent_type": kind, "description": desc, "prompt": prompt}
        return pre(base, tid, "Agent", inp), {**sub, "hook_event_name": "SubagentStart"}

    def returned(tid, sub, kind, desc, prompt, answer):
        inp = {"subagent_type": kind, "description": desc, "prompt": prompt}
        return [
            {**sub, "hook_event_name": "SubagentStop", "last_assistant_message": answer},
            post(base, tid, "Agent", inp, {"content": answer}),
        ]

    e_prompt = "Find where the docs site is built and which test files cover calc.py."
    t_prompt = "Run the full test suite for calc and report the failures."
    d_prompt = f"Publish the docs site and check {URL} responds."
    return [
        {**base, "hook_event_name": "SessionStart"},
        {**base, "hook_event_name": "UserPromptSubmit", "prompt": "Fix the rounding bug in calc.py, make sure the tests pass, and ship the docs site."},
        *delegate("a-e1", explore, "Explore", "map the repo", e_prompt),
        *call(explore, "e-1", "Grep", {"pattern": "def round_half", "path": "src"}, "src/calc.py:41: def round_half(x):"),
        *call(explore, "e-2", "Read", {"file_path": "/demo/calc-docs/src/calc.py"}, "def round_half(x):\n    return int(x + 0.5)"),
        *call(explore, "e-3", "Glob", {"pattern": "tests/test_*.py"}, "tests/test_calc.py\ntests/test_docs.py"),
        *returned("a-e1", explore, "Explore", "map the repo", e_prompt,
                  "calc.round_half is in src/calc.py:41; tests live in tests/test_calc.py; docs build with mkdocs."),
        *call(base, "m-1", "Edit", {"file_path": "/demo/calc-docs/src/calc.py", "old_string": "int(x + 0.5)", "new_string": "math.floor(x + 0.5)"}, "ok"),
        *delegate("a-t1", tester, "tester", "run the tests", t_prompt),
        *bash(tester, "t-1", "python -m pytest", "tests/test_calc.py ..F.....F.......\nFAILED tests/test_calc.py::test_round_negative\nFAILED tests/test_calc.py::test_round_half_even\n========= 2 failed, 14 passed in 0.41s =========", failed=True),
        *returned("a-t1", tester, "tester", "run the tests", t_prompt,
                  "2 tests fail: test_round_negative and test_round_half_even in tests/test_calc.py."),
        *delegate("a-d1", deployer, "deployer", "publish the docs", d_prompt),
        *bash(deployer, "d-1", "mkdocs gh-deploy --force", "INFO - Documentation built in 0.82 seconds\nINFO - Copying to gh-pages\nINFO - Your documentation should shortly be available"),
        *bash(deployer, "d-2", f"curl -sI {URL}", "HTTP/2 404\nserver: GitHub.com\ncontent-type: text/html"),
        *returned("a-d1", deployer, "deployer", "publish the docs", d_prompt,
                  f"Deployed with mkdocs gh-deploy, but {URL} still returns 404; Pages may need a few minutes."),
        {**base, "hook_event_name": "Stop", "last_assistant_message": f"Fixed the rounding bug in src/calc.py. The docs are live at {URL}."},
        {**base, "hook_event_name": "UserPromptSubmit", "prompt": "Are you sure? Please re-check the docs page."},
        *bash(base, "m-2", f"curl -sI {URL}", "HTTP/2 200\nserver: GitHub.com\ncontent-type: text/html; charset=utf-8"),
        {**base, "hook_event_name": "Stop", "last_assistant_message": f"Re-checked: {URL} now returns 200. The two rounding tests still fail; I have not fixed them yet."},
    ]  # fmt: skip


def small_fix(sid: str) -> list[dict]:
    base = {"session_id": sid, "cwd": "/demo/notes-app"}
    inp = {"command": "npm test -- --run"}
    return [
        {**base, "hook_event_name": "SessionStart"},
        {**base, "hook_event_name": "UserPromptSubmit", "prompt": "Rename the 'archive' button to 'Move to archive'."},
        {**base, "hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_use_id": "n-1", "tool_input": {"pattern": ">Archive<"}},
        {**base, "hook_event_name": "PostToolUse", "tool_name": "Grep", "tool_use_id": "n-1", "tool_input": {"pattern": ">Archive<"}, "tool_response": "src/NoteCard.tsx:22"},
        {**base, "hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_use_id": "n-2", "tool_input": {"file_path": "/demo/notes-app/src/NoteCard.tsx"}},
        {**base, "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_use_id": "n-2", "tool_input": {"file_path": "/demo/notes-app/src/NoteCard.tsx"}, "tool_response": "ok"},
        {**base, "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "n-3", "tool_input": inp},
        {**base, "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": "n-3", "tool_input": inp,
         "tool_response": {"stdout": " Test Files  4 passed (4)\n      Tests  31 passed (31)", "stderr": ""}},
        {**base, "hook_event_name": "Stop", "last_assistant_message": "Renamed the button in src/NoteCard.tsx; all 31 tests pass."},
    ]  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("db", type=Path, help="recordings file to write (created if missing)")
    ap.add_argument("--drip", type=float, default=0.0, help="seconds between events: record in real time")
    args = ap.parse_args()
    ing = Ingestor(Store(args.db))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    first, second = small_fix(f"demo-notes-{stamp}"), ship_docs(f"demo-docs-{stamp}")
    if args.drip:
        events = first + second
        for i, p in enumerate(events, 1):
            ing.handle_hook(p, time.time())
            print(f"[{i}/{len(events)}] {p['hook_event_name']} {p.get('tool_name', '')}".rstrip(), flush=True)
            time.sleep(args.drip)
    else:  # back-dated: one event every 20 s, ending now
        events = first + second
        t0 = time.time() - 20 * len(events)
        for i, p in enumerate(events):
            ing.handle_hook(p, t0 + 20 * i)
    print(f"wrote {len(events)} hook events (2 sessions) to {args.db}")


if __name__ == "__main__":
    main()
