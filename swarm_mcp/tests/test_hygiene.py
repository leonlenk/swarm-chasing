"""Source hygiene: no line in the package or its tests looks like a real credential or a personal path."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# an AWS access key id literal; tests build their examples by concatenation instead
AWS_KEY_ID = re.compile("AK" + r"IA[0-9A-Z]{16}")
# a home directory with a real-looking user name (neutral examples: /home/user, /home/x)
HOME_PATH = re.compile(r"/home/(?!(?:user|x)\b)[a-z][a-z0-9_-]*")


def _sources():
    for sub in ("src", "tests"):
        yield from sorted((ROOT / sub).rglob("*.py"))


def test_no_credential_or_personal_path_literals():
    hits = []
    for f in _sources():
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if AWS_KEY_ID.search(line) or HOME_PATH.search(line):
                hits.append(f"{f.relative_to(ROOT)}:{n}")
    assert hits == []


def test_no_stale_command_or_tool_names_in_src():
    """Removed surface: `swarm-mcp ingest` is now `swarm-mcp add`, scope_list_sources is core_info."""
    stale = re.compile(r"swarm-mcp ingest\b|\bscope_list_sources\b")
    hits = [
        f"{f.relative_to(ROOT)}:{n}"
        for f in sorted((ROOT / "src").rglob("*.py"))
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
        if stale.search(line)
    ]
    assert hits == []
