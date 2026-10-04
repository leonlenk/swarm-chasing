"""End-to-end over the MCP protocol: in-process client, and a real stdio subprocess."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from conftest import config_for, run
from mcp import Client, StdioServerParameters

from swarm_mcp.server import build_server


def test_in_process_protocol_roundtrip(data_dir: Path):
    app = build_server(config_for(data_dir))

    async def go():
        async with Client(app) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert {"core_list_modules", "village_search_chat", "village_agent_activity"} <= set(tools)
            schema = tools["village_search_chat"].input_schema
            assert schema["required"] == ["query"]
            assert "description" in schema["properties"]["limit"]
            assert tools["village_search_chat"].annotations.read_only_hint is True

            res = await client.call_tool("village_search_chat", {"query": "genuinely", "limit": 1})
            assert res.is_error is False
            assert res.structured_content["total_matches"] == 2 and res.structured_content["returned"] == 1

            bad = await client.call_tool("village_search_chat", {"query": "x", "agent": "nobody"})
            assert bad.is_error is True
            assert "Unknown agent" in bad.content[0].text and "Traceback" not in bad.content[0].text

            resources = {str(r.uri) for r in (await client.list_resources()).resources}
            assert "village://schema" in resources

    run(go())


NOISY_BOOT = """
import swarm_mcp.modules.core as core
_orig = core.register
def register(mcp, ctx):
    _orig(mcp, ctx)
    @ctx.tool()
    def noisy() -> dict[str, bool]:
        print("stray print from a tool")  # must end up on stderr, not on the protocol stream
        return {"ok": True}
core.register = register
print("stray print at startup")
from swarm_mcp.server import main
main()
"""


def test_stdio_subprocess_keeps_stdout_clean(data_dir: Path):
    """Launch the server over real stdio; stray prints (startup and in-tool) must not corrupt the protocol."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", NOISY_BOOT],
        env={**os.environ, "SWARM_DATA_DIR": str(data_dir), "SWARM_MCP_LOG_LEVEL": "WARNING"},
    )

    async def go():
        async with Client(params) as client:
            res = await client.call_tool("core_list_modules", {})
            loaded = [m["name"] for m in res.structured_content["loaded"]]
            assert loaded == ["core", "investigate", "sweep", "village"]
            res = await client.call_tool("core_noisy", {})
            assert res.is_error is False and res.structured_content == {"ok": True}
            res = await client.call_tool("village_agents", {})
            assert res.structured_content["count"] == 4

    run(go())
