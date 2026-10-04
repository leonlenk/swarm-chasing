"""``swarm-mcp`` command line.

    swarm-mcp                              run the MCP server on stdio (what Claude Code launches)
    swarm-mcp info [--json]                modules, sources, findings health and config (exit 1 on bad findings)
    swarm-mcp add <path> [options]         add a dataset to the SwarmScope store (idempotent)
    swarm-mcp render <view> [options]      write a self-contained HTML view (views: timeline)
    swarm-mcp export --out DIR [filters]   export a redacted subset of the store, then check it

``add`` detects the AI Village layout and uses the built-in adapter. Any other
dataset is profiled, mapped (``--mapping`` or a draft by ``--agent``), checked
(it stops with the report on failure) and then ingested. ``--dry-run`` stops
after the check. The developer benchmark is ``python -m swarm_mcp.bench``.

Relative paths are tried against the current directory first, then the project
root (``uv run --directory swarm_mcp`` changes the cwd to swarm_mcp/).
Results go to stdout; progress and logs go to stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from swarm_mcp.config import Config, ConfigError, find_project_root, resolve_data_dir

SUBCOMMANDS = ("info", "add", "render", "export")
AGENT_MODES = ("none", "api", "claude-code")


class CommandError(ValueError):
    """A user-facing error: printed as ``error: ...`` with exit code 2."""


def resolve_output(raw: str, cwd: Path | None = None) -> Path:
    """Output path: absolute as given; relative against the cwd when its parent directory exists
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
    return resolve_output(args.db) if getattr(args, "db", None) else config.store_path


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- info


def cmd_info(args: argparse.Namespace, config: Config) -> int:
    import contextlib

    from swarm_mcp.info import format_info, server_info
    from swarm_mcp.server import build_server

    if getattr(args, "db", None):
        config = _with_db(config, _db(args, config))
    with contextlib.redirect_stdout(sys.stderr):
        app = build_server(config)
    info = server_info(config, app.swarm_registry)  # type: ignore[attr-defined]
    print(json.dumps(info, indent=2, default=str) if args.json else format_info(info))
    return 1 if info["findings"].get("bad") else 0


def _with_db(config: Config, db: Path) -> Config:
    from dataclasses import replace

    return replace(config, db_path=db)


# --------------------------------------------------------------------------- add

_SLUG = re.compile(r"^[a-z][a-z0-9_-]*$")


def village_dir(path: Path) -> Path | None:
    """``path`` (or ``path/ai-village``) if it holds the AI Village export's file set."""
    from swarm_mcp.scope.adapters.ai_village import AGENTS_FILE, CHAT_FILE, ROOMS_FILE

    for cand in (path, path / "ai-village"):
        if cand.is_dir() and all((cand / f).is_file() for f in (AGENTS_FILE, CHAT_FILE, ROOMS_FILE)):
            return cand
    return None


def default_name(path: Path) -> str:
    stem = path.name.split(".")[0] if path.is_file() else path.name
    slug = re.sub(r"[^a-z0-9_-]+", "_", stem.lower()).strip("_-")
    if not slug or not slug[0].isalpha():
        slug = f"ds_{slug}" if slug else "dataset"
    return slug


def _counts(res: dict[str, Any]) -> str:
    c = res["counts"]
    return ", ".join(f"{c.get(k, 0):,} {k}" for k in ("messages", "actions", "agents", "periods"))


def _next_steps(source: str) -> list[str]:
    return [
        "Next:",
        "  swarm-mcp info                       # check what is in the store",
        "  restart the 'swarm' MCP server (in Claude Code: /mcp) so the tools see the new data",
        f"  then try core_info and scope_search(source='{source}')",
    ]


def cmd_add(args: argparse.Namespace, config: Config) -> int:
    path = resolve_data_dir(args.path)
    if not path.exists():
        raise CommandError(f"dataset not found: {path}")
    db = _db(args, config)
    village = None if args.mapping else village_dir(path)
    if village is not None:
        return _add_village(args, village, db)
    return _add_mapped(args, config, path, db)


def _add_village(args: argparse.Namespace, path: Path, db: Path) -> int:
    from swarm_mcp.scope.adapters.ai_village import SOURCE, AiVillageAdapter
    from swarm_mcp.scope.ingest import ingest

    if args.name and args.name != SOURCE:
        raise CommandError(f"AI Village data always uses the source name {SOURCE!r} (got --name {args.name!r})")
    print(f"detected the AI Village layout in {path}: using the built-in ai_village adapter")
    if args.dry_run:
        info = AiVillageAdapter().inspect(path)
        print(f"dry run: {len(info['files'])} files, nothing ingested")
        for name, rows in sorted((info.get("manifest_row_counts") or {}).items()):
            print(f"  {name}: {rows:,} rows")
        return 0
    res = ingest("ai_village", path, db, progress=_say)
    print(f"ingested source '{res['source']}' into {res['db']}: {_counts(res)} ({res['seconds']}s)")
    print("(re-running add replaces this source; other sources and findings are kept)")
    print("\n".join(_next_steps(res["source"])))
    return 0


