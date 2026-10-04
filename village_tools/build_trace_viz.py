#!/usr/bin/env python3
"""Build the idea-spread viewer: embed trace JSON files (format v0) into trace_viz_template.html.

Usage:
    python build_trace_viz.py [TRACE_FILES_OR_DIRS ...] -o OUT.html

With no inputs it reads out/sprint_idea/traces/ and writes out/sprint_idea/idea_spread.html
(both relative to this script). A directory is read through its index.json when present,
otherwise every *.json in it. An index.json given as a file is expanded the same way.
Files that are not valid JSON, or fail the basic v0 checks below, are reported and skipped;
the page runs the full field-by-field check again in the browser.

Email addresses in free text (snippets, quotes, evidence, labels) are replaced with [email]
unless their domain is passed with --keep-email-domain; --keep-emails turns this off.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "trace_viz_template.html"
DEFAULT_IN = HERE / "out" / "sprint_idea" / "traces"
DEFAULT_OUT = HERE / "out" / "sprint_idea" / "idea_spread.html"
SCHEMA = HERE / "swarmtrace" / "trace.schema.json"
PLACEHOLDER = "__TRACE_PAYLOAD__"
KINDS = {"belief", "norm", "term"}
LISTS = ("agents", "events", "exposures", "adoptions", "edges", "persistence", "annotations", "quotes")


def expand(paths: list[Path]) -> list[Path]:
    """Turn the CLI inputs into an ordered, de-duplicated list of trace files."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            idx = p / "index.json"
            out.extend(from_index(idx) if idx.exists() else
                       sorted(f for f in p.glob("*.json") if f.name != "index.json" and not f.name.endswith(".schema.json")))
        elif p.name == "index.json":
            out.extend(from_index(p))
        elif p.exists():
            out.append(p)
        else:
            print(f"skip: {p} does not exist", file=sys.stderr)
    seen, uniq = set(), []
    for f in out:
        r = f.resolve()
        if r not in seen:
            seen.add(r)
            uniq.append(f)
    return uniq


def from_index(idx: Path) -> list[Path]:
    try:
        data = json.loads(idx.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"skip: {idx} is not readable JSON ({e})", file=sys.stderr)
        return []
    files = []
    for t in data.get("traces", []):
        f = t.get("file") or (f"{t['id']}.json" if t.get("id") else None)
        if not f:
            print(f"skip: an entry in {idx} has neither file nor id", file=sys.stderr)
            continue
        files.append(idx.parent / f)
    return files


EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")


def redact(trace: dict, keep: set[str]) -> int:
    """Replace email addresses in free-text fields, in place. Returns the number replaced."""
    n = 0

    def sub(text):
        nonlocal n
        if not isinstance(text, str):
            return text

        def r(m):
            nonlocal n
            if m.group(1).lower() in keep:
                return m.group(0)
            n += 1
            return "[email]"
        return EMAIL.sub(r, text)

    for key, fields in (("events", ("snippet",)), ("quotes", ("text", "note")), ("edges", ("evidence",)), ("annotations", ("label",))):
        for item in trace.get(key) or []:
            if isinstance(item, dict):
                for f in fields:
                    if f in item:
                        item[f] = sub(item[f])
    for f in ("statement", "source"):
        if f in trace:
            trace[f] = sub(trace[f])
    return n


def check(trace, name: str) -> list[str]:
    """Basic v0 checks; the page does the full validation."""
    if not isinstance(trace, dict):
        return [f"{name}: not a JSON object"]
    errs = []
    if trace.get("version", 0) != 0:
        errs.append(f"{name}: version is {trace.get('version')!r}; expected 0")
    if not isinstance(trace.get("id"), str) or not trace["id"].strip():
        errs.append(f"{name}: id must be a non-empty string")
    if trace.get("kind") not in KINDS:
        errs.append(f"{name}: kind {trace.get('kind')!r} is not one of belief, norm, term")
    for k in LISTS:
        if k in trace and not isinstance(trace[k], list):
            errs.append(f"{name}: {k} must be a list")
    names = {a.get("name") for a in trace.get("agents", []) if isinstance(a, dict)}
    for i, e in enumerate(trace.get("events", []) or []):
        if isinstance(e, dict) and e.get("agent") not in names:
            errs.append(f"{name}: events[{i}].agent {e.get('agent')!r} is not in agents")
            break
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Embed v0 trace files into the idea-spread viewer page.")
    ap.add_argument("inputs", nargs="*", type=Path, help=f"trace files, index.json files or directories (default: {DEFAULT_IN})")
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT, help=f"output HTML (default: {DEFAULT_OUT})")
    ap.add_argument("--keep-email-domain", action="append", default=[], metavar="DOMAIN",
                    help="leave addresses at this domain unredacted (repeatable), e.g. agent mailboxes")
    ap.add_argument("--keep-emails", action="store_true", help="do not redact email addresses")
    ap.add_argument("--schema", type=Path, default=SCHEMA, help="JSON Schema to bundle into the page's format section, if it exists")
    args = ap.parse_args(argv)

    inputs = args.inputs or [DEFAULT_IN]
    files = expand(inputs)
    traces, failed = [], 0
    for f in files:
        try:
            t = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"skip: {f} ({e})", file=sys.stderr)
            failed += 1
            continue
        errs = check(t, f.name)
        if errs:
            for m in errs:
                print("skip: " + m, file=sys.stderr)
            failed += 1
            continue
        red = 0 if args.keep_emails else redact(t, {d.lower() for d in args.keep_email_domain})
        traces.append(t)
        print(f"ok:   {f.name}  {t.get('kind'):6} {len(t.get('agents', [])):3d} agents  {len(t.get('events', [])):5d} events  {t.get('title', t['id'])}{f'  ({red} emails redacted)' if red else ''}")
    if not traces:
        print("No valid traces found. Pass trace files or a directory, e.g. out/sprint_idea/mock_trace.json", file=sys.stderr)
        return 1

    schema = None
    if args.schema and args.schema.exists():
        try:
            schema = json.loads(args.schema.read_text())
        except json.JSONDecodeError as e:
            print(f"note: {args.schema} is not valid JSON ({e}); page built without it", file=sys.stderr)
    try:
        schema_path = str(args.schema.resolve().relative_to(HERE.parent)) if schema else None
    except ValueError:
        schema_path = str(args.schema) if schema else None

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "traces": traces,
        "schema": schema,
        "schemaPath": schema_path,
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("<", "\\u003c")  # '<' only occurs inside JSON strings; keeps </script> and <!-- inert
    tpl = TEMPLATE.read_text()
    if tpl.count(PLACEHOLDER) != 1:
        print(f"error: {TEMPLATE.name} must contain {PLACEHOLDER} exactly once", file=sys.stderr)
        return 1
    html = tpl.replace(PLACEHOLDER, blob)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    print(f"wrote {args.out}  ({len(html) / 1e6:.2f} MB, {len(traces)} traces{', schema bundled' if schema else ''}"
          f"{f', {failed} skipped' if failed else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
