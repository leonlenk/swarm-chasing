"""Prerequisite checks and stage dispatch of the AI Village analysis scripts (tracer_hostility.py,
tracer_onboarding.py, memories.py, trace_export.py), on synthetic files only: no dataset is read."""

import datetime as dt

import pytest


def _no_dataset(*a, **k):
    raise AssertionError("the dataset was loaded before the stage checked its inputs")


@pytest.fixture
def th(tmp_path, monkeypatch):
    import tracer_hostility as th
    monkeypatch.setattr(th, "OUT", tmp_path)
    monkeypatch.setattr(th, "BATCH", tmp_path / "label_batches")
    (tmp_path / "label_batches").mkdir()
    for name in ("load_agents", "load_chat"):
        monkeypatch.setattr(th, name, _no_dataset)
    return th


def _cand(cid="c00000"):
    return {"cid": cid, "channel": "chat", "field": "content", "t": dt.datetime(2031, 1, 2, 10), "agent": "alpha",
            "room": "general", "patterns": "hostil", "snippet": "the kettle is hostile", "context": "",
            "is_gemini": False, "env_ctx": True, "rpg_week": False}


def test_hostility_analyze_without_labels_says_how_to_make_them(th, tmp_path):
    th.write_csv(tmp_path / "candidates.csv", [_cand()])
    th.write_csv(tmp_path / "sample.csv", [dict(_cand(), why="other_chat_rand")])
    with pytest.raises(SystemExit) as e:
        th.analyze()
    msg = str(e.value.code)
    assert "labels_*.json" in msg and "python tracer_hostility.py sample" in msg
    assert "hostility_stance_rubric.md" in msg and "python tracer_hostility.py analyze" in msg


def test_hostility_analyze_names_the_missing_stage(th, tmp_path):
    with pytest.raises(SystemExit) as e:
        th.analyze()
    assert "candidates.csv is missing" in str(e.value.code) and "tracer_hostility.py build" in str(e.value.code)


def test_write_csv_with_no_rows(th, tmp_path):
    th.write_csv(tmp_path / "empty.csv", [])
    assert th.read_csv(tmp_path / "empty.csv") == []


def test_hostility_build_needs_the_memory_sample(th, tmp_path, monkeypatch):
    monkeypatch.setattr(th, "MEM_SAMPLE", tmp_path / "cache" / "memory_daily_sample.jsonl.gz")
    with pytest.raises(SystemExit) as e:
        th.build()
    assert "memory_daily_sample.jsonl.gz is missing" in str(e.value.code)
    assert "python memories.py" in str(e.value.code)


@pytest.fixture
def to(tmp_path, monkeypatch):
    import tracer_onboarding as to
    monkeypatch.setattr(to, "OUTD", tmp_path)
    monkeypatch.setattr(to, "LBD", tmp_path / "label_batches")
    monkeypatch.setattr(to, "EVD", tmp_path / "evidence")
    monkeypatch.setattr(to, "MEM", tmp_path / "cache" / "memory_daily_sample.jsonl.gz")
    monkeypatch.setattr(to, "load_msgs", _no_dataset)
    (tmp_path / "label_batches").mkdir()
    return to


@pytest.mark.parametrize("stage", ["stage_guides", "stage_items"])
def test_onboarding_stages_need_the_memory_sample(to, stage):
    with pytest.raises(SystemExit) as e:
        getattr(to, stage)()
    assert "memory_daily_sample.jsonl.gz is missing" in str(e.value.code)
    assert "python memories.py" in str(e.value.code)


def test_memories_checks_ideas_json_before_scanning(tmp_path, monkeypatch):
    import memories
    monkeypatch.setattr(memories, "IDEAS", tmp_path / "ideas.json")
    monkeypatch.setattr(memories, "load_agents", _no_dataset)
    with pytest.raises(SystemExit) as e:
        memories.main()
    assert "ideas.json is missing" in str(e.value.code) and "python ideas.py" in str(e.value.code)


def _term_trace():
    return {"version": 0, "id": "term-toy", "title": "Toy term", "kind": "term", "statement": "A coined toy word.",
            "source": "Synthetic test fixture.", "start": "2031-01-01T00:00:00Z", "end": "2031-01-10T00:00:00Z",
            "agents": [{"name": "alpha", "lab": "LabA", "joined": None, "left": None}],
            "events": [{"id": "e1", "t": "2031-01-02T10:00:00Z", "agent": "alpha", "channel": "chat",
                        "stance": "originates", "conf": None, "room": None, "snippet": "toyword is born"}],
            "exposures": [], "adoptions": [], "edges": [], "persistence": [], "annotations": [], "quotes": [],
            "metrics": {}}


def test_trace_export_skips_sources_with_missing_inputs(tmp_path, monkeypatch, capsys):
    import json

    import trace_export
    from swarmtrace.adapters import aivillage

    def hostility():
        raise FileNotFoundError(2, "No such file or directory", str(tmp_path / "hostility" / "results.json"))

    monkeypatch.setattr(aivillage, "SOURCES", {"hostility": hostility, "terms": lambda: [_term_trace()]})
    out = tmp_path / "traces"
    assert trace_export.main(["--out", str(out)]) == 0
    assert (out / "term-toy.json").exists()
    assert [t["id"] for t in json.loads((out / "index.json").read_text())["traces"]] == ["term-toy"]
    err = capsys.readouterr().err
    assert "skipped source 'hostility'" in err and "results.json is missing" in err and "tracer_hostility.py" in err
    # a single requested source with missing inputs fails cleanly, without a traceback
    assert trace_export.main(["--out", str(out), "--source", "hostility"]) == 1


def _items_csv(path):
    import csv
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["item_id", "group", "agent", "kind", "t", "day_index", "text"])
        w.writeheader()
        w.writerow({"item_id": "I0001", "group": "newcomer", "agent": "alpha", "kind": "chat",
                    "t": "2031-01-02 10:00:00", "day_index": "1", "text": "always verify the kettle"})


def test_onboarding_analyze_refuses_without_labels(to, tmp_path):
    with pytest.raises(SystemExit) as e:
        to.stage_analyze()
    assert "items.csv is missing" in str(e.value.code)
    _items_csv(tmp_path / "items.csv")
    (tmp_path / "label_batches" / "labels_1.jsonl").write_text("")
    with pytest.raises(SystemExit) as e:
        to.stage_analyze()
    msg = str(e.value.code)
    assert "no labels found" in msg and "onboarding_labelling.md" in msg and "tracer_onboarding.py analyze" in msg
    assert not (tmp_path / "results.json").exists() and not (tmp_path / "uptake.png").exists()


def test_onboarding_handcheck_only_from_a_file(to, tmp_path):
    import json
    rows = [{"item_id": "I0001", "rule": "R01", "label": "STATES"}, {"item_id": "I0002", "rule": "R02", "label": "FOLLOWS"}]
    assert to.handcheck_summary(rows) is None
    (tmp_path / "handcheck.json").write_text(json.dumps({"I0001/R01": 1, "I0002/R02": 0, "I0009/R01": 1}))
    hc = to.handcheck_summary(rows)
    assert hc["n"] == 3 and hc["agreement"] == round(2 / 3, 3)
    assert hc["by_label"] == {"STATES": "1/1", "FOLLOWS": "0/1", "?": "1/1"}
