"""Command-line entry points fail with a message naming the fix, not a traceback (no dataset needed)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # village_tools/, for build_viz

import build_viz  # noqa: E402
from swarmtrace import cli  # noqa: E402


@pytest.mark.parametrize("argv", [["export", "--adapter", "nope"], ["validate", "x.json", "--adapter", "nope"]])
def test_unknown_adapter_lists_valid_ones(argv):
    with pytest.raises(SystemExit, match=r"unknown adapter 'nope'; valid adapters: aivillage"):
        cli.main(argv)


def test_build_viz_names_the_scripts_for_missing_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(build_viz, "OUT", tmp_path)
    (tmp_path / "ideas.json").write_text("{}")
    with pytest.raises(SystemExit) as e:
        build_viz.main()
    msg = str(e.value)
    assert "python3 cooperation.py" in msg and "python3 memories.py" in msg and "ideas.py" not in msg
    assert e.value.code != 0                           # a message string exits with status 1
