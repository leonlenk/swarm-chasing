"""``swarm-mcp`` command line.

    swarm-mcp                              run the MCP server on stdio (what Claude Code launches)
    swarm-mcp info [--json]                modules, sources, findings health and config (exit 1 on bad findings)
    swarm-mcp add <path> [options]         add a dataset to the SwarmScope store (idempotent)
    swarm-mcp render <view> [options]      write a self-contained HTML view (views: timeline)
    swarm-mcp export --out DIR [filters]   export a redacted subset of the store, then check it

``add --adapter auto`` (the default) picks the adapter from the path: ``--mapping``
given -> mapped; the AI Village file set -> ai_village; a bare git repository
(a directory with HEAD, objects/ and refs/ that git takes for a repository root) -> git;
anything else is mapped with ``mappings/<source>.json`` when it exists (re-runs
keep hand edits; delete it to redraft), else profiled and mapped by a draft
from ``--agent``, checked (it stops with the report on failure) and then
ingested. ``--adapter wiki`` (the collusion.wiki explorer SQLite schema) is
never auto-detected: pass it explicitly. ``--dry-run`` ingests nothing: it
writes the draft mapping (when none exists) and stops after the check (mapped),
or only inspects the dataset (village, git, wiki). The developer benchmark is ``python -m swarm_mcp.bench``.

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
ADD_ADAPTERS = ("auto", "village", "git", "wiki", "mapped")


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


def git_root(path: Path) -> Path | None:
    """The git directory when ``path`` is the root of a repository: ``path`` for a bare repository, ``path/.git``
    for a working tree. None otherwise, in particular for a folder inside another repository (``git -C`` would
    walk up to that one)."""
    import subprocess

    if not path.is_dir():
        return None
    try:
        res = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--absolute-git-dir"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):  # git not installed, or hung
        return None
    if res.returncode != 0 or not res.stdout.strip():
        return None
    git_dir, real = Path(res.stdout.strip()).resolve(), path.resolve()
    if git_dir == real:
        return path
    return path / ".git" if git_dir == real / ".git" else None


def git_repo_dir(path: Path) -> Path | None:
    """``path`` if it is a bare git repository (auto-detect): HEAD, objects/ and refs/, and git agrees it is a
    repository root (e.g. ``data/ai-village/repos/rpg-game.git``)."""
    if not ((path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()):
        return None
    return path if git_root(path) == path else None


def default_name(path: Path) -> str:
    stem = path.name.split(".")[0] if path.is_file() else path.name
    slug = re.sub(r"[^a-z0-9_-]+", "_", stem.lower()).strip("_-")
    if not slug or not slug[0].isalpha():
        slug = f"ds_{slug}" if slug else "dataset"
    return slug


def _counts(res: dict[str, Any]) -> str:
    c = res["counts"]
    keys = ["messages", "actions", "agents", "periods"] + [k for k in ("artifacts", "touches") if c.get(k)]
    return ", ".join(f"{c.get(k, 0):,} {k}" for k in keys)


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
    adapter = args.adapter
    if args.mapping and adapter not in ("auto", "mapped"):
        raise CommandError(f"--mapping only applies to mapped datasets (got --adapter {adapter})")
    if adapter == "village" or (adapter == "auto" and not args.mapping and village_dir(path)):
        village = village_dir(path)
        if village is None:
            raise CommandError(f"no AI Village file set in {path} (or {path / 'ai-village'})")
        return _add_village(args, village, db, detected=adapter == "auto")
    repo = git_repo_dir(path) if adapter == "auto" and not args.mapping else None
    if adapter == "git" or repo:
        repo = repo or git_root(path)
        if repo is None:
            raise CommandError(
                f"not a git repository root: {path} (pass a bare repository or the top folder of a working tree)"
            )
        return _add_builtin(args, "git", repo, db, detected=adapter == "auto")
    if adapter == "wiki":
        return _add_builtin(args, "wiki", path, db, detected=False)
    return _add_mapped(args, config, path, db)


def _add_village(args: argparse.Namespace, path: Path, db: Path, *, detected: bool = True) -> int:
    from swarm_mcp.scope.adapters.ai_village import SOURCE, AiVillageAdapter
    from swarm_mcp.scope.ingest import ingest

    if args.name and args.name != SOURCE:
        raise CommandError(f"AI Village data always uses the source name {SOURCE!r} (got --name {args.name!r})")
    how = "detected the AI Village layout" if detected else "AI Village data"
    print(f"{how} in {path}: using the built-in ai_village adapter")
    if args.dry_run:
        info = AiVillageAdapter().inspect(path)
        print(f"dry run: {len(info['files'])} files, nothing ingested")
        for name, rows in sorted((info.get("manifest_row_counts") or {}).items()):
            print(f"  {name}: {rows:,} rows")
        return 0
    res = _guarded(args, lambda replace: ingest("ai_village", path, db, progress=_say, replace=replace))
    print(f"ingested source '{res['source']}' into {res['db']}: {_counts(res)} ({res['seconds']}s)")
    print(_replaced_note(res))
    print("\n".join(_next_steps(res["source"])))
    return 0


def _guarded(args: argparse.Namespace, run: Callable[[bool], dict[str, Any]]) -> dict[str, Any]:
    """``run(replace)``, turning a ``SourceConflict`` (the source holds another dataset) into a user error."""
    from swarm_mcp.scope.ingest import SourceConflict

    try:
        return run(bool(args.replace))
    except SourceConflict as e:
        raise CommandError(
            f"source '{e.source}' already holds {e.existing[0]} data from {e.existing[1]}; pass --name to pick "
            f"another or --replace (nothing was ingested)"
        ) from None


def _replaced_note(res: dict[str, Any]) -> str:
    """What happened to the source: only "replaces" when it really was the same dataset."""
    if res.get("replaced") == "mapping":
        return f"(replaced source '{res['source']}', which used another mapping: {res.get('previous')})"
    if res.get("replaced") == "same":
        return "(this replaced the previous copy of the same dataset; other sources and findings are kept)"
    if res.get("replaced") == "replaced":
        return f"(replaced source '{res['source']}', which held {res.get('previous')}; other sources and findings are kept)"
    return "(a new source; other sources and findings are kept)"


_BUILTIN_LABEL = {"git": "a bare git repository", "wiki": "a wiki database"}


def _inspect_counts(info: dict[str, Any]) -> list[str]:
    """Counts from an adapter's ``inspect``: numbers as is, lists by length, dicts of numbers flattened."""
    out = []
    for k, v in info.items():
        if isinstance(v, bool) or k in ("adapter", "path", "source"):
            continue
        if isinstance(v, int):
            out.append(f"{v:,} {k.replace('_', ' ')}")
        elif isinstance(v, (list, tuple)):
            out.append(f"{len(v):,} {k.replace('_', ' ')}")
        elif isinstance(v, dict):
            out += [f"{n:,} {sub.replace('_', ' ')}" for sub, n in v.items() if isinstance(n, int)]
    return out


