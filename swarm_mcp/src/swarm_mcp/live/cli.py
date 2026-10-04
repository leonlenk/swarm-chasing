"""swarm-live CLI: the collector process and transcript import.

  uv run --directory swarm_mcp swarm-live serve  [--port 47831] [--db PATH] [--exit-when-idle]
  uv run --directory swarm_mcp swarm-live import PATH [--db PATH]

The Claude Code plugin starts ``serve --exit-when-idle`` from its SessionStart hook (see launch.py), so you only
run these by hand to keep a collector up permanently or to load sessions recorded before the plugin was installed.
"""

from __future__ import annotations

import argparse


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="swarm-live", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="run the hook collector")
    p.add_argument("--port", type=int)
    p.add_argument("--db")
    p.add_argument("--quiet", action="store_true")
    p.add_argument(
        "--exit-when-idle",
        action="store_true",
        help="exit once no Claude Code session is live (used by the plugin hook)",
    )
    p = sub.add_parser("import", help="load finished Claude Code transcripts")
    p.add_argument("path", help="a transcript .jsonl or a ~/.claude/projects/<project> directory")
    p.add_argument("--db")
    a = ap.parse_args(argv)

    from swarm_mcp.live.store import Store, default_db

    if a.cmd == "serve":
        from swarm_mcp.live.collector import DEFAULT_PORT, serve

        serve(a.port or DEFAULT_PORT, a.db, a.quiet, a.exit_when_idle)
    elif a.cmd == "import":
        from swarm_mcp.live.ingest import import_path

        st = Store(a.db or default_db())
        n = import_path(st, a.path)
        print(f"imported {n} transcript(s) into {st.path}")


if __name__ == "__main__":
    main()
