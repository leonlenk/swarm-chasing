"""Shared pieces for the self-contained HTML figure pages (timeline, series, quote cards).

- ``asset(name)``: the shared paper style (``assets/paper.css``) and export helpers
  (``assets/paperkit.js``), inlined into every page so pages work offline from ``file://``.
- ``json_for_script(obj)``: JSON that is safe inside ``<script type="application/json">``.
- ``fill(template, parts)``: one-pass token substitution; substituted (data-derived) text is
  never re-scanned for tokens.
- Village days: the AI Village numbers days from day 1 = 2025-04-02 in Pacific time
  (dataset README and SCHEMA, "Day numbering"; checked against the dataset's 380 daily
  summaries, whose ``summary_target`` day number equals Pacific date - 2025-04-02 + 1 in
  every case). In the store that date is the start of the first ``village_goal`` period,
  so ``village_days(store)`` derives day 1 from the data: the Pacific date of the earliest
  village goal. Stores without village goals get no day axis unless ``day_one`` is given.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ASSETS = Path(__file__).with_name("assets")
VILLAGE_TZ = "America/Los_Angeles"
VILLAGE_PERIOD_KIND = "village_goal"


@cache
def asset(name: str) -> str:
    if name not in ("paper.css", "paperkit.js"):
        raise ValueError(f"unknown asset {name!r}")
    return (ASSETS / name).read_text(encoding="utf-8")


def json_for_script(obj: Any) -> str:
    """JSON safe to place inside a <script> element: nothing can close the tag."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (
        s.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def fill(template: str, parts: dict[str, str]) -> str:
    """Replace each ``__NAME__`` token for NAME in parts, in one pass over the template only."""
    rx = re.compile("__(" + "|".join(re.escape(k) for k in parts) + ")__")
    return rx.sub(lambda m: parts[m.group(1)], template)


def iso_ms(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------- village days


def day_spec(day_one: str | date, tz: str = VILLAGE_TZ, basis: str = "") -> dict[str, str]:
    d = day_one if isinstance(day_one, date) else date.fromisoformat(str(day_one)[:10])
    ZoneInfo(tz)  # validate
    return {"day_one": d.isoformat(), "tz": tz, "basis": basis}


def village_days(store: Any, day_one: str | bool | None = None, tz: str = VILLAGE_TZ) -> dict[str, str] | None:
    """The day-axis spec for a store, or None.

    ``day_one``: None derives it from the store (earliest village goal, Pacific date);
    an ISO date sets it; False turns the day axis off.
    """
    if day_one is False:
        return None
    if day_one:
        return day_spec(str(day_one), tz, "given")
    try:
        row = store.one(
            "SELECT min(start_ts) AS t FROM periods WHERE kind = ? AND start_ts IS NOT NULL", [VILLAGE_PERIOD_KIND]
        )
    except Exception:  # noqa: BLE001 - a store without periods simply has no day axis
        return None
    t = (row or {}).get("t")
    if t is None:
        return None
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return day_spec(t.astimezone(ZoneInfo(tz)).date(), tz, "first village goal")


def day_number(ts: datetime | int | float, spec: dict[str, str]) -> int:
    """Village day of a timestamp (datetime, naive = UTC, or epoch ms)."""
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    elif ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(ZoneInfo(spec["tz"])).date()
    return (local - date.fromisoformat(spec["day_one"])).days + 1


def day_range_label(t0: Any, t1: Any, spec: dict[str, str] | None) -> str | None:
    """'Day 423' or 'Days 423–444' for a span, or None without a day spec."""
    if not spec or t0 is None or t1 is None:
        return None
    a, b = day_number(t0, spec), day_number(t1, spec)
    return f"Day {a}" if a == b else f"Days {a}–{b}"


def day_start_utc(n: int, spec: dict[str, str]) -> datetime:
    """UTC instant at which Village day n begins (local midnight)."""
    d = date.fromisoformat(spec["day_one"]) + timedelta(days=n - 1)
    return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(spec["tz"])).astimezone(timezone.utc)
