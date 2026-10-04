"""``python -m swarm_mcp.bench`` command line.

    python -m swarm_mcp.bench generate  --out DIR --seed N [--size small|medium]
    python -m swarm_mcp.bench reference --data DIR [--truth DIR/truth.json | --terms a,b] [--out outputs.json]
    python -m swarm_mcp.bench score     --truth DIR/truth.json --outputs outputs.json [--out score.json]

``generate`` writes DIR/ai-village/ (AI Village layout) and DIR/truth.json. Write it
somewhere gitignored (e.g. data/bench/...) or to a temp dir: it is data, not code.
``reference`` runs the built-in solver and writes tool outputs in the score.py contract
shape. ``score`` prints per-task precision/recall/F1 as JSON.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from swarm_mcp.bench.generate import DATASET_DIRNAME, SIZES, TRUTH_FILENAME, generate


def _dump(obj: Any, out: str | None) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text)


def _warn_if_tracked(path: Path) -> None:
    """Generated data must never be committed: warn when the output is inside a git work tree and not ignored."""
    try:
        probe = path / TRUTH_FILENAME
        inside = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return
        ignored = subprocess.run(["git", "-C", str(path), "check-ignore", "-q", str(probe)], capture_output=True)
        if ignored.returncode != 0:
            print(
                f"warning: {probe} is not gitignored; generated data must not be committed (use data/ or a temp dir)",
                file=sys.stderr,
            )
    except OSError:
        pass


def cmd_generate(args: argparse.Namespace) -> int:
    params = dict(SIZES[args.size])
    for key in ("n_agents", "days", "msgs_per_day"):
        if getattr(args, key) is not None:
            params[key] = getattr(args, key)
    res = generate(args.out, args.seed, **params)
    _warn_if_tracked(Path(args.out))
    t = res["truth"]
    _dump(
        {
            "dataset_dir": res["dataset_dir"],
            "truth_path": res["truth_path"],
            "seed": args.seed,
            "params": params,
            "row_counts": t["row_counts"],
            "terms": list(t["diffusion"]),
        },
        None,
    )
    return 0


def _dataset_dir(raw: str) -> Path:
    p = Path(raw)
    return p / DATASET_DIRNAME if (p / DATASET_DIRNAME).is_dir() else p


def cmd_reference(args: argparse.Namespace) -> int:
    from swarm_mcp.bench.reference import solve

    ds = _dataset_dir(args.data)
    if args.terms:
        terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    else:
        truth_path = Path(args.truth) if args.truth else ds.parent / TRUTH_FILENAME
        terms = list(json.loads(truth_path.read_text())["diffusion"])
    _dump(solve(ds, terms), args.out)
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from swarm_mcp.bench.score import score

    truth = json.loads(Path(args.truth).read_text())
    outputs = json.loads(Path(args.outputs).read_text())
    _dump(score(truth, outputs), args.out)
    return 0


def build_parser(prog: str = "python -m swarm_mcp.bench") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="Synthetic swarm benchmark for SwarmScope tools.")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="write a synthetic AI Village dataset plus truth.json")
    g.add_argument("--out", required=True, help="output directory (gets ai-village/ and truth.json)")
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--size", choices=sorted(SIZES), default="small")
    g.add_argument("--n-agents", dest="n_agents", type=int, help="override the size preset")
    g.add_argument("--days", type=int, help="override the size preset")
    g.add_argument("--msgs-per-day", dest="msgs_per_day", type=int, help="override the size preset")
    g.set_defaults(fn=cmd_generate)

    r = sub.add_parser("reference", help="run the reference solver; writes tool outputs (the shapes in score.py)")
    r.add_argument("--data", required=True, help="the generate --out directory, or its ai-village/ subdirectory")
    r.add_argument("--truth", help="truth.json to read the term list from (default: next to ai-village/)")
    r.add_argument("--terms", help="comma-separated terms to trace instead of reading truth.json")
    r.add_argument("--out", help="write outputs here instead of stdout")
    r.set_defaults(fn=cmd_reference)

    s = sub.add_parser("score", help="score tool outputs against truth.json")
    s.add_argument("--truth", required=True)
    s.add_argument("--outputs", required=True)
    s.add_argument("--out", help="write the score report here instead of stdout")
    s.set_defaults(fn=cmd_score)
    return p


def main(argv: list[str] | None = None, prog: str = "python -m swarm_mcp.bench") -> int:
    args = build_parser(prog).parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, FileNotFoundError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
