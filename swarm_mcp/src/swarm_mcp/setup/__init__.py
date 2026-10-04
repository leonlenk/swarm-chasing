"""Bring-your-own-dataset setup: profile a new multi-agent dataset, map it onto the
standard event records declaratively, check the mapping, and hand it to ingest.

    swarm-mcp add <path> [--name SLUG] [--agent none|api|claude-code] [--mapping M] [--dry-run]

Modules: ``readers`` (formats), ``profile`` (field stats + role guesses), ``mapping``
(spec + ``MappedAdapter``), ``check`` (conformance), ``agent`` (heuristic / LLM /
Claude Code drafting), ``protocol_bridge`` (the only glue to the record format).
See "Mapping a new dataset" in ``swarm_mcp/ADDING_MODULES.md``.
"""
