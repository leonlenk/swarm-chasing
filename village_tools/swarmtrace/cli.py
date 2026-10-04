"""swarmtrace command line.

    python -m swarmtrace.cli export --source hostility|onboarding|terms|all [--adapter aivillage] [--out DIR]
                                    [--allow-domain DOMAIN ...]
    python -m swarmtrace.cli validate FILE... [--adapter NAME] [--allow-domain DOMAIN ...]

export runs an adapter's sources, scrubs PII from free text (emails -> [email] except allowlisted domains,
phone-like strings -> [phone]), checks every trace, writes <id>.json into DIR, then rebuilds DIR/index.json from all
trace files in DIR (so exporting one source keeps the others listed). Before writing a source it deletes that
source's previous files (the adapter's OUTPUT_GLOBS), so dropped traces don't linger in the index.

validate checks trace files and index files (an index's listed files must exist next to it).

Both exit non-zero on any error. Warnings (leftover emails/phones, data outside the start/end window) are printed
but don't fail. An adapter may define SCRUB_ALLOW_DOMAINS (emails to keep, e.g. the agents' own addresses) and
TRACES (default output directory).
"""

import argparse
import datetime as dt
import importlib
import json
import sys
from pathlib import Path

from .format import MAX_BYTES, check, dumps, index_entry, iso, scrub_trace, validate, validate_index


def load_adapter(name):
    return importlib.import_module(f"swarmtrace.adapters.{name}")


def _is_index(obj):
    return isinstance(obj, dict) and "traces" in obj and "generated" in obj


def _allow(args, adapter=None):
    return tuple(getattr(adapter, "SCRUB_ALLOW_DOMAINS", ())) + tuple(args.allow_domain or ())


def build_index(out):
    """index.json listing every valid trace file in `out`; unreadable or invalid files are skipped with a warning."""
    entries = []
    for p in sorted(out.glob("*.json")):
        if p.name == "index.json":
            continue
        try:
            tr = json.loads(p.read_text())
        except (OSError, ValueError) as e:            # ValueError covers bad JSON and bad UTF-8
            print(f"  warning: index: skipping {p.name} (unreadable: {e})")
            continue
        if _is_index(tr) or validate(tr):
            print(f"  index: skipping {p.name} (not a valid trace)")
            continue
        entries.append(index_entry(tr, p.name))
    kind_order = {"belief": 0, "norm": 1, "term": 2}
    entries.sort(key=lambda e: (kind_order.get(e["kind"], 9), e["id"]))
    index = {"version": 0, "generated": iso(dt.datetime.now(dt.timezone.utc)), "traces": entries}
    errs = validate_index(index)
    if errs:
        raise SystemExit("index invalid:\n  " + "\n  ".join(errs))
    (out / "index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n")
    return index


def _summary(tr, size):
    n = {k: len(tr[k]) for k in ("agents", "events", "exposures", "adoptions", "edges", "persistence", "annotations",
                                 "quotes")}
    return (f"{tr['id']:<28} {tr['kind']:<6} agents {n['agents']:>3}  events {n['events']:>4}  exposures "
            f"{n['exposures']:>5}  adoptions {n['adoptions']:>3}  edges {n['edges']:>3}  persistence "
            f"{n['persistence']:>3}  annotations {n['annotations']:>3}  quotes {n['quotes']:>3}  {size / 1e6:.2f} MB")


def cmd_export(args):
    adapter = load_adapter(args.adapter)
    sources = list(adapter.SOURCES) if args.source == "all" else [args.source]
    unknown = [s for s in sources if s not in adapter.SOURCES]
    if unknown:
        raise SystemExit(f"unknown source(s) {unknown}; {args.adapter} has {sorted(adapter.SOURCES)}")
    out = Path(args.out) if args.out else getattr(adapter, "TRACES", None)
    if out is None:
        raise SystemExit("--out is required for this adapter")
    out.mkdir(parents=True, exist_ok=True)
    allow = _allow(args, adapter)
    bad = n_warn = 0
    for src in sources:
        traces = adapter.SOURCES[src]()
        for pattern in getattr(adapter, "OUTPUT_GLOBS", {}).get(src, []):
            for old in out.glob(pattern):
                old.unlink()
        for tr in traces:
            scrub_trace(tr, allow)
            errs, warns = check(tr, allow_domains=allow)
            if errs:
                bad += 1
                print(f"{tr.get('id')}: {len(errs)} problems, not written", *errs[:20], sep="\n  ")
                continue
            path = out / f"{tr['id']}.json"
            text = dumps(tr)
            path.write_text(text)
            print(_summary(tr, len(text.encode())))
            for w in warns:
                n_warn += 1
                print(f"  warning: {w}")
    index = build_index(out)
    print(f"index.json: {len(index['traces'])} traces in {out}; {bad} failed, {n_warn} warnings")
    return 1 if bad else 0


def cmd_validate(args):
    allow = _allow(args, load_adapter(args.adapter) if args.adapter else None)
    bad = n_warn = 0
    for f in args.files:
        p = Path(f)
        try:
            obj = json.loads(p.read_text())
        except (OSError, ValueError) as e:
            print(f"FAIL {p}: {e}")
            bad += 1
            continue
        if _is_index(obj):
            errs = validate_index(obj)
            for i, x in enumerate(obj.get("traces") or []):
                if isinstance(x, dict) and isinstance(x.get("file"), str) and not (p.parent / x["file"]).exists():
                    errs.append(f"traces[{i}].file: {x['file']} does not exist")
            what = f"index, {len(obj.get('traces') or [])} traces"
        else:
            errs, warns = check(obj, allow_domains=allow)
            for w in warns:
                n_warn += 1
                print(f"warn {p}: {w}")
            what = f"{obj.get('kind')} trace, {p.stat().st_size / 1e6:.2f} MB" if isinstance(obj, dict) else "?"
        if errs:
            bad += 1
            print(f"FAIL {p} ({what}): {len(errs)} problems", *errs[:50], sep="\n  ")
        else:
            print(f"ok   {p} ({what})")
    print(f"{len(args.files)} files: {bad} failed, {n_warn} warnings")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="swarmtrace", description="Export and validate idea traces (format v0).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="build traces with an adapter and write them plus index.json")
    ex.add_argument("--source", default="all", help="adapter source name, or 'all'")
    ex.add_argument("--adapter", default="aivillage", help="module in swarmtrace.adapters (default: aivillage)")
    ex.add_argument("--out", help="output directory (default: the adapter's TRACES)")
    va = sub.add_parser("validate", help=f"validate trace or index files (traces must be <= {MAX_BYTES:,} bytes)")
    va.add_argument("files", nargs="+")
    va.add_argument("--adapter", help="also allow that adapter's SCRUB_ALLOW_DOMAINS emails")
    for p in (ex, va):
        p.add_argument("--allow-domain", action="append", metavar="DOMAIN",
                       help="email domain to keep when scrubbing / not warn about (repeatable)")
    args = ap.parse_args(argv)
    return {"export": cmd_export, "validate": cmd_validate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
