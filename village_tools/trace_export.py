"""Export the AI Village idea traces (hostility belief, onboarding norms, coined terms) for the visualizer.

Thin wrapper around the swarmtrace CLI; equivalent to
    python -m swarmtrace.cli export --source all --out out/sprint_idea/traces
Extra arguments are passed through, e.g. `python trace_export.py --source terms`.
"""

import sys

from swarmtrace import cli
from swarmtrace.adapters.aivillage import TRACES

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--source" not in args:
        args += ["--source", "all"]
    if "--out" not in args:
        args += ["--out", str(TRACES)]
    sys.exit(cli.main(["export", *args]))
