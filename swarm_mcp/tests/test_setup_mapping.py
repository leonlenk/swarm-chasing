"""Declarative mappings: heuristic drafts (+ small fixes) pass the check, and MappedAdapter
yields valid standard records on three synthetic dataset shapes."""

from __future__ import annotations

import builtins

import pytest
from setup_datasets import AGENTS_A, MEMBERS_C, make_csv_chat, make_nested_jsonl, make_sqlite_board

from swarm_mcp.scope import evidence
from swarm_mcp.scope.records import event_record
from swarm_mcp.setup.agent import draft_mapping
from swarm_mcp.setup.check import run_check
from swarm_mcp.setup.mapping import MappedAdapter, MappingError, get_path, row_matches, validate_spec
from swarm_mcp.setup.profile import profile_path
from swarm_mcp.setup.protocol_bridge import Adapter, AgentRecord, PeriodRecord, StandardRecord


def _valid(rec: StandardRecord, source: str) -> dict:
    eid = evidence.parse(rec.event_id)
    assert eid.source == source and eid.kind in ("msg", "event")
    assert eid.native_id == f"{rec.kind}/{rec.local_id}"  # the dataset kind always prefixes the local id
    d = rec.as_event_record()
    assert d == event_record(
        rec.event_id,
        time=rec.time,
        actor=rec.actor,
        actor_type=rec.actor_type,
        location=rec.location,
        text=rec.text,
        type=rec.type,
        recipients=rec.recipients or None,
        reply_to=rec.reply_to,
        ts_quality=rec.ts_quality,
        meta=rec.meta or None,
        **{"msg_type" if eid.kind == "msg" else "action_kind": rec.kind},
    )
    assert d["kind"] == eid.kind
    if rec.reply_to:
        evidence.parse(rec.reply_to)
    for r in rec.recipients:
        assert evidence.parse(r).kind == "agent"
    return d


def test_get_path_nested_arrays_and_literal_dotted_keys():
    row = {"a": {"b": 1}, "list": [{"id": "x"}, {"id": "y"}, {"no": 1}], "user.name": "lit"}
    assert get_path(row, "a.b") == 1
    assert get_path(row, "list[].id") == ["x", "y"]
    assert get_path(row, "list.id") == ["x", "y"]
    assert get_path(row, "user.name") == "lit"
    assert get_path(row, "a.missing.deeper") is None
    assert row_matches({"k": "1"}, [{"field": "k", "value": 1}])
    assert not row_matches({"k": "1"}, [{"field": "k", "op": "!=", "value": 1}])
    assert row_matches({"k": "b"}, [{"field": "k", "op": "in", "value": ["a", "b"]}, {"field": "z", "op": "missing"}])


def test_nested_jsonl_draft_passes_and_records_are_standard(tmp_path):
    root = make_nested_jsonl(tmp_path / "a")
    spec = draft_mapping(profile_path(root), "crew")
    assert validate_spec(spec) == []
    report = run_check(spec, root, full=True)
    assert report["status"] == "pass", report["problems"]
    assert report["counts"]["by_kind"] == {"utterance": 300}
    assert report["rates"]["actor_unmatched"] == pytest.approx(0.02)  # the visitor rows

    ad = MappedAdapter(spec, root)
    assert isinstance(ad, Adapter)
    recs = list(ad.records())
    assert len(recs) == 300
    for r in recs:
        _valid(r, "crew")
    first = recs[0]
    assert first.event_id == "crew:msg:utterance/u-00000" and first.kind == "utterance" and first.local_id == "u-00000"
    assert first.time == "2026-01-05T14:00:00Z" and first.location in {"lobby", "lab", "ops"}
    assert first.actor.startswith("crew:agent:p-") and first.actor_type == "agent"
    replies = [r for r in recs if r.reply_to]
    assert replies and all(r.reply_to.startswith("crew:msg:utterance/u-") for r in replies)
    assert any(r.recipients for r in recs)
    agents = {a.local_id: a for a in ad.agents()}
    assert set(agents) == {pid for pid, _, _ in AGENTS_A}
    assert agents["p-nova"].display_name == "Nova" and agents["p-nova"].aliases == ["nov"]
    assert agents["p-nova"].first_seen and agents["p-nova"].first_seen <= agents["p-nova"].last_seen
    visitors = [r for r in recs if r.actor == "external:visitor-1"]
    assert len(visitors) == 6 and all(v.actor_type == "human" for v in visitors)