def _add_builtin(args: argparse.Namespace, name: str, path: Path, db: Path, *, detected: bool) -> int:
    """The git or wiki adapter, unchanged: ``--name`` becomes the adapter's source (default: its own,
    the repo or folder name)."""
    from swarm_mcp.scope.adapters import get_adapter
    from swarm_mcp.scope.ingest import ingest

    if args.name and not _SLUG.match(args.name):
        raise CommandError(f"--name must be a lowercase slug (letters, digits, _ or -), got {args.name!r}")
    source = args.name
    if source is None and name == "git" and path.name == ".git":  # a working tree's .git: name it after the tree
        source = default_name(path.parent)
    how = f"detected {_BUILTIN_LABEL[name]}" if detected else _BUILTIN_LABEL[name]
    print(f"{how} in {path}: using the built-in {name} adapter")
    try:
        info = get_adapter(name, source).inspect(path)
    except Exception as e:  # noqa: BLE001 - not a repository / not a database: a user error, not a crash
        raise CommandError(f"{path} is not readable as {_BUILTIN_LABEL[name]}: {type(e).__name__}: {e}") from None
    if args.dry_run:
        counts = ", ".join(_inspect_counts(info)) or "no counts"
        print(f"dry run: source '{info.get('source')}': {counts}; nothing ingested")
        return 0
    res = _guarded(args, lambda replace: ingest(name, path, db, source=source, progress=_say, replace=replace))
    print(f"ingested source '{res['source']}' ({res['adapter']} adapter) into {res['db']}: {_counts(res)} "
          f"({res['seconds']}s)")  # fmt: skip
    print(_replaced_note(res))
    print("\n".join(_next_steps(res["source"])))
    return 0


