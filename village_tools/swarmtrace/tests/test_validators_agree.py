"""The three trace validators accept the same traces: swarmtrace.format.validate (Python), trace.schema.json
(jsonschema) and the viewer's TraceData.validate in trace_viz_template.html (run under node when it is installed).

One fixture list, synthetic data only. Each case changes one field of a valid trace. The viewer's one documented
leniency: a required field that is ABSENT may be a warning (it has a fallback); anything present must match the schema.
Python additionally checks what the schema can't express (cross-references, start <= end, real dates, size).
"""

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from swarmtrace.format import validate
from swarmtrace.tests.test_format import tiny

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "trace.schema.json"
TEMPLATE = ROOT.parent / "trace_viz_template.html"
DROP = object()                                       # sentinel: delete the field

# (case, path, new value or DROP, valid?)
CASES = [
    ("as is", (), None, True),
    ("no group", ("agents", 1, "group"), DROP, True),
    ("empty metrics", ("metrics",), {}, True),
    ("empty snippet", ("events", 0, "snippet"), "", True),
    ("microsecond time", ("events", 0, "t"), "2025-01-02T10:00:00.123456Z", True),
    ("version false", ("version",), False, False),
    ("version true", ("version",), True, False),
    ("version string", ("version",), "0", False),
    ("version 1", ("version",), 1, False),
    ("id not a slug", ("id",), "Toy Idea", False),
    ("id number", ("id",), 5, False),
    ("kind unknown", ("kind",), "meme", False),
    ("title empty", ("title",), "", False),
    ("title blank", ("title",), "   ", False),
    ("title number", ("title",), 3, False),
    ("statement null", ("statement",), None, False),
    ("start no Z", ("start",), "2025-01-01T00:00:00", False),
    ("start date only", ("start",), "2025-01-01", False),
    ("start space", ("start",), "2025-01-01 00:00:00Z", False),
    ("unknown top field", ("extra",), 1, False),
    ("events not a list", ("events",), {"e1": {}}, False),
    ("metrics list", ("metrics",), [1], False),
    ("metric bool", ("metrics", "Adopters"), True, False),
    ("metric null", ("metrics", "Adopters"), None, False),
    ("too many metrics", ("metrics",), {f"m{i}": i for i in range(11)}, False),
    ("agent unknown field", ("agents", 0, "colour"), "red", False),
    ("agent name blank", ("agents", 0, "name"), " ", False),
    ("agent lab empty", ("agents", 0, "lab"), "", False),
    ("agent group null", ("agents", 1, "group"), None, False),
    ("agent joined bad", ("agents", 0, "joined"), "yesterday", False),
    ("event id list", ("events", 1, "id"), ["e2"], False),
    ("event id dict", ("events", 1, "id"), {"id": "e2"}, False),
    ("event id number", ("events", 1, "id"), 2, False),
    ("event id blank", ("events", 1, "id"), " ", False),
    ("event conf bool", ("events", 0, "conf"), True, False),
    ("event conf 4", ("events", 0, "conf"), 4, False),
    ("event conf string", ("events", 0, "conf"), "3", False),
    ("event room number", ("events", 0, "room"), 7, False),
    ("event snippet long", ("events", 0, "snippet"), "x" * 221, False),
    ("event snippet null", ("events", 0, "snippet"), None, False),
    ("event channel bad", ("events", 0, "channel"), "email", False),
    ("event unknown field", ("events", 0, "note"), "x", False),
    ("exposure source number", ("exposures", 0, "source"), 1, False),
    ("exposure event list", ("exposures", 0, "event"), ["e1"], False),
    ("exposure via bad", ("exposures", 0, "via"), "smoke", False),
    ("adoption event list", ("adoptions", 0, "event"), ["e2"], False),
    ("adoption event null", ("adoptions", 0, "event"), None, False),
    ("adoption independent string", ("adoptions", 0, "independent"), "false", False),
    ("adoption sources null", ("adoptions", 0, "sources"), None, False),
    ("adoption source number", ("adoptions", 0, "sources"), [1], False),
    ("edge kind bad", ("edges", 0, "kind"), "gossip", False),
    ("edge evidence number", ("edges", 0, "evidence"), 1, False),
    ("persistence where bad", ("persistence", 0, "where"), "chat", False),
    ("annotation kind bad", ("annotations", 0, "kind"), "joke", False),
    ("annotation label blank", ("annotations", 0, "label"), " ", False),
    ("quote text long", ("quotes", 0, "text"), "y" * 161, False),
    ("quote text empty", ("quotes", 0, "text"), "", False),
    ("quote agent null", ("quotes", 0, "agent"), None, False),
    ("quote note number", ("quotes", 0, "note"), 1, False),
    # absent required fields: Python and the schema reject; the viewer may only warn (it has a fallback)
    ("absent version", ("version",), DROP, False),
    ("absent title", ("title",), DROP, False),
    ("absent metrics", ("metrics",), DROP, False),
    ("absent quotes", ("quotes",), DROP, False),
    ("absent lab", ("agents", 0, "lab"), DROP, False),
    ("absent conf", ("events", 0, "conf"), DROP, False),
    ("absent adoption event", ("adoptions", 0, "event"), DROP, False),
]


def _case(path, value):
    tr = tiny()
    if path:
        *head, last = path
        obj = tr
        for k in head:
            obj = obj[k]
        if value is DROP:
            del obj[last]
        else:
            obj[last] = copy.deepcopy(value)
    return tr


FIXTURES = [(name, _case(path, value), ok, value is DROP and path != ("agents", 1, "group")) for name, path, value, ok
            in CASES]
IDS = [f[0] for f in FIXTURES]


@pytest.mark.parametrize("name,trace,ok,absent", FIXTURES, ids=IDS)
def test_python_and_schema_agree(name, trace, ok, absent):
    jsonschema = pytest.importorskip("jsonschema", reason="needs jsonschema: add --with jsonschema")
    v = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    schema_errs = [e.message for e in v.iter_errors(trace)]
    py_errs = validate(trace)
    assert (not py_errs) == ok, py_errs
    assert (not schema_errs) == ok, schema_errs


def _viewer_validate(traces):
    """TraceData.validate from the template, run under node on each trace: [{errors, warnings}]."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed: the viewer's validator is checked statically only")
    html = TEMPLATE.read_text()
    js = re.search(r"<script>\s*(\(function \(\) \{\s*'use strict';.*?)</script>", html, re.S).group(1)
    prog = ("const module = {exports: {}};\n" + js + "\nconst fx = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
            "process.stdout.write(JSON.stringify(fx.map(t => module.exports.validate(t))));\n")
    out = subprocess.run([node, "-e", prog], input=json.dumps(traces), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_viewer_agrees():
    res = _viewer_validate([f[1] for f in FIXTURES])
    wrong = []
    for (name, _, ok, absent), r in zip(FIXTURES, res, strict=True):
        if ok and (r["errors"] or r["warnings"]):
            wrong.append(f"{name}: valid but viewer says {r}")
        elif not ok and not r["errors"] and not (absent and r["warnings"]):
            wrong.append(f"{name}: invalid but viewer accepts it ({r})")
    assert not wrong, "\n".join(wrong)


def test_viewer_validator_does_not_coerce_ids():
    """Node-less check: the viewer's validator must type-check ids and event references, not String() them."""
    html = TEMPLATE.read_text()
    body = html[html.index("function validate(raw)"):html.index("function labKey(")]
    assert "String(" not in body
    assert "typeof e.id !== 'string'" in body and "must be an event id" in body
    assert "raw.version !== 0" in body and "ISO_RX.test(v)" in body and "unknownChk" in body