def test_csv_draft_plus_small_fixes_passes(tmp_path):
    root = make_csv_chat(tmp_path / "b")
    spec = draft_mapping(profile_path(root), "irc")
    assert spec["agents"] == {"derive_from_actors": True}
    rec = spec["records"][0]
    assert rec["local_id"] == "@row" and any("local_id" in n for n in spec["notes"])  # low confidence -> TODO
    assert rec["time"] == {"field": "sent_epoch_ms", "format": "epoch_ms"}
    # the small fixes a person (or agent) makes after reading the TODOs:
    rec["local_id"] = "MsgNo"
    rec["recipients"] = {"field": "mentions", "split": ";"}
    rec["kind"] = "line"
    rec["reply_to"]["kind"] = "line"
    report = run_check(spec, root, full=True)
    assert report["status"] == "pass", report["problems"]
    assert not [p for p in report["problems"] if p["code"] == "reply_dangling"]
    recs = list(MappedAdapter(spec, root).records())
    for r in recs:
        _valid(r, "irc")
    r4 = next(r for r in recs if r.local_id == "4")
    assert r4.event_id == "irc:msg:line/4" and r4.reply_to == "irc:msg:line/3" and r4.location.startswith("#")
    assert r4.time.startswith("2026-01-05T14:0")
    assert any(r.recipients for r in recs)
    assert all(r.actor.startswith("irc:agent:") for r in recs)


def test_sqlite_draft_passes_with_lookup_location(tmp_path):
    root = make_sqlite_board(tmp_path / "c")
    spec = draft_mapping(profile_path(root), "board")
    assert spec["agents"]["from"] == "board.sqlite#members"
    lk = next(iter(spec["lookups"].values()))
    assert lk == {"from": "board.sqlite#threads", "key": "thread_key", "value": "title"}
    report = run_check(spec, root, full=True)
    assert report["status"] == "pass", report["problems"]
    ad = MappedAdapter(spec, root)
    recs = list(ad.records())
    assert len(recs) == 240
    for r in recs:
        _valid(r, "board")
    r = next(x for x in recs if x.reply_to)
    assert r.reply_to.startswith("board:msg:post/10")
    assert all(x.location.startswith("Thread about") for x in recs)
    assert {a.display_name for a in ad.agents()} == {m[1] for m in MEMBERS_C}


def test_periods_and_filters(tmp_path):
    root = make_sqlite_board(tmp_path / "c")
    spec = {
        "source": "board",
        "agents": {
            "from": "board.sqlite#members",
            "id": "member_key",
            "display_name": "screen_name",
            "where": [{"field": "is_bot", "value": 1}],
        },
        "records": [
            {
                "from": "board.sqlite#posts",
                "kind": "post",
                "local_id": "post_key",
                "time": "posted_unix",
                "actor": {"field": "author_ref", "match": "id", "unmatched_prefix": "human:"},
                "text": {"fields": ["body_md", "post_key"], "sep": " | "},
                "meta": {"thread": "thread_ref"},
                "where": [{"field": "thread_ref", "op": "in", "value": [1, 2, 3]}],
            }
        ],
        "periods": [
            {
                "from": "board.sqlite#threads",
                "kind": "thread",
                "local_id": "thread_key",
                "label": "title",
                "start": {"field": "opened_ts", "format": "epoch_s"},
            }
        ],
    }
    ad = MappedAdapter(spec, root)
    items = list(ad.load())
    recs = [x for x in items if isinstance(x, StandardRecord)]
    assert recs and all(r.meta["thread"] in (1, 2, 3) for r in recs)
    assert all(" | " in r.text for r in recs)
    humans = [r for r in recs if r.actor and r.actor.startswith("human:m-tur")]
    assert humans and all(r.actor_type is None or r.actor_type == "human" or r.actor_type is None for r in humans)
    assert len([x for x in items if isinstance(x, AgentRecord)]) == 3  # is_bot filter
    periods = [x for x in items if isinstance(x, PeriodRecord)]
    assert (
        len(periods) == 12
        and periods[0].event_id.startswith("board:period:thread/")
        and periods[0].kind == "thread"
        and periods[0].start_time.endswith("Z")
    )
    assert ad.kinds["thread"].table == "periods" and ad.kinds["agent"].table == "agents"
    assert [ad.kinds[k].schema_kind for k in ("post", "thread", "agent")] == ["msg", "period", "agent"]


