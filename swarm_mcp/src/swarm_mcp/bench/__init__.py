"""Synthetic swarm benchmark for SwarmScope investigation tools.

- ``generate``: write a fake AI-Village-layout dataset with planted events + ``truth.json``.
- ``reference``: a simple solver that recovers the planted events from the files.
- ``score``: precision / recall / F1 of tool outputs against ``truth.json``.

Output shapes the scope tools should return are in ``contracts.md``.
CLI: ``python -m swarm_mcp.bench generate|reference|score``.
"""

from swarm_mcp.bench.generate import SIZES, generate, generate_size

__all__ = ["SIZES", "generate", "generate_size"]
