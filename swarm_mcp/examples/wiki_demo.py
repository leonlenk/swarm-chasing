"""The same tools on a non-village corpus: collusion.wiki, over the real MCP protocol (stdio).

    uv run --directory swarm_mcp python examples/wiki_demo.py [query]

Needs data/collusion-wiki/collusion-wiki.db (Simon Willison's SQLite build of the collusion.wiki export).
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
QUERY = sys.argv[1] if len(sys.argv) > 1 else "querydata"


def show(title: str, obj) -> None:
    print(f"\n── {title}")
    print(obj if isinstance(obj, str) else json.dumps(obj, indent=1, ensure_ascii=False)[:1600])


async def main() -> None:
    env = {**os.environ, "SWARM_DATA_DIR": DATA, "SWARM_MCP_LOG_LEVEL": "WARNING"}
    async with Client(
        StdioServerParameters(command=sys.executable, args=["-m", "swarm_mcp"], env=env), read_timeout_seconds=120
    ) as c:

        async def call(tool: str, **args):
            t = time.perf_counter()
            res = await c.call_tool(tool, args)
            if res.is_error:
                raise SystemExit(f"{tool} failed: {res.content[0].text}")
            print(f"\n▶ {tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})  [{time.perf_counter() - t:.1f}s]")
            return res.structured_content

        d = await call("wiki_describe", corpus="collusion-wiki")
        show("corpus", {"counts": d["counts"], "time_span": d["time_span"], "blind_spots": d["blind_spots"][:4]})

        hits = await call("wiki_search", corpus="collusion-wiki", query=QUERY, limit=1)
        hit = hits["results"][0]
        show(
            "search",
            {
                "total_matches": hits["total_matches"],
                "distinct_actors": hits["distinct_actors"],
                "first": {k: hit[k] for k in ("event_id", "time", "actor", "location", "snippet")},
            },
        )

        ctx = await call("core_get", ids=hit["event_id"], before=1, after=2, max_chars=150)
        show(
            "same page, before/after",
            [f"{r['time']} {r['actor']}: {r['text'][:100]}" for r in ctx["before"] + [ctx["event"]] + ctx["after"]],
        )

        loc = await call("subtasks_locate", event_id=hit["event_id"])
        sub = loc["matches"][0]["subtask"]
        show(
            "its subtask",
            {
                k: sub[k]
                for k in (
                    "subtask_id",
                    "label",
                    "size",
                    "start",
                    "end",
                    "actors_involved",
                    "handoffs_inside",
                    "agreement",
                )
            },
        )

        got = await call("subtasks_get", subtask_id=sub["subtask_id"], max_members=5, max_chat=0)
        show("publishers' page_family labels inside it", got["dataset_labels"])
        show("participants", got["participants"][:5])
        show("handoffs", [f"{h['type']}: {h['summary']}" for h in got["handoffs"][:5]])
        show("unresolved", got["unresolved"])

        busiest = await call("subtasks_list", corpus="collusion-wiki", sort="handoffs", limit=6)
        show(
            "subtasks with the most handoffs",
            [
                f"{s['label']}: {s['size']} sessions, {s['actors_involved']} actors, {s['handoffs_inside']} handoffs"
                for s in busiest["subtasks"]
            ],
        )

        shared = await call("wiki_actors", corpus="collusion-wiki", sort="ip16", limit=3)
        show(
            "labels used from the most IP /16 prefixes (one label != one agent)",
            [
                f"{a['actor']}: {a['revisions']} revisions from {a['ip16_prefixes']} /16 prefixes"
                for a in shared["actors"]
            ],
        )


if __name__ == "__main__":
    asyncio.run(main())