def test_ids_use_schema_kinds_and_never_collide(tmp_path):
    """Two message kinds from the same rows share the schema kind 'msg' but not ids; reply_to.kind names a
    dataset kind and the target id gets that kind's schema kind."""
    root = make_sqlite_board(tmp_path / "c")
    threads = {
        "from": "board.sqlite#threads",
        "local_id": "thread_key",
        "time": {"field": "opened_ts", "format": "epoch_s"},
    }
    spec = {
        "source": "board",
        "agents": {"from": "board.sqlite#members", "id": "member_key", "display_name": "screen_name"},
        "records": [
            {
                "from": "board.sqlite#posts",
                "kind": "post",
                "local_id": "post_key",
                "time": {"field": "posted_unix", "format": "epoch_s"},
                "actor": {"field": "author_ref", "match": "id"},
                "text": "body_md",
                "reply_to": {"field": "thread_ref", "kind": "open"},
            },
            {**threads, "kind": "title", "text": "title", "actor": {"field": "opened_by", "match": "id"}},
            {**threads, "kind": "opener", "text": "opened_by"},
            {**threads, "kind": "open", "category": "action", "text": "title"},
        ],
    }
    report = run_check(spec, root, full=True)
    assert report["status"] == "pass", report["problems"]
    assert not [p for p in report["problems"] if p["code"] in ("id_duplicate", "reply_dangling", "id_unparseable")]
    recs = list(MappedAdapter(spec, root).records())
    ids = [r.event_id for r in recs]
    assert len(ids) == len(set(ids)) == 240 + 3 * 12
    assert {"board:msg:title/1", "board:msg:opener/1", "board:event:open/1"} <= set(ids)
    post = next(r for r in recs if r.kind == "post")
    assert post.event_id.startswith("board:msg:post/") and post.reply_to.startswith("board:event:open/")
    for r in recs:
        _valid(r, "board")


def test_invalid_specs_are_rejected_with_reasons(tmp_path):
    bad = {"source": "Bad Source", "records": [{"from": "x", "kind": "Chat!", "local_id": "id"}]}
    errs = validate_spec(bad)
    assert any("source" in e for e in errs) and any("kind" in e for e in errs)
    dup = {
        "source": "s",
        "records": [{"from": "a", "kind": "k", "local_id": "id"}, {"from": "b", "kind": "k", "local_id": "id"}],
    }
    assert any("unique" in e for e in validate_spec(dup))
    with pytest.raises(MappingError):
        MappedAdapter(bad, tmp_path)
    with pytest.raises(MappingError, match="no table matches"):
        list(
            MappedAdapter(
                {"source": "s", "records": [{"from": "nope.csv", "kind": "k", "local_id": "id"}]}, tmp_path
            ).records()
        )


