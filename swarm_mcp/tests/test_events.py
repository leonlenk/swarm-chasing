"""Shared event ids: format, the source registry, core_get (one id or a batch) and core_info sources."""

from __future__ import annotations

import pytest
from conftest import call, call_error, config_for

from swarm_mcp.events import EventSources, event_record, make_event_id, parse_event_id
from swarm_mcp.server import build_server
from swarm_mcp.toolkit import ToolInputError


def ids(records):
    return [r["event_id"].removeprefix("village:chat:") for r in records]


def test_id_format_round_trip():
    eid = make_event_id("git", "commit", "rpg-game@abc:def")  # local ids may contain ':'
    assert eid == "git:commit:rpg-game@abc:def"
    p = parse_event_id(eid)
    assert (p.source, p.kind, p.local_id) == ("git", "commit", "rpg-game@abc:def") and str(p) == eid
    for bad in ("", "village", "village:chat", "Village:chat:x", "village::x", "1x:chat:x"):
        with pytest.raises(ToolInputError, match="Malformed event_id"):
            parse_event_id(bad)
    with pytest.raises(ValueError):
        make_event_id("village", "Chat", "x")


def test_event_record_shape():
    r = event_record("village:chat:m1", time="2026-01-05T13:00:00Z", actor="GPT-5.2", text="hi", extra_field=1)
    assert list(r)[:8] == ["event_id", "source", "kind", "time", "actor", "actor_type", "location", "text"]
    assert r["source"] == "village" and r["kind"] == "chat" and r["extra_field"] == 1 and "truncated" not in r


def test_duplicate_source_rejected():
    reg = EventSources()
    from swarm_mcp.events import EventSource

    reg.add(EventSource("village", "village", {"chat": ""}, resolve=lambda *a, **k: {}))
    with pytest.raises(ValueError, match="already registered"):
        reg.add(EventSource("village", "other", {"chat": ""}, resolve=lambda *a, **k: {}))


def test_core_info_lists_village_source(app):
    out = call(app, "core_info")
    (src,) = [s for s in out["sources"] if s["source"] == "village"]
    # store-backed: the scope module registers one source per ingested dataset
    assert src["module"] == "scope" and src["kinds"][0]["id_format"] == "village:chat:<id>"


def test_search_results_expand_through_core_get(app):
    hit = call(app, "scope_search", query="same agent")["results"][0]
    assert hit["evidence_id"] == "village:chat:m0005"
    out = call(app, "core_get", ids=hit["evidence_id"], before=2, after=2)
    assert out["event"]["text"].startswith("Opus 4.5 and Claude Opus 4.5")
    assert out["event"]["actor"] == "GPT-5.2" and out["event"]["location"] == "general"
    # context stays in the same room: m0006 is in #rest, so it is skipped
    assert ids(out["before"]) == ["m0003", "m0004"] and ids(out["after"]) == ["m0007", "m0008"]
    assert out["context"] == "previous/next messages in the same room"
    assert any("truncated" in n for n in out["notes"])  # m0007 is the long message


def test_core_get_edges_and_errors(app):
    first = call(app, "core_get", ids="village:chat:m0001", before=5, after=0)
    assert first["before"] == [] and first["after"] == []
    assert "Malformed event_id" in call_error(app, "core_get", ids="m0001")
    err = call_error(app, "core_get", ids="nope:chat:m0001")
    assert "Unknown event source 'nope'" in err and "village" in err
    assert "has no kind 'turn'" in call_error(app, "core_get", ids="village:turn:x")
    assert "No 'chat' record with id 'zzz'" in call_error(app, "core_get", ids="village:chat:zzz")


def test_core_get_batch(app):
    out = call(app, "core_get", ids=["village:chat:m0002", "village:chat:zzz", "bad"], max_chars=80)
    assert ids([r["event"] for r in out["results"]]) == ["m0002"] and len(out["errors"]) == 2
    assert out["errors"][0]["id"] == "village:chat:zzz" and "No 'chat' record" in out["errors"][0]["error"]
    assert "At most 50" in call_error(app, "core_get", ids=["village:chat:m0001"] * 51)


def test_failed_register_drops_its_event_source(tmp_path, fake_modules):
    pkg, add = fake_modules
    add("core", "from swarm_mcp.modules.core import *  # noqa\n")
    add(
        "flaky",
        """
NAME = "flaky"
def register(mcp, ctx):
    @ctx.event_source(kinds={"thing": "a thing"})
    def resolve(kind, local_id, *, before, after, max_chars):
        return {}
    raise RuntimeError("kaboom")
""",
    )
    add(
        "steady",
        """
from swarm_mcp.events import event_record
NAME = "steady"
def register(mcp, ctx):
    @ctx.event_source(kinds={"thing": "a thing"})
    def resolve(kind, local_id, *, before, after, max_chars):
        return {"event": event_record(ctx.event_id(kind, local_id), time=None, actor="x", text=local_id)}
""",
    )
    app = build_server(config_for(tmp_path), package=pkg)
    assert set(app.swarm_registry.events.by_name) == {"steady"}
    out = call(app, "core_get", ids="steady:thing:a:b")
    assert out["event"]["text"] == "a:b" and out["before"] == [] and out["after"] == []
