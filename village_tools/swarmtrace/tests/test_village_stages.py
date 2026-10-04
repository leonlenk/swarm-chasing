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
