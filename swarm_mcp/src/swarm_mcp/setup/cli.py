"""``python -m swarm_mcp.setup``: inspect a dataset, draft and check a mapping.

    inspect <path> [--rows N] [--out FILE] [--json]
    setup <source> <path> [--agent none|api|claude-code] [--mappings-dir DIR] [--rounds 3]
    check <mapping.json> [<path>] [--full] [--rows N] [--json]
    schema

Exit codes: 0 ok / check passed, 1 check failed, 2 usage or setup error.

Relative paths work from the repo root even under ``uv run --directory swarm_mcp``:
inputs resolve like ``SWARM_DATA_DIR`` (cwd first, then the project root) and
outputs (``--out``, ``--mappings-dir``) go under the project root.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from swarm_mcp.config import find_project_root, resolve_data_dir
from swarm_mcp.setup.profile import DEFAULT_ROWS


def _in(raw: str | None) -> Path | None:
    return resolve_data_dir(raw) if raw else None


def _out(raw: str) -> Path:
    p = Path(raw).expanduser()
    return p if p.is_absolute() else find_project_root(Path.cwd()) / p


def cmd_inspect(args: argparse.Namespace) -> int:
    from swarm_mcp.setup.profile import profile_path, summarize

    path = _in(args.path)
    prof = profile_path(path, rows=args.rows)
    if args.out:
        out = _out(args.out)
    else:
        out = (path if path.is_dir() else path.parent) / ".swarmscope" / "profile.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(prof, indent=1, default=str) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(prof, indent=1, default=str))
    else:
        print(summarize(prof))
        print(f"\nwrote {out}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from swarm_mcp.setup.check import format_report, run_check
    from swarm_mcp.setup.mapping import load_spec

    spec = load_spec(_in(args.mapping))
    report = run_check(spec, _in(args.path), full=args.full, rows=args.rows)
    print(json.dumps(report, indent=1, default=str) if args.json else format_report(report))
    return 0 if report["status"] == "pass" else 1


def cmd_setup(args: argparse.Namespace) -> int:
    from swarm_mcp.setup.agent import setup_dataset
    from swarm_mcp.setup.check import format_report

    res = setup_dataset(
        args.source, _in(args.path), agent=args.agent, mappings_dir=_out(args.mappings_dir), rounds=args.rounds
    )
    print(format_report(res["report"]))
    for n in res.get("notes") or []:
        print(f"  note: {n}")
    print(f"\nwrote {res['mapping_path']} and {res['log_path']}")
    if res.get("rationale"):
        print(f"rationale: {res['rationale']}")
    print(res["message"])
    return 0 if res["passed"] or args.agent == "claude-code" else 1


def cmd_schema(_: argparse.Namespace) -> int:
    from swarm_mcp.setup.spec_schema import MAPPING_SCHEMA

    print(json.dumps(MAPPING_SCHEMA, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m swarm_mcp.setup", description="Bring-your-own-dataset setup for swarm-mcp."
    )
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("inspect", help="profile a dataset: tables, fields, role guesses, foreign keys")
    i.add_argument("path", help="dataset file or folder")
    i.add_argument("--rows", type=int, default=DEFAULT_ROWS, help=f"rows sampled per table (default {DEFAULT_ROWS})")
    i.add_argument("--out", help="profile JSON path (default <path>/.swarmscope/profile.json)")
    i.add_argument("--json", action="store_true", help="print the full profile JSON instead of the summary")
    i.set_defaults(fn=cmd_inspect)

    c = sub.add_parser("check", help="run a mapping on a sample (or --full) and validate the records")
    c.add_argument("mapping", help="mapping JSON file")
    c.add_argument("path", nargs="?", help="dataset path (default: the mapping's 'root')")
    c.add_argument("--full", action="store_true", help="check every row, not a sample")
    c.add_argument("--rows", type=int, default=2000, help="rows per table in sample mode (default 2000)")
    c.add_argument("--json", action="store_true", help="print the report as JSON")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("setup", help="profile, draft a mapping (heuristic, LLM or Claude Code), and check it")
    s.add_argument("source", help="event-id source slug for this dataset, e.g. forum")
    s.add_argument("path", help="dataset file or folder")
    s.add_argument("--agent", choices=("none", "api", "claude-code"), default="none")
    s.add_argument("--mappings-dir", default="mappings", help="where to write <source>.json etc. (default mappings/)")
    s.add_argument("--rounds", type=int, default=3, help="api mode: max LLM rounds (default 3)")
    s.set_defaults(fn=cmd_setup)

    sc = sub.add_parser("schema", help="print the mapping JSON Schema")
    sc.set_defaults(fn=cmd_schema)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    from swarm_mcp.setup.agent import SetupError
    from swarm_mcp.setup.mapping import MappingError

    try:
        code = args.fn(args)
    except (SetupError, MappingError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        code = 2
    sys.exit(code)
