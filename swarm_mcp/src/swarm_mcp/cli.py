"""``swarm-mcp`` command line.

    swarm-mcp                                  run the MCP server on stdio (what Claude Code launches)
    swarm-mcp --list-modules                   print the module report as JSON and exit
    swarm-mcp ingest <adapter> <path>          build/refresh the SwarmScope store (data/swarmscope.duckdb)
    swarm-mcp render timeline [options]        write a self-contained HTML agent swimlane
    swarm-mcp check-findings                   verify every finding's evidence ids resolve (exit 1 if not)

Relative paths are tried against the current directory first, then the project
root (``uv run --directory swarm_mcp`` changes the cwd to swarm_mcp/).
Subcommand output goes to stdout; logs go to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from swarm_mcp.config import Config, find_project_root, resolve_data_dir

SUBCOMMANDS = ("ingest", "render", "check-findings")


def resolve_output(raw: str, cwd: Path | None = None) -> Path:
    """Output file path: absolute as given; relative against the cwd when its parent directory exists
    there, otherwise against the project root (so ``data/x.html`` lands in the repo's gitignored data/
    even under ``uv run --directory swarm_mcp``)."""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    cwd = cwd or Path.cwd()
    if (cwd / path).parent.is_dir():
        return (cwd / path).resolve()
    return (find_project_root(cwd) / path).resolve()


def _db(args: argparse.Namespace, config: Config) -> Path:
    return resolve_data_dir(args.db) if getattr(args, "db", None) else config.store_path


def cmd_ingest(args: argparse.Namespace, config: Config) -> int:
    from swarm_mcp.scope.adapters import get_adapter
    from swarm_mcp.scope.ingest import ingest

    path = resolve_data_dir(args.path)
    if args.inspect:
        print(json.dumps(get_adapter(args.adapter, args.source).inspect(path), indent=2, default=str))
        return 0
    result = ingest(
        args.adapter,
        path,
        _db(args, config),
        include_events=not args.no_events,
        source=args.source,
        progress=lambda m: print(m, file=sys.stderr),
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_render(args: argparse.Namespace, config: Config) -> int:
    from swarm_mcp.scope.viz.timeline_html import render_timeline
    from swarm_mcp.toolkit import Scrubber, parse_time

    # default output lives under the (gitignored) data dir: the page embeds masked dataset snippets
    out = resolve_output(args.out) if args.out else config.data_dir / "swarmscope-timeline.html"
    result = render_timeline(
        _db(args, config),
        out,
        top=args.top,
        since=parse_time(args.since, field="since"),
        until=parse_time(args.until, end=True, field="until"),
        channel=args.channel,
        source=args.source,
        scrub=Scrubber(config.scrub, config.email_allowlist),
        snippet_chars=args.snippet_chars,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_check_findings(args: argparse.Namespace, config: Config) -> int:
    from swarm_mcp.scope.findings import check_findings

    findings_file = Path(args.findings).expanduser() if args.findings else config.findings_path / "findings.jsonl"
    result = check_findings(findings_file, _db(args, config))
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarm-mcp", description="swarm-mcp: SwarmScope store tools.")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("ingest", help="ingest a dataset into the SwarmScope DuckDB store (idempotent)")
    i.add_argument("adapter", help="adapter name: ai_village, git or wiki")
    i.add_argument(
        "path", help="dataset path, e.g. data/ai-village, data/ai-village/repos/rpg-game.git, data/collusion-wiki"
    )
    i.add_argument("--source", help="evidence-id source prefix (git/wiki default to the repo / folder name)")
    i.add_argument("--db", help="store path (default: $SWARMSCOPE_DB or data/swarmscope.duckdb)")
    i.add_argument("--no-events", action="store_true", help="skip events.jsonl.gz (actions table stays empty)")
    i.add_argument("--inspect", action="store_true", help="only describe the dataset (files, fields), load nothing")
    i.set_defaults(fn=cmd_ingest)

    r = sub.add_parser("render", help="render a visualization")
    rsub = r.add_subparsers(dest="what", required=True)
    t = rsub.add_parser("timeline", help="self-contained HTML swimlane: one row per agent, one mark per message")
    t.add_argument("--out", help="output HTML file (default: <data dir>/swarmscope-timeline.html, gitignored)")
    t.add_argument("--top", type=int, default=12, help="number of agents (by message count) to show")
    t.add_argument("--since", help="inclusive start, ISO date/datetime (UTC)")
    t.add_argument("--until", help="exclusive end, ISO date/datetime (UTC); a bare date includes that day")
    t.add_argument("--channel", help="only this channel (e.g. general)")
    t.add_argument("--source", help="only this source (e.g. village)")
    t.add_argument("--snippet-chars", type=int, default=160, help="hover snippet length (masked)")
    t.add_argument("--db", help="store path")
    t.set_defaults(fn=cmd_render)

    c = sub.add_parser("check-findings", help="verify that every finding's evidence ids resolve")
    c.add_argument("--findings", help="findings.jsonl path (default: findings/findings.jsonl)")
    c.add_argument("--db", help="store path")
    c.set_defaults(fn=cmd_check_findings)
    return p


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in SUBCOMMANDS:
        from swarm_mcp.server import main as serve

        serve(argv)
        return
    from swarm_mcp.server import setup_logging

    config = Config.from_env()
    setup_logging(config.log_level)
    args = build_parser().parse_args(argv)
    try:
        code = args.fn(args, config)
    except (ValueError, FileNotFoundError) as e:  # ToolInputError is a ValueError
        print(f"error: {e}", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