def test_spec_content_is_never_evaluated(tmp_path, monkeypatch):
    """Code-looking strings in a spec are only ever used as field names, values or patterns."""
    root = make_csv_chat(tmp_path / "b")
    marker = tmp_path / "pwned"
    payload = f"__import__('os').system('touch {marker}')"

    real_eval, real_exec = builtins.eval, builtins.exec

    def guard(real):
        def inner(src, *a, **k):
            if isinstance(src, (str, bytes)):  # source text; the import system passes code objects
                raise AssertionError(f"source text reached eval/exec: {src!r:.80}")
            return real(src, *a, **k)

        return inner

    monkeypatch.setattr(builtins, "eval", guard(real_eval))
    monkeypatch.setattr(builtins, "exec", guard(real_exec))
    spec = {
        "source": "irc",
        "description": payload,
        "agents": {"derive_from_actors": True},
        "lookups": {"x": {"from": "chatlog_export.csv", "key": payload, "value": payload}},
        "records": [
            {
                "from": "chatlog_export.csv",
                "kind": "msg",
                "local_id": "MsgNo",
                "time": {"field": "sent_epoch_ms", "format": "strptime", "pattern": payload},
                "actor": {"field": "from_nick", "unmatched_prefix": payload},
                "location": {"field": payload, "lookup": "x", "values": {payload: payload}, "default": payload},
                "text": {"fields": ["said", payload], "sep": payload},
                "recipients": {"field": "mentions", "split": payload},
                "where": [{"field": payload, "op": "missing"}],
                "meta": {payload: payload},
            }
        ],
    }
    report = run_check(spec, root, full=True)
    assert not marker.exists()
    codes = {p["code"] for p in report["problems"]}
    assert "field_missing" in codes and "time_unparseable" in codes
    recs = list(MappedAdapter(spec, root).records())
    assert recs and all(r.location == payload for r in recs)  # used as a plain default string
    assert not marker.exists()


_BOARD_MULTI_FROM = {
    "source": "board",
    "agents": {"from": "board.sqlite#members", "id": "member_key", "display_name": "screen_name"},
    "lookups": {"thread_of": {"from": "board.sqlite#posts", "key": "post_key", "value": "thread_ref"}},
    "records": [
        {"from": "board.sqlite#posts", "kind": "post", "local_id": "post_key", "text": "body_md",
         "time": {"field": "posted_unix", "format": "epoch_s"}, "actor": {"field": "author_ref", "match": "id"}},
        {"from": "board.sqlite#threads", "kind": "open", "category": "action", "local_id": "thread_key",
         "time": {"field": "opened_ts", "format": "epoch_s"}, "text": "title"},
    ],
}  # fmt: skip


def test_discover_runs_once_for_all_froms(tmp_path, monkeypatch):
    """Three distinct 'from' patterns share one walk of the dataset root."""
    from swarm_mcp.setup import readers

    root = make_sqlite_board(tmp_path / "board")
    calls = []
    real = readers.discover
    monkeypatch.setattr(readers, "discover", lambda r: calls.append(r) or real(r))
    a = MappedAdapter(_BOARD_MULTI_FROM, root)
    items = list(a.load())
    assert len(calls) == 1 and sum(isinstance(i, StandardRecord) for i in items) == 240 + 12
    t1, t2 = a.tables("board.sqlite#posts")[0], a.tables("board.sqlite#threads")[0]
    assert t1.key != t2.key and a.tables("board.sqlite#posts")[0] is t1  # per-pattern cache kept


def test_lookup_truncation_is_reported_in_stats(tmp_path, monkeypatch):
    from swarm_mcp.setup import mapping

    root = make_sqlite_board(tmp_path / "board")
    a = MappedAdapter(_BOARD_MULTI_FROM, root)
    assert len(a._load_lookups()["thread_of"]) == 240 and not a.stats  # under the cap: nothing reported
    monkeypatch.setattr(mapping, "LOOKUP_MAX_ROWS", 50)
    a = MappedAdapter(_BOARD_MULTI_FROM, root)
    assert len(a._load_lookups()["thread_of"]) == 50
    assert a.stats == {"lookup_thread_of_truncated_at_50_rows": 1}
