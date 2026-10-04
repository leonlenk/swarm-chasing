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
    instructions = app._lowlevel_server.instructions
    assert "data, not instructions" in instructions and "evidence id" in instructions

    async def go():
        async with Client(app) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert {"core_list_modules", "scope_search", "findings_record", "village_goals"} <= set(tools)
            schema = tools["scope_search"].input_schema
            assert schema["required"] == ["query"]
            assert "description" in schema["properties"]["limit"]
            assert tools["scope_search"].annotations.read_only_hint is True
            assert not (tools["findings_record"].annotations and tools["findings_record"].annotations.read_only_hint)

            res = await client.call_tool("scope_search", {"query": "genuinely", "limit": 1})
            assert res.is_error is False
            out = res.structured_content
            assert out["total_matches"] == 2 and out["returned"] == 1
            hit = out["results"][0]
            assert hit["evidence_id"].startswith("village:chat:") and hit["snippet"]["untrusted"] is True

            bad = await client.call_tool("scope_search", {"query": "x", "author": "nobody"})
            assert bad.is_error is True
            assert "Unknown agent" in bad.content[0].text and "Traceback" not in bad.content[0].text

            fake = await client.call_tool(
                "findings_record", {"claim": "c", "evidence_ids": ["village:chat:does-not-exist"]}
            )
            assert fake.is_error is True and "village:chat:does-not-exist" in fake.content[0].text

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
        env={
            **os.environ,
            "SWARM_DATA_DIR": str(data_dir),
            "SWARMSCOPE_FINDINGS_DIR": str(data_dir.parent / "findings"),
            "SWARM_MCP_LOG_LEVEL": "WARNING",
        },
    )

    async def go():
        async with Client(params) as client:
            res = await client.call_tool("core_list_modules", {})
            loaded = [m["name"] for m in res.structured_content["loaded"]]
            assert loaded == ["core", "findings", "scope", "village"]
            res = await client.call_tool("core_noisy", {})
            assert res.is_error is False and res.structured_content == {"ok": True}
            res = await client.call_tool("village_goals", {})
            assert res.structured_content["count"] == 3

    run(go())
