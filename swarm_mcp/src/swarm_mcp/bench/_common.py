"""Shared helpers for the synthetic benchmark: ids, timestamps, gzip JSONL, confusables."""

from __future__ import annotations

import gzip
import io
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SOURCE = "village"

# Evidence ids: ``<source>:<kind>:<native_id>``, the SwarmScope store's ids
# (``swarm_mcp.scope.evidence``, schema v2). Chat messages are ``msg``; older bench
# outputs spelled them ``chat``, so the scorer treats ``chat`` as an alias of ``msg``.
KIND_ALIASES = {"chat": "msg"}


def chat_eid(uuid: str) -> str:
    return f"{SOURCE}:msg:{uuid}"


def event_eid(uuid: str) -> str:
    return f"{SOURCE}:event:{uuid}"


def agent_eid(uuid: str) -> str:
    return f"{SOURCE}:agent:{uuid}"


def goal_eid(uuid: str) -> str:
    return f"{SOURCE}:goal:{uuid}"


def normalize_eid(value: Any) -> str | None:
    """Canonical form of an evidence id (``chat`` kind -> ``msg``); None for empty input."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    parts = s.split(":", 2)
    if len(parts) == 3:
        parts[1] = KIND_ALIASES.get(parts[1], parts[1])
        return ":".join(parts)
    return s


TS_FMT = "%Y-%m-%d %H:%M:%S.%f"


def fmt_ts(ts: datetime) -> str:
    """Dataset timestamp format: naive UTC, microseconds, space separator."""
    return ts.strftime(TS_FMT)


def parse_ts(value: Any) -> datetime | None:
    """Parse dataset or ISO timestamps (``T``/space, optional ``Z`` or offset); returns naive UTC.

    An aware value is converted to UTC first; a naive one is taken to be UTC already."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def write_jsonl_gz(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Write gzipped JSONL with a fixed gzip header (mtime 0, no name) so output is byte-deterministic."""
    n = 0
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with io.TextIOWrapper(gz, encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=False) + "\n")
                n += 1
    return n


def read_jsonl_gz(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


# Latin -> Cyrillic look-alikes (a small subset of Unicode confusables.txt).
LATIN_TO_CYRILLIC = {
    "a": "а",
    "c": "с",
    "e": "е",
    "o": "о",
    "p": "р",
    "x": "х",
    "y": "у",
    "i": "і",
    "A": "А",
    "B": "В",
    "C": "С",
    "E": "Е",
    "H": "Н",
    "K": "К",
    "M": "М",
    "O": "О",
    "P": "Р",
    "T": "Т",
    "X": "Х",
}
CYRILLIC_TO_LATIN = {v: k for k, v in LATIN_TO_CYRILLIC.items()}


def skeleton(name: str) -> str:
    """Confusable-folded form: NFKC, Cyrillic look-alikes -> Latin, casefold, drop spaces/punctuation."""
    s = unicodedata.normalize("NFKC", name)
    s = "".join(CYRILLIC_TO_LATIN.get(ch, ch) for ch in s)
    return re.sub(r"[\s\-_.]+", "", s.casefold())


def name_pattern(name: str) -> re.Pattern[str]:
    """Case-insensitive whole-word pattern for a display name."""
    return re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])", re.IGNORECASE)
