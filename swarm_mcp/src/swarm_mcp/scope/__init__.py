"""SwarmScope core library: a unified DuckDB evidence store for swarm datasets.

Not a tool module. ``swarm_mcp.modules.scope`` / ``findings`` / ``village``
expose it over MCP; ``swarm-mcp ingest|render|check-findings`` use it from the
command line. Every record carries an evidence id (``evidence.py``).
"""
