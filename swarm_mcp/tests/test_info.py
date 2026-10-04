"""`swarm-mcp info` text: a source's date span covers its messages and its actions. All data is synthetic."""

from __future__ import annotations

import json
from pathlib import Path

from swarm_mcp import info
from swarm_mcp.scope import db
from swarm_mcp.scope.ingest import ingest_mapped

ACTIONS_ONLY = {
    "source": "acts",
    "records": [
        {
            "from": "acts.jsonl",
            "kind": "deploy",
            "category": "action",
            "local_id": "id",
            "time": {"field": "at", "format": "iso"},
            "actor": {"field": "who", "unmatched_prefix": "human:"},
            "text": "note",
        }
    ],
}


def _info_text(store: Path) -> str:
    with db.connect(store) as s:
        sources = info.store_sources(s)
    return info.format_info(
        {
            "server": {"version": "x"},
            "config": {
                "config_file": None,
                "data_dir": "d",
                "db_path": "db",
                "db_exists": True,
                "findings_dir": "f",
                "sweeps_dir": "s",
                "llm": {"model": "m", "effort": "low", "api_key_set": False},
            },
            "modules": {"loaded": [], "skipped": []},
            "sources": sources,
            "findings": {"ok": True, "message": "ok"},
        }
    )


def _line(text: str, source: str) -> str:
    return next(line for line in text.splitlines() if line.strip().startswith(source + " "))


def test_info_span_falls_back_to_actions(store_path: Path, tmp_path: Path):
    """Regression: a source with only actions showed '? .. ?' because the span used messages alone."""
    root = tmp_path / "acts"
    root.mkdir()
    rows = [
        {"id": "d1", "at": "2026-02-01T08:00:00Z", "who": "ops", "note": "deploy one"},
        {"id": "d2", "at": "2026-02-03T17:30:00Z", "who": "ops", "note": "deploy two"},
    ]
    (root / "acts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    mapping = tmp_path / "acts.json"
    mapping.write_text(json.dumps(ACTIONS_ONLY))
    res = ingest_mapped(mapping, root, store_path)
    assert res["counts"]["actions"] == 2 and res["counts"]["messages"] == 0

    text = _info_text(store_path)
    acts = _line(text, "acts")
    assert "0 messages, 2 actions" in acts and "2026-02-01T08:00:00Z .. 2026-02-03T17:30:00Z" in acts
    assert "?" not in acts
    # a source with both spans the earliest and latest of the two (village actions start after its messages)
    with db.connect(store_path) as s:
        m = s.one("SELECT min(ts) AS lo, max(ts) AS hi FROM messages WHERE source = 'village'")
        a = s.one("SELECT min(ts) AS lo, max(ts) AS hi FROM actions WHERE source = 'village'")
    lo, hi = min(m["lo"], a["lo"]), max(m["hi"], a["hi"])
    span = f"{lo:%Y-%m-%dT%H:%M:%SZ} .. {hi:%Y-%m-%dT%H:%M:%SZ}"
    assert span in _line(text, "village")


def test_info_span_unknown_without_records():
    src = {"source": "empty", "row_counts": {}, "messages_ts": {"min": None, "max": None}, "actions_ts": {}}
    assert info._span(src) == "? .. ?"
