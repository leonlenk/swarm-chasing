"""AI Village adapter pieces that run without the dataset, on synthetic text only."""

import ast
import datetime as dt
import re
from pathlib import Path

from swarmtrace.adapters.aivillage import quotes_from_ids

VT = Path(__file__).resolve().parents[2]


def test_quotes_from_ids_reads_text_from_the_lookup(capsys):
    t = dt.datetime(2025, 1, 2, 10, 0, 0)
    cands = {"c00001": {"t": t, "agent": "alpha"}, "c00002": {"t": t, "agent": "beta"},
             "c00003": {"t": t, "agent": "beta"}}
    texts = {"c00001": "Status: the printer\nis haunted again, says alpha.", "c00002": "too short"}
    specs = [("c00001", "origin", 8, 36), ("c00002", "span past the end", 0, 40), ("c00003", "no text", 0, 4),
             ("c00009", "no candidate", 0, 4)]
    out = quotes_from_ids(specs, cands, texts)
    assert out == [{"cid": "c00001", "t": t, "agent": "alpha", "stage": "origin", "quote": "the printer is haunted again"}]
    printed = capsys.readouterr().out
    assert all(f"quote {c} not found" in printed for c in ("c00002", "c00003", "c00009"))
    assert "printer" not in printed                    # skip messages never echo text


def test_mutation_quotes_hold_ids_and_spans_only():
    """tracer_hostility.MUTATION_QUOTES must not carry agent text: each entry is (cid, stage label, start, end)."""
    tree = ast.parse((VT / "tracer_hostility.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "MUTATION_QUOTES")
    specs = ast.literal_eval(node.value)
    assert specs
    for spec in specs:
        cid, stage, start, end = spec
        assert re.fullmatch(r"c\d{5}", cid) and isinstance(stage, str) and len(stage) <= 80
        assert isinstance(start, int) and isinstance(end, int) and 0 <= start < end
