"""swarm-live: record Claude Code sessions (main agents, subagents, tool calls, messages) as they run.

Standard library only. The pieces:
    launch.py     the plugin's SessionStart hook: starts the collector if needed (``uv run ... swarm-live serve``)
    collector.py  local HTTP server on 127.0.0.1:47831 that every other hook POSTs to
    ingest.py     hook payloads and transcripts -> agents / actions / messages, subagents linked to their parent
    store.py      the recordings SQLite file (``SWARM_LIVE_DB``, else $CLAUDE_PLUGIN_DATA or ~/.swarm-live)
    cli.py        ``swarm-live serve`` and ``swarm-live import <transcripts>``

The recordings reach the SwarmScope store through the ``claude_code`` adapter
(``scope/adapters/claude_code.py``): ``swarm-mcp add <recordings db>``, or the ``claude_code_sync`` tool.
"""
