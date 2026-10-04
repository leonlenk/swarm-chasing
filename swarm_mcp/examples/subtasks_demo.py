"""End-to-end demo over the real MCP protocol: launch the server on stdio (as Claude Code does) and follow
one lead from a chat search to a subtask, its handoffs, and the commits behind them.

    uv run --directory swarm_mcp swarm-mcp add data/ai-village
    uv run --directory swarm_mcp swarm-mcp add data/ai-village/repos/rpg-game.git
    uv run --directory swarm_mcp python examples/subtasks_demo.py [query]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

DATA = os.environ.get("SWARM_DATA_DIR") or str(Path(__file__).resolve().parents[2] / "data")
QUERY = sys.argv[1] if len(sys.argv) > 1 else "clean talent-only branch"


def show(title: str, obj) -> None:
    print(f"\n── {title}")
    print(obj if isinstance(obj, str) else json.dumps(obj, indent=1, ensure_ascii=False)[:1800])


async def main() -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "swarm_mcp"],
        env={**os.environ, "SWARM_DATA_DIR": DATA, "SWARM_MCP_LOG_LEVEL": "WARNING"},
    )
    async with Client(server, read_timeout_seconds=120) as c:

        async def call(tool: str, **args):
            t = time.perf_counter()
            res = await c.call_tool(tool, args)
            dt = time.perf_counter() - t
            if res.is_error:
                raise SystemExit(f"{tool} failed: {res.content[0].text}")
            print(f"\n▶ {tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})  [{dt:.1f}s]")
            return res.structured_content

        tools = await c.list_tools()
        show("tools", sorted(t.name for t in tools.tools))

        hits = await call("scope_search", query=QUERY, source="village", limit=1)
        hit = hits["results"][0]
        show("search hit", hit)

        rec = await call("core_get", ids=hit["evidence_id"], before=1, after=1, max_chars=160)
        around = rec["neighbors"]["before"] + [{"ts": rec["ts"], "author": rec["author"], "snippet": rec["content"]}]
        around += rec["neighbors"]["after"]
        show("context", [f"{r['ts']} {r['author']}: {r['snippet']['content'][:110]}" for r in around])

        loc = await call("subtasks_locate", event_id=hit["evidence_id"])
        sub = loc["matches"][0]["subtask"]
        show("its subtask", sub)

        got = await call("subtasks_get", subtask_id=sub["subtask_id"], max_chat=2)
        show("members", [f"{m['event_id']}  {m['state']:8s} {m['actor']}: {m['title']['content'][:60]}" for m in got["members"]])
        show("handoffs", [f"{h['time']} {h['summary']['content']}" for h in got["handoffs"]])
        show(
            "other methods",
            [
                f"{o['method']}: {o['pieces']} piece(s), largest '{o['largest_piece']['label']['content']}'"
                for o in got["other_methods"]
            ],
        )
        show("unresolved", got["unresolved"])

        ev = got["handoffs"][0]["evidence"][0]
        commit = await call("core_get", ids=ev, max_chars=400)
        show(
            "evidence commit",
            {"agent": commit["agent"], "content": commit["content"]["content"], "artifacts": commit.get("artifacts")},
        )

        pair = await call("subtasks_trace_pair", corpus="rpg-game", actor_a="Opus 4.5", actor_b="GPT-5.2", limit=3)
        show("pair summary", pair["summary"])
        show(
            "top shared subtasks",
            [f"{s['label']['content']} ({s['handoffs_between_them']} handoffs)" for s in pair["shared_subtasks"][:4]],
        )


if __name__ == "__main__":
    asyncio.run(main())
