"""Bring-your-own-dataset setup: profile a new multi-agent dataset, map it onto the
standard event records declaratively, check the mapping, and hand it to ingest.

    python -m swarm_mcp.setup inspect <path>                    # profile -> <path>/.swarmscope/profile.json
    python -m swarm_mcp.setup setup <source> <path> --agent none|api|claude-code
    python -m swarm_mcp.setup check <mapping.json> [<path>] [--full]
    python -m swarm_mcp.setup schema                            # the mapping JSON Schema

Modules: ``readers`` (formats), ``profile`` (field stats + role guesses), ``mapping``
(spec + ``MappedAdapter``), ``check`` (conformance), ``agent`` (heuristic / LLM /
Claude Code drafting), ``protocol_bridge`` (the only glue to the record format).
See ``swarm_mcp/docs/BRING_YOUR_OWN_DATA.md``.
"""