def _add_mapped(args: argparse.Namespace, config: Config, path: Path, db: Path) -> int:
    from swarm_mcp.setup.check import format_report, run_check
    from swarm_mcp.setup.mapping import MappingError, load_spec

    mappings_dir = config.project_root / "mappings"
    if args.mapping:
        mapping_path = resolve_data_dir(args.mapping)
        try:
            spec = load_spec(mapping_path)
        except MappingError as e:
            raise CommandError(str(e)) from None
        source = spec.get("source")
        if args.name and args.name != source:
            raise CommandError(f"--name {args.name!r} differs from the mapping's source {source!r}")
        print(f"using mapping {mapping_path} (source '{source}')")
        _say("checking the mapping on a sample ...")
        report = run_check(spec, path)
    else:
        source = args.name or default_name(path)
        if not _SLUG.match(source):
            raise CommandError(f"--name must be a lowercase slug (letters, digits, _ or -), got {source!r}")
        mapping_path, report = _draft(args, path, source, mappings_dir)
        if report is None:  # claude-code: the slash command takes over
            return 0
    print(format_report(report))
    if report["status"] != "pass":
        print(
            f"\nThe mapping does not pass the check, so nothing was ingested. Fix {mapping_path} "
            f"(see the report and the TODO notes), then run:\n  swarm-mcp add {path} --mapping {mapping_path}"
        )
        return 1
    if args.dry_run:
        print(f"\ndry run: the mapping passes; nothing ingested. Ingest with:\n  swarm-mcp add {path} --mapping {mapping_path}")
        return 0
    from swarm_mcp.scope.ingest import ingest_mapped

    res = ingest_mapped(mapping_path, path, db, progress=_say)
    print(f"\ningested source '{res['source']}' into {res['db']}: {_counts(res)} ({res['seconds']}s)")
    print(f"mapping: {mapping_path} (re-running add replaces this source; other sources and findings are kept)")
    print("\n".join(_next_steps(res["source"])))
    return 0


def _draft(args: argparse.Namespace, path: Path, source: str, mappings_dir: Path):
    from swarm_mcp.setup.agent import SetupError, setup_dataset

    print(f"no known layout in {path}: profiling it and drafting a mapping (--agent {args.agent})")
    try:
        res = setup_dataset(source, path, agent=args.agent, mappings_dir=mappings_dir)
    except SetupError as e:
        raise CommandError(str(e)) from None
    mapping_path = Path(res["mapping_path"])
    print(f"wrote {mapping_path} and {res['log_path']}")
    for n in res.get("notes") or []:
        print(f"  note: {n}")
    if args.agent == "claude-code":
        print(
            f"\nwrote {res['task_path']}. In Claude Code run:\n  /swarm-setup {source} {path}\n"
            "It refines the mapping until the check passes, then runs "
            f"`swarm-mcp add {path} --mapping {mapping_path}`."
        )
        return mapping_path, None
    if res.get("rationale"):
        print(f"rationale: {res['rationale']}")
    return mapping_path, res["report"]


# --------------------------------------------------------------------------- render

# One entry per view: name -> (help, add_arguments, run). Graph and board views slot in here.
RenderView = tuple[str, Callable[[argparse.ArgumentParser], None], Callable[[argparse.Namespace, Config], Any]]


def _timeline_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--top", type=int, default=12, help="number of agents (by message count) to show")
    p.add_argument("--channel", help="only this channel (e.g. general)")
    p.add_argument("--snippet-chars", type=int, default=160, help="hover snippet length (masked)")


