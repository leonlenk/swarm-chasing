#!/usr/bin/env python3
"""Build the idea-spread viewer: embed trace JSON files (format v0) into trace_viz_template.html.

Usage:
    python build_trace_viz.py [TRACE_FILES_OR_DIRS ...] -o OUT.html [--day-one YYYY-MM-DD]

With no inputs it reads out/sprint_idea/traces/ and writes out/sprint_idea/idea_spread.html
(both relative to this script). A directory is read through its index.json when present,
otherwise every *.json in it. An index.json given as a file is expanded the same way.
Files that are not valid UTF-8 JSON, or that the page would reject (check() below mirrors the page's
errors), are reported with the reason and skipped; the rest of the page is still built. Each kept trace is also
run through the swarmtrace validator and any strict-v0 problems are printed as notes: the page reads such traces
leniently (missing fields become warnings) and runs its full field-by-field check again in the browser.

The page is self-contained: the shared paper style, PaperKit (SVG/PNG export) and d3 are
inlined by pagekit.py, so it opens offline from file://.

Village days: with --day-one the page can label time axes "Day N" (day N = the calendar date,
in --day-tz, N-1 days after day one). With no inputs (the bundled AI Village traces) day one
defaults to 2025-04-02, America/Los_Angeles: the AI Village dataset README and SCHEMA say
"day 1 = 2025-04-02", and every daily summary's day number equals its Pacific date minus
2025-04-02, plus one. For any other input the day axis is off unless --day-one is given.

Free text (snippets, quotes, evidence, labels, statement, source) is scrubbed with swarmtrace's scrub, the
same engine the exporter uses: email addresses become [email] and phone-number-like strings [phone]. Addresses at
the AI Village agents' own mailbox domain (swarmtrace.adapters.aivillage.SCRUB_ALLOW_DOMAINS), which the export
keeps on purpose, and at any --keep-email-domain are kept; --keep-emails turns scrubbing off.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pagekit
from swarmtrace import validate as strict_problems
from swarmtrace.adapters.aivillage import SCRUB_ALLOW_DOMAINS
from swarmtrace.format import scrub

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "trace_viz_template.html"
DEFAULT_IN = HERE / "out" / "sprint_idea" / "traces"
DEFAULT_OUT = HERE / "out" / "sprint_idea" / "idea_spread.html"
SCHEMA = HERE / "swarmtrace" / "trace.schema.json"
PLACEHOLDER = "__TRACE_PAYLOAD__"
AI_VILLAGE_DAY_ONE = "2025-04-02"   # AI Village README/SCHEMA: "day 1 = 2025-04-02" (Pacific time)
AI_VILLAGE_TZ = "America/Los_Angeles"
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


def load_json(path: Path):
    """Parse a JSON file; NaN/Infinity are rejected (they would break the page's JSON.parse)."""
    def bad_constant(c):
        raise ValueError(f"{c} is not valid JSON")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=bad_constant)


def from_index(idx: Path) -> list[Path]:
    try:
        data = load_json(idx)
    except (OSError, ValueError, RecursionError) as e:   # ValueError covers bad JSON and bad UTF-8
        print(f"skip: {idx} is not readable JSON ({e})", file=sys.stderr)
        return []
    entries = data.get("traces") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        print(f"skip: {idx} is not an index (expected an object with a 'traces' list)", file=sys.stderr)
        return []
    files = []
    for i, t in enumerate(entries):
        t = t if isinstance(t, dict) else {}
        f = t.get("file") or (f"{t['id']}.json" if isinstance(t.get("id"), str) and t["id"] else None)
        if not isinstance(f, str) or not f:
            print(f"skip: {idx} traces[{i}] has no file name (expected an object with 'file' or 'id')", file=sys.stderr)
            continue
        files.append(idx.parent / f)
    return files


def redact(trace: dict, keep: set[str]) -> int:
    """Scrub emails (outside `keep` domains) and phone numbers from free-text fields, in place, with swarmtrace's
    scrub. Returns the number of fields changed."""
    n = 0

    def sub(text):
        nonlocal n
        if not isinstance(text, str):
            return text
        out = scrub(text, tuple(keep))
        n += out != text
        return out

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
    """Problems that make the page reject a trace (the same rules as its in-browser check, which reports softer
    issues as warnings). Safe on any JSON value: a list or object where text is expected is an error, not a crash."""
    if not isinstance(trace, dict):
        return [f"{name}: not a JSON object"]
    if isinstance(trace.get("traces"), list) and "events" not in trace:
        return [f"{name}: is an index file, not a trace"]
    errs = []
    v = trace.get("version")
    if v is not None and (isinstance(v, bool) or v != 0):
        errs.append(f"{name}: version is {v!r}; expected 0")
    if not isinstance(trace.get("id"), str) or not trace["id"].strip():
        errs.append(f"{name}: id must be a non-empty string")
    if not isinstance(trace.get("kind"), str) or trace["kind"] not in KINDS:
        errs.append(f"{name}: kind {trace.get('kind')!r} is not one of belief, norm, term")
    for k in ("title", "statement", "source"):
        if trace.get(k) is not None and not isinstance(trace[k], str):
            errs.append(f"{name}: {k} must be text")
    lists = {}
    for k in LISTS:
        x = trace.get(k)
        if x is not None and not isinstance(x, list):
            errs.append(f"{name}: {k} must be a list")
        lists[k] = x if isinstance(x, list) else []
        bad = next((i for i, item in enumerate(lists[k]) if not isinstance(item, dict)), None)
        if bad is not None:
            errs.append(f"{name}: {k}[{bad}] must be an object")
    names = set()
    for i, a in enumerate(lists["agents"]):
        if isinstance(a, dict):
            if not isinstance(a.get("name"), str) or not a["name"].strip():
                errs.append(f"{name}: agents[{i}].name must be a non-empty string")
                break
            names.add(a["name"])
    for i, e in enumerate(lists["events"]):
        if isinstance(e, dict) and not (isinstance(e.get("agent"), str) and e["agent"] in names):
            errs.append(f"{name}: events[{i}].agent {e.get('agent')!r} is not in agents")
            break
    return errs