def _add_mapped(args: argparse.Namespace, config: Config, path: Path, db: Path) -> int:
    from swarm_mcp.setup.check import format_report, run_check
    from swarm_mcp.setup.mapping import MappingError, load_spec

    mappings_dir = config.project_root / "mappings"
    mapping_path = resolve_data_dir(args.mapping) if args.mapping else None
    if mapping_path is None:
        source = args.name or default_name(path)
        if not _SLUG.match(source):
            raise CommandError(f"--name must be a lowercase slug (letters, digits, _ or -), got {source!r}")
        if (mappings_dir / f"{source}.json").is_file():  # never redraft over the user's edits
            mapping_path = mappings_dir / f"{source}.json"
            print(f"using existing mapping mappings/{source}.json (delete it to redraft)")
    if mapping_path is not None:
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
    else:  # no mapping yet: draft one (a dry run too, so it can be reviewed)
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

    res = _guarded(  # a re-add with a changed mapping replaces the source; another dataset needs --replace
        args,
        lambda replace: ingest_mapped(
            mapping_path, path, db, progress=_say, replace=replace, allow_mapping_change=True
        ),
    )
    print(f"\ningested source '{res['source']}' into {res['db']}: {_counts(res)} ({res['seconds']}s)")
    print(f"mapping: {mapping_path} {_replaced_note(res)}")
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
    # the check rescans with every rule, so by default the export masks private IPs too
    rules = ["default"] if args.keep_ips else ["all"]
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
    a.add_argument("path", help="dataset folder or file, e.g. data/ai-village or data/ai-village/repos/rpg-game.git")
    a.add_argument(
        "--adapter", choices=ADD_ADAPTERS, default="auto",
        help="auto = --mapping given -> mapped, the AI Village file set -> village, a bare git repo -> git, "
        "else mapped with a drafted mapping; wiki (a collusion.wiki explorer SQLite db) is used only when given",
    )  # fmt: skip
    a.add_argument(
        "--name",
        help="source slug: for mapped data (default: from the folder name), git or wiki (default: the repo / "
        "folder name); AI Village is always 'village'",
    )
    a.add_argument(
        "--agent", choices=AGENT_MODES, default="none",
        help="who drafts the mapping: none = heuristic draft with TODO notes; api = an LLM (ANTHROPIC_API_KEY); "
        "claude-code = write a task file for the /swarm-setup command",
    )  # fmt: skip
    a.add_argument("--mapping", help="use this mapping JSON instead of drafting one")
    a.add_argument(
        "--dry-run",
        action="store_true",
        help="ingest nothing. mapped: write the draft mapping (if mappings/<source>.json does not exist yet) and "
        "stop after the check; village/git/wiki: only inspect",
    )
    a.add_argument(
        "--replace",
        action="store_true",
        help="replace a source of the same name that holds another dataset (another adapter or path; its rows "
        "are deleted)",
    )
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
    e.add_argument("--kind", action="append", default=[], help="only these id kinds, e.g. msg, event (repeatable)")
    e.add_argument("--channel", help="only messages in this channel")
    e.add_argument("--author", help="only this author: agent name/alias/id, 'human' or 'human:<id>'")
    e.add_argument("--since", help="inclusive UTC start (ISO date or datetime)")
    e.add_argument("--until", help="exclusive UTC end; a bare date includes that day")
    e.add_argument("--query", help="only records whose text contains this (case-insensitive)")
    e.add_argument("--with-agents", action="store_true", help="also export the sources' agent records")
    e.add_argument(
        "--keep-ips", action="store_true",
        help="leave private/loopback IPs unmasked (the check then flags them unless --no-check)",
    )  # fmt: skip
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
