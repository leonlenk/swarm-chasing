"""Timestamp parsing shared by the profiler, the mapped adapter and the check.

Formats (``format`` in a mapping's time spec):

| format      | accepts                                                            |
|-------------|--------------------------------------------------------------------|
| ``auto``    | any of the below, epoch unit picked by magnitude                   |
| ``iso``     | ISO 8601 (``2026-01-05T14:00:00Z``, ``2026-01-05 14:00:00.123``)    |
| ``epoch_s`` / ``epoch_ms`` / ``epoch_us`` | numbers or digit strings             |
| ``rfc2822`` | ``Mon, 05 Jan 2026 14:00:00 +0000`` (mail headers)                 |
| ``strptime``| with ``pattern``, e.g. ``%d/%m/%Y %H:%M``                          |

Results are naive UTC ``datetime``s (naive inputs are taken as UTC);
``iso_z`` renders them as ``2026-01-05T14:00:00Z``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

PLAUSIBLE_MIN = datetime(1990, 1, 1)
PLAUSIBLE_MAX = datetime(2100, 1, 1)
FORMATS = ("auto", "iso", "epoch_s", "epoch_ms", "epoch_us", "rfc2822", "strptime")
_NUM = re.compile(r"^-?\d+(\.\d+)?$")
_ISOISH = re.compile(r"^\d{4}-\d{2}-\d{2}")
_EPOCH_DIV = {"epoch_s": 1, "epoch_ms": 1e3, "epoch_us": 1e6}


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def epoch_unit(x: float) -> str:
    """Guess the epoch unit from the magnitude (seconds until 5138 AD, then ms, us)."""
    a = abs(x)
    if a < 1e11:
        return "epoch_s"
    if a < 1e14:
        return "epoch_ms"
    return "epoch_us"


def _from_epoch(x: float, unit: str) -> datetime:
    return datetime.fromtimestamp(x / _EPOCH_DIV[unit], tz=timezone.utc).replace(tzinfo=None)


def _iso(s: str) -> datetime:
    s = s.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    return _naive_utc(datetime.fromisoformat(s))


def to_datetime(value: Any, fmt: str = "auto", pattern: str | None = None) -> datetime | None:
    """Parse ``value``; ``None`` for missing/empty values, ``ValueError`` when present but unparseable."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"not a timestamp: {value!r}")
    if isinstance(value, datetime):
        return _naive_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if fmt == "strptime":
        if not pattern:
            raise ValueError("format 'strptime' needs a pattern")
        return _naive_utc(datetime.strptime(str(value).strip(), pattern))
    is_num = isinstance(value, (int, float)) or (isinstance(value, str) and bool(_NUM.match(value.strip())))
    if fmt in _EPOCH_DIV:
        if not is_num:
            raise ValueError(f"not an epoch number: {value!r}")
        try:
            return _from_epoch(float(value), fmt)
        except (OverflowError, OSError, ValueError):
            raise ValueError(f"out of range: {value!r} as {fmt} (wrong epoch unit?)") from None
    if fmt == "iso":
        return _iso(str(value))
    if fmt == "rfc2822":
        return _naive_utc(parsedate_to_datetime(str(value)))
    if fmt != "auto":
        raise ValueError(f"unknown time format {fmt!r}; use one of {', '.join(FORMATS)}")
    if is_num:
        x = float(value)
        return _from_epoch(x, epoch_unit(x))
    s = str(value).strip()
    try:
        return _iso(s)
    except ValueError:
        pass
    try:
        return _naive_utc(parsedate_to_datetime(s))
    except (TypeError, ValueError, IndexError):
        raise ValueError(f"unparseable timestamp: {value!r}") from None


def detect_format(value: Any) -> str | None:
    """The format a single value is in, if it parses to a plausible date (1990–2100); else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (datetime, date)):
        return "native"
    if isinstance(value, (int, float)) or (isinstance(value, str) and _NUM.match(value.strip())):
        x = float(value)
        unit = epoch_unit(x)
        try:
            dt = _from_epoch(x, unit)
        except (OverflowError, OSError, ValueError):
            return None
        return unit if PLAUSIBLE_MIN <= dt < PLAUSIBLE_MAX else None
    if not isinstance(value, str) or len(value) > 64:
        return None
    s = value.strip()
    if _ISOISH.match(s):
        try:
            dt = _iso(s)
        except ValueError:
            return None
        return "iso" if PLAUSIBLE_MIN <= dt < PLAUSIBLE_MAX else None
    if re.search(r"\d{1,2} \w{3} \d{4} \d{2}:\d{2}", s):
        try:
            dt = _naive_utc(parsedate_to_datetime(s))
        except (TypeError, ValueError, IndexError):
            return None
        return "rfc2822" if PLAUSIBLE_MIN <= dt < PLAUSIBLE_MAX else None
    return None


def plausible(dt: datetime) -> bool:
    return PLAUSIBLE_MIN <= dt < PLAUSIBLE_MAX


def iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat() + "Z"
