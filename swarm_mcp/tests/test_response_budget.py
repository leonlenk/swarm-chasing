"""The per-call response budget: tools that return many texts stop at RESPONSE_BUDGET_CHARS and say so.

All data is synthetic: the conftest village plus long messages, periods and findings added in tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from conftest import A_OPUS, call, config_for

from swarm_mcp.server import build_server
from swarm_mcp.toolkit import RESPONSE_BUDGET_CHARS, ResponseBudget

N_BULK = 120
BULK_IDS = [f"village:msg:bulk{i:04d}" for i in range(N_BULK)]
NOTE = "truncated: response budget reached"
SLACK = 3000  # envelope around the budgeted items (filters, notes, counters)


def size(out) -> int:
    return len(json.dumps(out, ensure_ascii=False))


@pytest.fixture
def big_app(data_dir: Path):
    """The synthetic village plus 120 messages of 6,000 chars in channel 'bulk' and 400 periods with long labels."""
    con = duckdb.connect(str(data_dir / "swarmscope.duckdb"))
    try:
        con.execute(
            "INSERT INTO messages SELECT 'village:msg:bulk' || lpad(CAST(i AS TEXT), 4, '0'), 'village', 'bulk', ?, "
            "[], NULL, TIMESTAMP '2026-02-01' + i * INTERVAL 1 MINUTE, 'exact', 'chat', "
            "'bulk report ' || i || ' ' || repeat('lorem ipsum ', 500), '{}' FROM range(?) t(i)",
            [f"village:agent:{A_OPUS}", N_BULK],
        )
        con.execute(
            "INSERT INTO periods SELECT 'village:sprint:s' || lpad(CAST(i AS TEXT), 4, '0'), 'village', 'sprint', "
            "'Sprint ' || i || ' ' || repeat('y', 2000), TIMESTAMP '2026-03-01' + i * INTERVAL 1 HOUR, "
            "TIMESTAMP '2026-03-01' + (i + 1) * INTERVAL 1 HOUR, '{}' FROM range(400) t(i)"
        )
    finally:
        con.close()
    return build_server(config_for(data_dir))


def test_budget_admits_first_item_then_stops():
    b = ResponseBudget(limit=100)
    assert b.admit("x" * 500) is True  # the first item always fits, so paging makes progress
    assert b.admit("y") is False and b.exhausted is True
    assert b.admit("z") is False
    assert b.note("hint").startswith(NOTE) and "hint" in b.note("hint")
    small = ResponseBudget(limit=100)
    assert all(small.admit("a" * 10) for _ in range(5)) and small.exhausted is False


@pytest.mark.parametrize("query", [None, "bulk report"])
def test_search_stops_at_budget_and_pages_on(big_app, query):
    args = {"channel": "bulk", "limit": 200, "max_chars": 20000}
    if query:
        args["query"] = query
    out = call(big_app, "scope_search", **args)
    assert out["total"] == N_BULK and 0 < out["returned"] < N_BULK
    assert out["has_more"] is True and out["next_offset"] == out["returned"]
    assert any(n.startswith(NOTE) for n in out["notes"])
    assert size(out) <= RESPONSE_BUDGET_CHARS + SLACK  # was ~740 KB

    seen, offset = [], 0
    while True:
        page = call(big_app, "scope_search", **args, offset=offset)
        assert size(page) <= RESPONSE_BUDGET_CHARS + SLACK
        seen += [r["evidence_id"] for r in page["results"]]
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert seen == BULK_IDS  # paging with next_offset loses and repeats nothing


def test_core_get_batch_caps_context_and_budget(big_app):
    ids = BULK_IDS[:50]
    out = call(big_app, "core_get", ids=ids, before=50, after=50, max_chars=20000)
    assert size(out) <= RESPONSE_BUDGET_CHARS + SLACK  # was ~1.8 MB
    notes = " ".join(out["notes"])
    assert "before/after capped at 10" in notes and NOTE in notes
    assert out["requested"] == 50 and out["errors"] == [] and 0 < out["returned"] < 50
    assert [r["evidence_id"] for r in out["results"]] + out["not_returned"] == ids
    for r in out["results"]:
        assert len(r["neighbors"]["before"]) <= 10 and len(r["neighbors"]["after"]) <= 10
    assert len(out["results"][-1]["neighbors"]["after"]) == 10

    rest = call(big_app, "core_get", ids=out["not_returned"], max_chars=500)
    assert rest["returned"] == len(out["not_returned"]) and "notes" not in rest and "not_returned" not in rest

    # a single id keeps the full before/after range
    one = call(big_app, "core_get", ids=BULK_IDS[60], before=50, after=50)
    assert len(one["neighbors"]["before"]) == 50 and len(one["neighbors"]["after"]) == 50


def test_small_requests_are_untouched(big_app):
    out = call(big_app, "core_get", ids=BULK_IDS[:3], before=2, after=2)
    assert out["returned"] == 3 and "notes" not in out and "not_returned" not in out
    page = call(big_app, "scope_search", channel="bulk", limit=5)
    assert page["returned"] == 5 and "notes" not in page


def test_periods_page_stops_at_budget(big_app):
    out = call(big_app, "scope_periods", kind="sprint", limit=200)
    assert out["total"] == 400 and 0 < out["returned"] < 200
    assert out["has_more"] is True and out["next_offset"] == out["returned"]
    assert any(n.startswith(NOTE) for n in out["notes"])
    assert size(out) <= RESPONSE_BUDGET_CHARS + SLACK
    nxt = call(big_app, "scope_periods", kind="sprint", limit=200, offset=out["next_offset"])
    assert nxt["periods"][0]["index"] == out["periods"][-1]["index"] + 1


def test_findings_list_stops_at_budget(big_app):
    for i in range(45):
        call(
            big_app,
            "findings_record",
            claim=f"Claim {i}: " + "the bulk reports repeat themselves. " * 55,
            evidence_ids=[BULK_IDS[i]],
        )
    out = call(big_app, "findings_list", limit=200)
    assert out["total_matching"] == 45 and 0 < out["returned"] < 45 and out["has_more"] is True
    assert any(n.startswith(NOTE) for n in out["notes"])
    assert size(out) <= RESPONSE_BUDGET_CHARS + SLACK

    sample = call(big_app, "findings_list", sample=45)
    assert 0 < sample["returned"] < 45 and any(n.startswith(NOTE) for n in sample["notes"])
    assert size(sample) <= RESPONSE_BUDGET_CHARS + SLACK
