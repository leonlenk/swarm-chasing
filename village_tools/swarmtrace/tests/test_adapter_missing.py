"""The AI Village adapter degrades gracefully when local inputs are missing (synthetic data only)."""

import datetime as dt

import pytest

from swarmtrace.adapters import aivillage
from swarmtrace.adapters.aivillage import quotes_from_ids, require, story_notes


def test_story_notes_skip_missing_relapse_and_retractions(capsys):
    # Every mutation quote missing locally: quotes_from_ids skips them all, and the notes must not crash.
    mutation = quotes_from_ids([("c00001", "relapse", 0, 4), ("c00002", "retraction", 0, 4)], {}, {})
    assert mutation == []
    assert story_notes([], mutation, dates=("2025-01-02",)) == []
    out = capsys.readouterr().out
    assert "no retraction found on 2025-01-02" in out and "no relapse quote" in out


def test_story_notes_keep_what_is_present():
    t1, t2 = dt.datetime(2025, 1, 2, 9), dt.datetime(2025, 1, 2, 8)
    retr = [{"t": t1, "reason": "takes back the claim"}]
    mutation = [{"t": t2, "agent": "alpha", "stage": "retraction (own words)", "quote": "earlier take-back"},
                {"t": t1, "agent": "alpha", "stage": "relapse", "quote": "says it again"}]
    notes = story_notes(retr, mutation, dates=("2025-01-02", "2025-02-03"))
    assert [n["label"] for n in notes] == ["Gemini 2.5 Pro retracts: earlier take-back", "Relapse: says it again"]
    assert notes[0]["t"] == "2025-01-02T08:00:00Z"


def test_require_names_the_producing_script(tmp_path):
    with pytest.raises(SystemExit, match=r"memory_daily_sample.*python3 memories.py"):
        require(tmp_path / "memory_daily_sample.jsonl.gz", "memories.py")
    require(tmp_path, "unused.py")                    # present: no error


def test_hostility_without_memory_cache_names_memories_py(tmp_path, monkeypatch):
    for f in ("results.json", "labels.csv", "candidates.csv", "adoption.csv", "exposure_events.csv"):
        (tmp_path / f).write_text("")
    monkeypatch.setattr(aivillage, "HOST", tmp_path)
    monkeypatch.setattr(aivillage, "MEMORY_CACHE", tmp_path / "memory_daily_sample.jsonl.gz")
    with pytest.raises(SystemExit, match="python3 memories.py"):
        aivillage.hostility()