def _timeline_run(args: argparse.Namespace, config: Config) -> Any:
    from swarm_mcp.scope.viz.timeline_html import render_timeline
    from swarm_mcp.toolkit import Scrubber, parse_time

    out = resolve_output(args.out) if args.out else config.data_dir / "swarmscope-timeline.html"
    return render_timeline(
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


RENDER_VIEWS: dict[str, RenderView] = {
    "timeline": ("HTML swimlane: one row per agent, one mark per message", _timeline_args, _timeline_run),
}


def cmd_render(args: argparse.Namespace, config: Config) -> int:
    result = RENDER_VIEWS[args.view][2](args, config)
    print(json.dumps(result, indent=2, default=str))
    return 0


# --------------------------------------------------------------------------- export


def cmd_export(args: argparse.Namespace, config: Config) -> int:
    from swarm_mcp.scope.records import export_store

    filters = {
        "source": args.source or None,
        "kind": args.kind or None,
        "channel": args.channel,
        "author": args.author,
        "since": args.since,
        "until": args.until,
        "query": args.query,
    }
    out_dir = resolve_output(args.out)
    rules = ["default"] + (["ip"] if args.redact_ips else [])
    res = export_store(
        _db(args, config),
        out_dir,
        filters,
        allow_email_domains=config.email_allowlist,
        rules=rules,
        with_agents=args.with_agents,
        check=not args.no_check,
    )
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        rec, red = res["records"], res["redaction"]
        print(f"exported {rec['events']:,} records to {res['out_dir']}")
        for src, kinds in rec["by_source_kind"].items():
            print(f"  {src}: " + ", ".join(f"{n:,} {k}" for k, n in kinds.items()))
        if rec.get("agents") is not None:
            print(f"  agents: {rec['agents']:,}")
        print(f"  time range: {rec['time_range']['first']} .. {rec['time_range']['last']}")
        counts = ", ".join(f"{n:,} {t}" for t, n in red["counts"].items()) or "nothing"
        print(f"redacted: {counts} in {red['records_changed']:,} records (rules: {', '.join(red['rules'])}; "
              f"emails kept for: {', '.join(red['allow_email_domains']) or 'none'})")  # fmt: skip
        if "check" in res:
            chk = res["check"]
            status = "passed" if chk["ok"] else "FAILED"
            print(f"check {status}: {chk['lines_scanned']:,} lines in {len(chk['files_scanned'])} files rescanned "
                  f"with all rules; {len(chk['findings'])} findings, {len(chk['problems'])} problems")  # fmt: skip
            for f in chk["findings"][:20]:
                print(f"  {f['file']}:{f['line']} field {f['field']}: {f['type']} x{f['count']}")
            for p in chk["problems"][:20]:
                print(f"  {p['file']}: {p['problem']}")
        else:
            print("check skipped (--no-check)")
    return 0 if res.get("ok", True) else 1


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarm-mcp",
        description="swarm-mcp: run the MCP server (no arguments) or manage the SwarmScope store.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("info", help="modules, sources, findings health and config (exit 1 if findings are bad)")
    i.add_argument("--json", action="store_true", help="print the full report as JSON")
    i.add_argument("--db", help="store path (default: [data] db in swarm.toml, or <data dir>/swarmscope.duckdb)")
    i.set_defaults(fn=cmd_info)

    a = sub.add_parser("add", help="add a dataset to the store: detect or map it, check it, ingest it (idempotent)")
    a.add_argument("path", help="dataset folder or file, e.g. data/ai-village")
    a.add_argument("--name", help="source slug for a mapped dataset (default: from the folder name)")
    a.add_argument(
        "--agent", choices=AGENT_MODES, default="none",
        help="who drafts the mapping: none = heuristic draft with TODO notes; api = an LLM (ANTHROPIC_API_KEY); "
        "claude-code = write a task file for the /swarm-setup command",
    )  # fmt: skip
    a.add_argument("--mapping", help="use this mapping JSON instead of drafting one")
    a.add_argument("--dry-run", action="store_true", help="stop after the check; ingest nothing")
    a.add_argument("--db", help="store path (default: [data] db in swarm.toml, or <data dir>/swarmscope.duckdb)")
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("render", help="write a self-contained HTML view of the store")
    views = r.add_subparsers(dest="view", required=True)
    for name, (help_text, add_args, _run) in RENDER_VIEWS.items():
        v = views.add_parser(name, help=help_text)
        v.add_argument("--out", help=f"output HTML file (default: <data dir>/swarmscope-{name}.html, gitignored)")
        v.add_argument("--since", help="inclusive start, ISO date/datetime (UTC)")
        v.add_argument("--until", help="exclusive end, ISO date/datetime (UTC); a bare date includes that day")
        v.add_argument("--source", help="only this source (e.g. village)")
        v.add_argument("--db", help="store path")
        add_args(v)
    r.set_defaults(fn=cmd_render)

    e = sub.add_parser("export", help="export a redacted subset of the store, then rescan it (the check)")
    e.add_argument("--out", required=True, metavar="DIR", help="export directory (created; keep it under data/)")
    e.add_argument("--source", action="append", default=[], help="only these sources (repeatable)")
    e.add_argument("--kind", action="append", default=[], help="only these id kinds, e.g. chat, event (repeatable)")
    e.add_argument("--channel", help="only messages in this channel")
    e.add_argument("--author", help="only this author: agent name/alias/id, 'human' or 'human:<id>'")
    e.add_argument("--since", help="inclusive UTC start (ISO date or datetime)")
    e.add_argument("--until", help="exclusive UTC end; a bare date includes that day")
    e.add_argument("--query", help="only records whose text contains this (case-insensitive)")
    e.add_argument("--with-agents", action="store_true", help="also export the sources' agent records")
    e.add_argument("--redact-ips", action="store_true", help="also mask private/loopback IPs")
    e.add_argument("--no-check", action="store_true", help="skip the strict rescan of the export")
    e.add_argument("--json", action="store_true", help="print the summary as JSON")
    e.add_argument("--db", help="store path")
    e.set_defaults(fn=cmd_export)
    return p


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in (*SUBCOMMANDS, "-h", "--help"):
        from swarm_mcp.server import main as serve

        serve(argv)
        return
    args = build_parser().parse_args(argv)
    from swarm_mcp.server import setup_logging

    try:
        config = Config.load()
        setup_logging(config.log_level)
        code = args.fn(args, config)
    except (ValueError, FileNotFoundError, ConfigError) as e:  # ToolInputError and CommandError are ValueErrors
        print(f"error: {e}", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
