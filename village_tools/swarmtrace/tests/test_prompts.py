"""The LLM prompts in village_tools/prompts/ against the code that reads their output.

Each prompt must name every label or scale its reader accepts, and its synthetic output example must parse into
the shape that reader expects. The constants are read from the source with `ast`, since importing the analysis
scripts creates out/ directories and pulls in heavy dependencies.

Run from the repo root: uv run --no-project --with pytest --with jsonschema pytest village_tools/swarmtrace/tests
"""

import ast
import json
import re
from pathlib import Path

import pytest

from swarmtrace.format import STANCES

VT = Path(__file__).resolve().parents[2]
PROMPTS = VT / "prompts"
FILES = ["hostility_stance_rubric.md", "onboarding_rule_extraction.md", "onboarding_labelling.md",
         "coherence_judge.md", "README.md"]


def const(path, name):
    """The literal value assigned to a top-level name in a Python source file."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


HOST_LABELS = const(VT / "tracer_hostility.py", "LABELS")
UPTAKE = const(VT / "tracer_onboarding.py", "UPTAKE")
N_STANCE = const(VT / "swarmtrace" / "adapters" / "aivillage.py", "N_STANCE")
H_STANCE = const(VT / "swarmtrace" / "adapters" / "aivillage.py", "H_STANCE")
SCALES = const(VT / "coherence.py", "SCALES")
ONB_LABELS = set(N_STANCE) | set(UPTAKE) | {"NA"}     # load_labels() drops "NA"


def text(name):
    return (PROMPTS / name).read_text()


def example(name):
    """The fenced block right after the prompt's <!-- output-example --> marker, parsed."""
    m = re.search(r"<!-- output-example -->\s*```(json|jsonl)\n(.*?)\n```", text(name), re.S)
    assert m, f"{name}: no output example"
    lang, body = m.groups()
    return [json.loads(line) for line in body.splitlines() if line.strip()] if lang == "jsonl" else json.loads(body)


@pytest.mark.parametrize("name", FILES)
def test_prompt_file_exists(name):
    assert (PROMPTS / name).is_file() and text(name).strip()


@pytest.mark.parametrize("name, reader", [("hostility_stance_rubric.md", "load_labels"),
                                          ("onboarding_labelling.md", "load_labels"),
                                          ("onboarding_rule_extraction.md", "stage_rules"),
                                          ("coherence_judge.md", "analyse_judged")])
def test_prompt_names_its_reader_and_says_it_is_reconstructed(name, reader):
    assert reader in text(name)
    assert "reconstructed" in text(name).lower()


def test_constants_parsed():
    # Guards the ast parsing: these are what the readers accept today.
    assert "ENDORSES" in HOST_LABELS and set(H_STANCE) <= set(HOST_LABELS)
    assert {"STATES", "FOLLOWS", "VIOLATES", "MUTATED"} <= set(N_STANCE) and set(UPTAKE) <= set(N_STANCE)
    assert "overall" in SCALES


def test_hostility_rubric_covers_labels_and_output_shape():
    t = text("hostility_stance_rubric.md")
    for label in HOST_LABELS:
        assert f"`{label}`" in t, label
    rows = example("hostility_stance_rubric.md")
    assert isinstance(rows, list) and rows
    for r in rows:
        assert set(r) == {"id", "label", "confidence", "reason"}
        assert r["label"] in HOST_LABELS and r["confidence"] in (1, 2, 3)
        int(r["confidence"])                        # analyze() reads int(x["confidence"])
    assert {r["label"] for r in rows} == set(HOST_LABELS)   # one example per class
    assert len({r["id"] for r in rows}) == len(rows)


def test_onboarding_labelling_covers_labels_and_output_shape():
    t = text("onboarding_labelling.md")
    for label in ONB_LABELS:
        assert f"`{label}`" in t, label
    rows = example("onboarding_labelling.md")
    assert rows
    for d in rows:
        assert set(d) == {"item_id", "guide_ref", "labels"} and re.fullmatch(r"I\d{4}", d["item_id"])
        for lab in d["labels"]:
            assert set(lab) == {"rule", "label", "conf", "version"}
            assert re.fullmatch(r"R\d\d", lab["rule"]) and lab["label"] in ONB_LABELS and lab["conf"] in (1, 2, 3)
            assert bool(lab["version"]) == (lab["label"] == "MUTATED") and len(lab["version"]) <= 160
    assert any(not d["labels"] for d in rows)          # an item with no rule still gets a line


def test_onboarding_rule_extraction_output_shape():
    t = text("onboarding_rule_extraction.md")
    assert "RULES" in t and "TIP_EDGES" in t and "rules_batch_" in t
    out = example("onboarding_rule_extraction.md")
    assert set(out) == {"rules", "tips"}
    shorts = set()
    for r in out["rules"]:
        assert {"short", "wording", "guides", "first_stated", "evidence"} <= set(r) <= \
            {"short", "wording", "guides", "first_stated", "evidence", "keyword_proxy"}
        assert re.fullmatch(r"[a-z_]+", r["short"]) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["first_stated"])
        assert all(len(e["quote"].split()) <= 25 for e in r["evidence"])
        if "keyword_proxy" in r:
            re.compile(r["keyword_proxy"])
        shorts.add(r["short"])
    for tip in out["tips"]:
        assert set(tip) == {"giver", "recipient", "t", "rules", "quote"} and set(tip["rules"]) <= shorts
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", tip["t"])


def test_coherence_judge_covers_scales_and_output_shape():
    t = text("coherence_judge.md")
    for s in SCALES:
        assert f"`{s}`" in t, s
    assert "judge_items_" in t and "judge_ratings_" in t
    rows = example("coherence_judge.md")
    assert isinstance(rows, list) and rows
    for r in rows:
        assert set(r) == {"item_id", *SCALES} and re.fullmatch(r"x\d{3}", r["item_id"])
        assert all(r[s] in (1, 2, 3, 4, 5) for s in SCALES)


def test_readme_lists_prompts_and_every_trace_stance():
    t = text("README.md")
    for name in FILES[:-1]:
        assert name in t, name
    for s in STANCES:
        assert f"`{s}`" in t, s
    for f in ("labels_manual.json", "handcheck.json", "HANDCHECK", "judge_key.json"):
        assert f in t, f
