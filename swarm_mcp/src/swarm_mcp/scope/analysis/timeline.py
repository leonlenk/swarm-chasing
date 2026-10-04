"""Activity over time: message/action counts per UTC time bucket, optionally grouped.

Pure functions of a ``Store`` plus already-resolved filters (a resolved channel
name, an exact author id or ``db.HUMAN`` for all humans, and ``parse_time``
strings for the half-open [since, until) window). Callers resolve user input
with ``Store.resolve_channel`` / ``Store.author_filter`` first.

Only whitelisted identifiers (bin, table, group column) are interpolated into
SQL; every value is a bound parameter.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from swarm_mcp.scope.db import HUMAN, Store, date_filters, label_for
from swarm_mcp.toolkit import ToolInputError

BINS = ("hour", "day", "week", "month")
GROUP_BY = ("none", "channel", "author")
# table -> its author column (actions are attributed to the acting agent)
AUTHOR_COLUMN = {"messages": "author_id", "actions": "agent_id"}
MAX_BUCKETS = 2000
NO_GROUP = "(none)"


def ts_iso(value: Any, *, micro: bool = False) -> str | None:
    """A DuckDB timestamp/date value -> ISO string with an explicit Z (UTC)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ" if micro else "%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def record_filters(
    table: str,
    *,
    source: str | None = None,
    channel: str | None = None,
    author_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> tuple[list[str], list[Any]]:
    """WHERE fragments and params for the common scope filters on ``messages`` or ``actions``.

    ``author_id`` is an exact author/agent id, or ``db.HUMAN`` ('human') for every
    human author. ``channel`` only applies to messages."""
    if table not in AUTHOR_COLUMN:
        raise ToolInputError(f"table must be one of {', '.join(AUTHOR_COLUMN)}, not {table!r}")
    where: list[str] = []
    params: list[Any] = []
    if source:
        where.append("source = ?")
        params.append(source)
    if channel is not None:
        if table != "messages":
            raise ToolInputError("channel only applies to table='messages' (actions have no channel); drop it")
        where.append("channel = ?")
        params.append(channel)
    if author_id:
        col = AUTHOR_COLUMN[table]
        if author_id == HUMAN:
            where.append(f"{col} LIKE 'human:%'")
        else:
            where.append(f"{col} = ?")
            params.append(author_id)
    w, p = date_filters("ts", since, until)
    return where + w, params + p


def timeline(
    store: Store,
    *,
    bin: str = "day",
    group_by: str = "none",
    table: str = "messages",
    source: str | None = None,
    channel: str | None = None,
    author_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    top_groups: int = 10,
    max_buckets: int = MAX_BUCKETS,
) -> dict[str, Any]:
    """Counts per ``date_trunc(bin, ts)`` bucket (UTC), empty buckets omitted.

    Returns ``{table, bin, group_by, total, bins_returned, peak, first_bucket,
    last_bucket, series | groups + other, notes}``. With grouping, ``groups``
    holds the ``top_groups`` groups by total (each with its own sparse series)
    and ``other`` sums the rest. Raises ToolInputError when the number of
    non-empty buckets exceeds ``max_buckets``.
    """
    if bin not in BINS:
        raise ToolInputError(f"bin must be one of {', '.join(BINS)}, not {bin!r}")
    if group_by not in GROUP_BY:
        raise ToolInputError(f"group_by must be one of {', '.join(GROUP_BY)}, not {group_by!r}")
    if group_by == "channel" and table != "messages":
        raise ToolInputError("group_by='channel' only applies to table='messages'; use group_by='author' for actions")
    where, params = record_filters(table, source=source, channel=channel, author_id=author_id, since=since, until=until)
    w = " AND ".join(["ts IS NOT NULL", *where])
    bucket = f"date_trunc('{bin}', ts)"  # bin is whitelisted above

    overall = store.all(
        f"SELECT {bucket} AS bucket, count(*) AS n FROM {table} WHERE {w} GROUP BY 1 ORDER BY 1", params
    )
    if len(overall) > max_buckets:
        coarser = {"hour": "day", "day": "week", "week": "month"}.get(bin)
        hint = f"use bin='{coarser}'" if coarser else "use a coarser bin"
        raise ToolInputError(
            f"timeline would return {len(overall)} non-empty '{bin}' buckets (max {max_buckets}). "
            f"Either {hint} or narrow the window with since/until."
        )
    total = sum(r["n"] for r in overall)
    peak = max(overall, key=lambda r: r["n"]) if overall else None  # earliest bucket wins ties
    out: dict[str, Any] = {
        "table": table,
        "bin": bin,
        "group_by": group_by,
        "total": total,
        "bins_returned": len(overall),
        "peak": {"bucket": ts_iso(peak["bucket"]), "count": peak["n"]} if peak else None,
        "first_bucket": ts_iso(overall[0]["bucket"]) if overall else None,
        "last_bucket": ts_iso(overall[-1]["bucket"]) if overall else None,
    }
    notes = [
        "Buckets are UTC and labelled by their start; empty buckets are omitted (a missing bucket means 0).",
        "Rows without a timestamp are excluded.",
    ]
    if bin == "week":
        notes.append("Week buckets start on Monday (ISO weeks).")

    if group_by == "none":
        out["series"] = [{"bucket": ts_iso(r["bucket"]), "count": r["n"]} for r in overall]
        out["notes"] = notes
        return out

    gcol = "channel" if group_by == "channel" else AUTHOR_COLUMN[table]  # whitelisted
    gexpr = f"coalesce({gcol}, '{NO_GROUP}')"
    totals = store.all(
        f"SELECT {gexpr} AS g, count(*) AS n FROM {table} WHERE {w} GROUP BY 1 ORDER BY n DESC, g", params
    )
    top = totals[: max(1, top_groups)]
    keys = [r["g"] for r in top]
    series: dict[str, list[dict[str, Any]]] = {k: [] for k in keys}
    if keys:
        rows = store.all(
            f"SELECT {gexpr} AS g, {bucket} AS bucket, count(*) AS n FROM {table} "
            f"WHERE {w} AND list_contains(?, {gexpr}) GROUP BY 1, 2 ORDER BY 1, 2",
            [*params, keys],
        )
        for r in rows:
            series[r["g"]].append({"bucket": ts_iso(r["bucket"]), "count": r["n"]})
    names = store.display_names() if group_by == "author" else {}
    out["groups"] = [
        {
            "group": label_for(r["g"], names) if group_by == "author" else r["g"],
            "group_id": r["g"],
            "total": r["n"],
            "series": series[r["g"]],
        }
        for r in top
    ]
    rest = totals[len(top) :]
    out["other"] = {"groups": len(rest), "total": sum(r["n"] for r in rest)}
    out["groups_total"] = len(totals)
    notes.append(f"groups = the top {len(top)} {group_by} values by total; 'other' sums the remaining {len(rest)}.")
    out["notes"] = notes
    return out
