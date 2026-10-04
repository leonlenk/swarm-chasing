"""Derived data (stores, sweeps, setup outputs) must never be committable."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout"
)

IGNORED = [
    "data/swarmscope.duckdb",
    "swarm_mcp/swarmscope.duckdb",
    "swarm_mcp/data/swarmscope.duckdb.wal",
    "sweeps/sw-1.jsonl",
    "swarm_mcp/sweeps/sw-1.labels.jsonl",
    "swarm_mcp/sweeps/notes.txt",
    "mappings/crew.profile.json",
    "swarm_mcp/mappings/crew.setup.json",
    "mappings/crew.task.md",
]
KEPT = ["mappings/crew.json", "swarm_mcp/src/swarm_mcp/sweep.py", "swarm_mcp/src/swarm_mcp/modules/sweep.py"]


def _ignored(path: str) -> bool:
    return subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", path]).returncode == 0


@pytest.mark.parametrize("path", IGNORED)
def test_derived_data_is_ignored(path: str):
    assert _ignored(path)


@pytest.mark.parametrize("path", KEPT)
def test_code_and_mappings_are_not_ignored(path: str):
    assert not _ignored(path)