def day_spec(day_one: str | None, tz: str, *, default_on: bool) -> dict | None:
    """The Village-day spec embedded in the page, or None (no day axis)."""
    if day_one is None:
        day_one = AI_VILLAGE_DAY_ONE if default_on else None
    if day_one is None or str(day_one).lower() in ("off", "none", ""):
        return None
    d = date.fromisoformat(str(day_one))
    ZoneInfo(tz)  # raises for an unknown zone
    return {"day_one": d.isoformat(), "tz": tz}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Embed v0 trace files into the idea-spread viewer page.")
    ap.add_argument("inputs", nargs="*", type=Path, help=f"trace files, index.json files or directories (default: {DEFAULT_IN})")
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT, help=f"output HTML (default: {DEFAULT_OUT})")
    ap.add_argument("--keep-email-domain", action="append", default=[], metavar="DOMAIN",
                    help="also leave addresses at this domain unredacted (repeatable); "
                         f"{', '.join(SCRUB_ALLOW_DOMAINS)} (the agents' mailboxes) is always kept")
    ap.add_argument("--keep-emails", action="store_true", help="do not scrub emails or phone numbers")
    ap.add_argument("--schema", type=Path, default=SCHEMA, help="JSON Schema to bundle into the page's format section, if it exists")
    ap.add_argument("--day-one", metavar="YYYY-MM-DD",
                    help="date of Village day 1, so time axes can show 'Day N'; 'off' disables it. Default: "
                         f"{AI_VILLAGE_DAY_ONE} (AI Village README/SCHEMA) when no inputs are given, otherwise off")
    ap.add_argument("--day-tz", default=AI_VILLAGE_TZ, help=f"time zone in which Village days turn over (default {AI_VILLAGE_TZ})")
    args = ap.parse_args(argv)

    inputs = args.inputs or [DEFAULT_IN]
    files = expand(inputs)
    traces, failed, seen_ids = [], 0, {}
    for f in files:
        try:
            t = load_json(f)
        except (OSError, ValueError, RecursionError) as e:   # ValueError covers bad JSON and bad UTF-8
            print(f"skip: {f} is not readable JSON ({e})", file=sys.stderr)
            failed += 1
            continue
        errs = check(t, f.name)
        if errs:
            for m in errs:
                print("skip: " + m, file=sys.stderr)
            failed += 1
            continue
        if t["id"] in seen_ids:
            print(f"skip: {f.name}: trace id {t['id']!r} was already loaded from {seen_ids[t['id']]}", file=sys.stderr)
            failed += 1
            continue
        seen_ids[t["id"]] = f
        strict = strict_problems(t, max_bytes=None)
        if strict:
            print(f"note: {f.name} is not strict v0 ({len(strict)} problems, e.g. {strict[0]}); "
                  "the page reads it leniently", file=sys.stderr)
        red = 0 if args.keep_emails else redact(t, {d.lower() for d in [*SCRUB_ALLOW_DOMAINS, *args.keep_email_domain]})
        traces.append(t)
        print(f"ok:   {f.name}  {t.get('kind'):6} {len(t.get('agents', [])):3d} agents  {len(t.get('events', [])):5d} events  {t.get('title', t['id'])}{f'  ({red} fields scrubbed)' if red else ''}")
    if not traces:
        print(f"No valid traces found in {', '.join(map(str, inputs))}. Export the AI Village traces first with "
              "`python village_tools/trace_export.py` (writes out/sprint_idea/traces/), or pass trace files, "
              "index.json files or directories.", file=sys.stderr)
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

    days = day_spec(args.day_one, args.day_tz, default_on=not args.inputs)
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "traces": traces,
        "schema": schema,
        "schemaPath": schema_path,
        "days": days,
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("<", "\\u003c")  # '<' only occurs inside JSON strings; keeps </script> and <!-- inert
    try:
        html = pagekit.build(TEMPLATE.read_text(), {PLACEHOLDER: blob})
    except ValueError as e:
        print(f"error: {TEMPLATE.name}: {e}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    print(f"wrote {args.out}  ({len(html) / 1e6:.2f} MB, {len(traces)} traces{', schema bundled' if schema else ''}"
          f"{f', Day 1 = ' + days['day_one'] if days else ''}{f', {failed} skipped' if failed else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
