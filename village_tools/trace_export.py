"""Export the AI Village idea traces (hostility belief, onboarding norms, coined terms) for the visualizer.

Wrapper around the swarmtrace CLI; like
    python -m swarmtrace.cli export --source all --out out/sprint_idea/traces
but it exports one source at a time, so a source whose inputs have not been produced yet is skipped with a
warning (naming the missing file and the script that makes it) and the other sources are still exported. A
skipped source's previously exported traces, if any, are left in place. Exit status: 1 when nothing was exported
or a trace failed validation, else 0. Extra arguments are passed through, e.g. `python trace_export.py --source terms`.

Inputs, by source (run these first, from village_tools/; each script's docstring lists its stages):
    terms       out/ideas.json                   python ideas.py
    hostility   out/sprint_idea/hostility/       tracer_hostility.py build, sample, sample2, LLM labels, analyze
    onboarding  out/sprint_idea/onboarding/      tracer_onboarding.py events, guides, rules, items, LLM labels, analyze
hostility and onboarding also need out/cache/memory_daily_sample.jsonl.gz from memories.py (after ideas.py).
"""

import argparse
import sys

from swarmtrace import cli

HOW = {
    "terms": "Produce it with `python ideas.py`.",
    "hostility": "Produce it with the tracer_hostility.py stages (build, sample, sample2, LLM labelling, analyze).",
    "onboarding": "Produce it with the tracer_onboarding.py stages (events, guides, rules, items, LLM labelling, "
                  "analyze).",
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--source", default="all")
    ap.add_argument("--adapter", default="aivillage")
    known, _ = ap.parse_known_args(argv)
    rest, drop = [], False
    for a in argv:                                   # everything but --source, which is set per run below
        if drop:
            drop = False
        elif a == "--source":
            drop = True
        elif not a.startswith("--source="):
            rest.append(a)
    adapter = cli.load_adapter(known.adapter)
    sources = list(adapter.SOURCES) if known.source == "all" else [known.source]

    exported, skipped, rc = [], [], 0
    for src in sources:
        try:
            rc = cli.main(["export", *rest, "--source", src]) or rc
        except FileNotFoundError as e:
            hint = HOW.get(src, "") if known.adapter == "aivillage" else ""
            print(f"warning: skipped source {src!r}: its input {e.filename or e} is missing. {hint} "
                  "Its previously exported traces, if any, are left as they were.", file=sys.stderr)
            skipped.append(src)
            continue
        exported.append(src)
    if skipped:
        print(f"exported: {', '.join(exported) or 'nothing'}; skipped (missing inputs): {', '.join(skipped)}",
              file=sys.stderr)
    return rc if exported else 1


if __name__ == "__main__":
    sys.exit(main())
