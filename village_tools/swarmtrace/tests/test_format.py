"""Tests for the trace v0 validator, helpers and CLI on a tiny synthetic (non-AI-Village) trace.

Run from village_tools/:  uv run --no-project --with pytest --with jsonschema pytest swarmtrace/tests -q
"""

import copy
import json
from pathlib import Path

import pytest

from swarmtrace import cli
from swarmtrace.format import (check, clip, dumps, fit_window, index_entry, iso, parse_iso, pii_hits, scrub,
                               scrub_trace, scrub_tracking, validate, validate_index)

SCHEMA = Path(__file__).resolve().parents[1] / "trace.schema.json"


def tiny():
    return {
        "version": 0, "id": "toy-idea", "title": "Toy idea", "kind": "belief",
        "statement": "Bots believe the printer is haunted.", "source": "Hand-written test fixture.",
        "start": "2025-01-01T00:00:00Z", "end": "2025-01-10T00:00:00Z",
        "agents": [{"name": "alpha", "lab": "LabA", "joined": "2025-01-01T00:00:00Z", "left": None},
                   {"name": "beta", "lab": "LabB", "group": "newcomer", "joined": None, "left": None}],
        "events": [{"id": "e1", "t": "2025-01-02T10:00:00Z", "agent": "alpha", "channel": "chat",
                    "stance": "originates", "conf": 3, "room": "general", "snippet": "The printer is haunted."},
                   {"id": "e2", "t": "2025-01-03T10:00:00Z", "agent": "beta", "channel": "memory",
                    "stance": "endorses", "conf": None, "room": None, "snippet": "alpha says it is haunted"}],
        "exposures": [{"t": "2025-01-02T10:00:00Z", "agent": "beta", "source": "alpha", "via": "room", "event": "e1"},
                      {"t": "2025-01-02T12:00:00Z", "agent": "beta", "source": None, "via": "search", "event": None}],
        "adoptions": [{"agent": "beta", "t": "2025-01-03T10:00:00Z", "event": "e2", "independent": False,
                       "sources": ["alpha"]}],
        "edges": [{"from": "alpha", "to": "beta", "t": "2025-01-03T10:00:00Z", "kind": "transmission",
                   "evidence": "same room"}],
        "persistence": [{"agent": "beta", "start": "2025-01-03T00:00:00Z", "end": "2025-01-05T23:59:59Z",
                         "where": "memory"}],
        "annotations": [{"t": "2025-01-01T00:00:00Z", "label": "Goal: fix the printer", "kind": "goal"}],
        "quotes": [{"t": "2025-01-02T10:00:00Z", "agent": "alpha", "text": "The printer is haunted.", "note": None}],
        "metrics": {"Adopters": 1, "Share adopting": "50%"},
    }


def test_tiny_trace_is_valid():
    assert check(tiny()) == ([], [])


def test_round_trip():
    tr = tiny()
    back = json.loads(dumps(tr))
    assert back == tr
    assert validate(back) == []
    entry = index_entry(back, "toy-idea.json")
    index = {"version": 0, "generated": "2025-02-01T00:00:00Z", "traces": [entry]}
    assert validate_index(json.loads(json.dumps(index))) == []
    assert (entry["n_agents"], entry["n_events"]) == (2, 2)


@pytest.mark.parametrize("path,value,expect", [
    (("events", 0, "agent"), "gamma", "events[0].agent: 'gamma' not in agents"),
    (("edges", 0, "to"), "gamma", "edges[0].to: 'gamma' not in agents"),
    (("adoptions", 0, "sources"), ["gamma"], "adoptions[0].sources[0]: 'gamma' not in agents"),
    (("exposures", 0, "source"), "gamma", "exposures[0].source: 'gamma' not in agents"),
    (("quotes", 0, "agent"), "gamma", "quotes[0].agent: 'gamma' not in agents"),
    (("adoptions", 0, "event"), "e9", "adoptions[0].event: 'e9' not an event id"),
])
def test_bad_references(path, value, expect):
    tr = tiny()
    obj = tr
    for k in path[:-1]:
        obj = obj[k]
    obj[path[-1]] = value
    assert expect in validate(tr)


@pytest.mark.parametrize("bad", ["2025-01-02 10:00:00", "2025-01-02T10:00:00", "2025-01-02T10:00:00+00:00",
                                 "2025-13-02T10:00:00Z", "yesterday", None, 1735812000])
def test_bad_timestamps(bad):
    tr = tiny()
    tr["events"][0]["t"] = bad
    errs = validate(tr)
    assert any(e.startswith("events[0].t:") for e in errs), errs


@pytest.mark.parametrize("path,value,expect", [
    (("agents", 0, "name"), ["alpha"], "agents[0].name: expected string, got list"),
    (("agents", 0, "name"), {"n": "alpha"}, "agents[0].name: expected string, got dict"),
    (("events", 0, "id"), ["e1"], "events[0].id: expected a non-empty string"),
    (("events", 0, "id"), {"id": "e1"}, "events[0].id: expected a non-empty string"),
    (("events", 0, "agent"), ["alpha"], "events[0].agent: ['alpha'] not in agents"),
    (("edges", 0, "from"), {"name": "alpha"}, "edges[0].from: {'name': 'alpha'} not in agents"),
    (("adoptions", 0, "sources"), [["alpha"]], "adoptions[0].sources[0]: ['alpha'] not in agents"),
    (("adoptions", 0, "event"), ["e2"], "adoptions[0].event: ['e2'] not an event id"),
    (("exposures", 0, "event"), {"id": "e1"}, "exposures[0].event: {'id': 'e1'} not an event id"),
    (("quotes", 0, "agent"), ["alpha"], "quotes[0].agent: ['alpha'] not in agents"),
])
def test_unhashable_ids_and_names_are_errors(path, value, expect):
    tr = tiny()
    obj = tr
    for k in path[:-1]:
        obj = obj[k]
    obj[path[-1]] = value
    errs = validate(tr)                                 # used to raise TypeError (unhashable type)
    assert expect in errs, errs


@pytest.mark.parametrize("bad", [["toy-idea"], {"id": "toy-idea"}])
def test_index_unhashable_id_is_an_error(bad):
    entry = index_entry(tiny(), "toy-idea.json")
    entry["id"] = bad
    errs = validate_index({"version": 0, "generated": "2025-02-01T00:00:00Z", "traces": [entry, dict(entry)]})
    assert any(e.startswith("traces[0].id: expected string") for e in errs), errs


def test_start_after_end():
    tr = tiny()
    tr["start"], tr["end"] = tr["end"], tr["start"]
    assert "start: after end" in validate(tr)


@pytest.mark.parametrize("path,value", [
    (("kind",), "rumour"), (("events", 0, "channel"), "email"), (("events", 0, "stance"), "likes"),
    (("events", 0, "conf"), 4), (("events", 0, "conf"), True), (("exposures", 0, "via"), "telepathy"),
    (("edges", 0, "kind"), "gossip"), (("annotations", 0, "kind"), "party"), (("persistence", 0, "where"), "disk"),
])
def test_bad_enums(path, value):
    tr = tiny()
    obj = tr
    for k in path[:-1]:
        obj = obj[k]
    obj[path[-1]] = value
    prefix = ".".join(str(p) for p in path).replace(".0.", "[0].")
    errs = validate(tr)
    assert any(e.startswith(prefix + ":") for e in errs), errs


def test_structure_and_limits():
    tr = tiny()
    tr["events"][1]["id"] = "e1"
    tr["events"][0]["snippet"] = "x" * 221
    tr["agents"].append(dict(tr["agents"][0]))
    tr["metrics"] = {str(i): i for i in range(11)}
    del tr["quotes"][0]["note"]
    tr["extra"] = 1
    errs = validate(tr)
    for want in ("events[1].id: duplicate event id 'e1'", "events[0].snippet: 221 chars > 220",
                 "agents[2].name: duplicate agent 'alpha'", "metrics: 11 entries > 10", "quotes[0].note: missing",
                 "extra: unknown field"):
        assert want in errs, (want, errs)
    assert any("bytes serialized" in e for e in validate(tiny(), max_bytes=100))


def test_window_warning_and_fit():
    tr = tiny()
    tr["events"][1]["t"] = "2025-02-01T00:00:00Z"
    tr["adoptions"][0]["t"] = "2025-02-01T00:00:00Z"
    errs, warns = check(tr)
    assert errs == [] and len(warns) == 1 and "outside the window" in warns[0]
    fit_window(tr)
    assert tr["end"] == "2025-02-01T00:00:00Z" and check(tr) == ([], [])


def test_helpers():
    assert iso(parse_iso("2025-01-02T03:04:05Z")) == "2025-01-02T03:04:05Z"
    assert iso("2025-01-02 03:04:05.123456") == "2025-01-02T03:04:05Z"
    text = "lorem " * 100 + "KEY PHRASE" + " ipsum" * 100
    i = text.index("KEY")
    c = clip(text, i, i + 10, limit=80)
    assert "KEY PHRASE" in c and len(c) <= 80 and c.startswith("…") and c.endswith("…")
    assert clip("  short\n\n text  ") == "short text"


def _cut_through(pii, limit=60):
    """A long text whose clip window (around KEY) ends inside `pii`."""
    head = "KEY " + "word " * 9
    room = limit - 2
    pad = room - len(head) - 5                          # the window ends 5 chars into the PII
    return head + "x" * max(0, pad - 1) + " " + pii + " tail" * 40


@pytest.mark.parametrize("pii", ["jane.doe@examplecorp.com", "555-867-5309", "+1 415 555 0134", "+44 20 7946 0958"])
def test_clip_never_leaves_partial_pii(pii):
    """Regression: clip cut first and scrub ran on the snippet, so 'jane.doe@examplecor…' or '555-86…' survived
    scrubbing with pii_hits() == []."""
    text = _cut_through(pii)
    c = clip(text, 0, 3, limit=60)
    assert c.startswith("KEY") and c.endswith("…") and len(c) <= 60
    assert pii[:5] not in c and pii[-4:] not in c and not any(ch.isdigit() for ch in c)
    assert pii not in scrub(c) and pii_hits(c) == []
    # the whole PII inside the window is scrubbed, with the key phrase still in view
    whole = "lorem " * 30 + f"KEY then {pii} then more" + " ipsum" * 30
    i = whole.index("KEY")
    c = clip(whole, i, i + 3, limit=80)
    assert "KEY" in c and pii not in c and ("[email]" in c or "[phone]" in c)


def test_clip_drops_partial_pii_at_edges_of_stored_excerpts():
    stored = "ne.doe@examplecorp.com said KEY is broken, call 555-867-53"   # already cut on both sides
    c = clip(stored, stored.index("KEY"), stored.index("KEY") + 3, cut_before=True, cut_after=True)
    assert c == "said KEY is broken, call"
    assert clip("bot@agentvillage.org and x@gmail.com", allow_domains=("agentvillage.org",)) == \
        "bot@agentvillage.org and [email]"


def test_scrub_tracking_moves_marks():
    text = "mail jane@gmail.com about KEY now"
    out, (a, b) = scrub_tracking(text, (text.index("KEY"), text.index("KEY") + 3))
    assert out == "mail [email] about KEY now" and out[a:b] == "KEY"
    out, (a, b) = scrub_tracking(text, (text.index("jane") + 2, text.index("about")))
    assert out[a:b] == "[email] "


@pytest.mark.parametrize("raw,want", [
    ("連絡はbob@example.comまで", "連絡は[email]まで"), ("jöhn@example.com", "[email]"),
    ("émail bob@example.comé", "émail [email]é"), ("電話+81 90 1234 5678です", "電話[phone]です"),
    ("電話555-867-5309です", "電話[phone]です"),
])
def test_scrub_next_to_non_ascii_letters(raw, want):
    assert scrub(raw) == want and pii_hits(want) == []


@pytest.mark.parametrize("raw,allow,want", [
    ("mail jane.doe+x@gmail.com now", (), "mail [email] now"),
    ("bot is claude-3.7@agentvillage.org", ("agentvillage.org",), "bot is claude-3.7@agentvillage.org"),
    ("bot is a@mail.agentvillage.org", ("agentvillage.org",), "bot is a@mail.agentvillage.org"),
    ("bot is claude-3.7@agentvillage.org", (), "bot is [email]"),
    ("call +1 (415) 555-0134 or 415-555-0134 or +44 20 7946 0958", (), "call [phone] or [phone] or [phone]"),
    ("ping @Claude Opus 5 at 2026-07-24 18:53:59, v1.27.0, 75.126.1.1, #4,688,813,549, sha 7d5d7e8", (),
     "ping @Claude Opus 5 at 2026-07-24 18:53:59, v1.27.0, 75.126.1.1, #4,688,813,549, sha 7d5d7e8"),
])
def test_scrub(raw, allow, want):
    assert scrub(raw, allow) == want


# Same cases as swarm_mcp/tests/test_redact.py (the email patterns are twins).
@pytest.mark.parametrize("raw,want", [
    ("bob_o'neil@example.com", "[email]"), ("'bob@example.com'", "'[email]'"), ("it's bob@example.com", "it's [email]"),
    ("иван@пример.рф", "[email]"), ("bob@münchen.de", "[email]"), ("user@xn--e1afmkfd.xn--p1ai", "[email]"),
    ("联系bob@example.com谢谢", "联系[email]谢谢"), ("bob@пример.рф, ok", "[email], ok"),
    ("bob@example.com-ish", "[email]-ish"), ("bob@example.com-cdn.net", "[email]"),
])
def test_scrub_email_takes_the_whole_address(raw, want):
    assert scrub(raw) == want and pii_hits(want) == []


@pytest.mark.parametrize("raw", ["@handle", "user@localhost", "a@b", "a@b.c", "foo@bar", "ping me @bob.", "v1.2@3.4"])
def test_scrub_email_leaves_non_addresses(raw):
    assert scrub(raw) == raw and pii_hits(raw) == []


def test_scrub_email_allowlist_still_keeps_whole_domains():
    allow = ("agentvillage.org",)
    assert scrub("bot@agentvillage.org, o'neil@mail.agentvillage.org", allow) == \
        "bot@agentvillage.org, o'neil@mail.agentvillage.org"
    assert scrub("x@notagentvillage.org x@agentvillage.org.evil.com", allow) == "[email] [email]"


# Same cases as swarm_mcp/tests/test_redact.py (the phone patterns are twins).
@pytest.mark.parametrize("raw,want", [
    # a letter, "_" or "-word" right after the number: the whole number goes, no digits left behind
    ("+44 20 7946 0958x", "[phone]x"), ("+44 20 7946 0958café", "[phone]café"), ("+44 20 7946 0958_", "[phone]_"),
    ("+44 20 7946 0958-ish", "[phone]-ish"), ("415-555-0134x", "[phone]x"), ("1-415-555-0134-ish", "[phone]-ish"),
    ("+33 6 12 34 56 78x", "[phone]x"),
    # formats that used to slip through
    ("1-415-555-0134", "[phone]"), ("1.415.555.0134", "[phone]"), ("+33 6 12 34 56 78", "[phone]"),
    ("+81-3-1234-5678", "[phone]"), ("+4915112345678", "[phone]"), ("+44 (0)20 7946 0958", "[phone]"),
    ("1 (415) 555-0134", "[phone]"), ("tel:+14155550134", "tel:[phone]"), ("phone=415-555-0134", "phone=[phone]"),
    ("+33 6 12 34 56 78", "[phone]"), ("415 555 0134", "[phone]"),
])
def test_scrub_phone_takes_the_whole_number(raw, want):
    assert scrub(f"call {raw} now") == f"call {want} now"
    assert pii_hits(raw) == ["phone"] and pii_hits(scrub(raw)) == []


@pytest.mark.parametrize("raw", [
    "2026-10-04", "10/04/2026", "2026.10.04", "12:34:56", "12.30", "1.2.3", "v3.10.12", "2.0.0-rc1",
    "550e8400-e29b-41d4-a716-446655440000", "deadbeefcafe1234", "4155550134", "14155550134", "1,234,567.89",
    "75.126.1.1", "192.168.1.10", "10.0.19041.1", "ISBN 978-3-16-148410-0", "score 1+2345678901", "+3.14159265",
    "+100.000000", "2026-10-04T12:34:56+05:30", "123-456-78901", "1234-567-8901", "ev_123-456-7890", "#123-456-7890",
    "415-555-0134.5", "123-456-7890ab1", "4111 1111 1111 1111", "+1 2345 6789 0123 4567", "415\n555\n0134",
])
def test_scrub_phone_leaves_non_phones(raw):
    assert scrub(raw) == raw and pii_hits(raw) == []


def test_pii_warning_and_scrub_trace():
    tr = tiny()
    tr["events"][0]["snippet"] = "ask jane.doe@gmail.com or bot@agentvillage.org"
    tr["quotes"][0]["text"] = "ring 415-555-0134"
    errs, warns = check(tr, allow_domains=("agentvillage.org",))
    assert errs == [] and len(warns) == 1
    assert "events[0].snippet (email)" in warns[0] and "quotes[0].text (phone)" in warns[0]
    assert "jane.doe" not in warns[0] and "555" not in warns[0]          # warnings never echo the PII
    scrub_trace(tr, ("agentvillage.org",))
    assert tr["events"][0]["snippet"] == "ask [email] or bot@agentvillage.org"
    assert tr["quotes"][0]["text"] == "ring [phone]"
    assert check(tr, allow_domains=("agentvillage.org",)) == ([], [])
    tr["events"][0]["snippet"] = "x" * 214 + " a@b.co"                    # 221 chars after [email] -> re-trimmed
    scrub_trace(tr)
    assert len(tr["events"][0]["snippet"]) <= 220 and validate(tr) == []


def test_schema_agrees_on_shapes():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text())
    v = jsonschema.Draft202012Validator(schema)
    assert list(v.iter_errors(tiny())) == []
    bad = tiny()
    bad["edges"][0]["kind"] = "gossip"
    bad["events"][0]["t"] = "2025-01-02 10:00"
    assert len(list(v.iter_errors(bad))) == 2


def test_cli_validate(tmp_path, capsys):
    good, bad = tmp_path / "toy-idea.json", tmp_path / "broken.json"
    good.write_text(dumps(tiny()))
    broken = copy.deepcopy(tiny())
    broken["events"][0]["agent"] = "gamma"
    bad.write_text(dumps(broken))
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"version": 0, "generated": iso("2025-02-01T00:00:00"),
                                 "traces": [index_entry(tiny(), "toy-idea.json"), index_entry(tiny(), "missing.json")]}))
    assert cli.main(["validate", str(good)]) == 0
    assert cli.main(["validate", str(good), str(bad)]) == 1
    assert cli.main(["validate", str(index)]) == 1
    out = capsys.readouterr().out
    assert "not in agents" in out and "missing.json does not exist" in out


def test_build_index_skips_corrupt_json(tmp_path, capsys):
    (tmp_path / "toy-idea.json").write_text(dumps(tiny()))
    (tmp_path / "corrupt.json").write_text('{"version": 0, "id": ')          # truncated write
    (tmp_path / "binary.json").write_bytes(b"\xff\xfe\x00garbage")           # not UTF-8
    index = cli.build_index(tmp_path)
    assert [e["file"] for e in index["traces"]] == ["toy-idea.json"]
    assert validate_index(json.loads((tmp_path / "index.json").read_text())) == []
    out = capsys.readouterr().out
    assert "warning: index: skipping corrupt.json" in out and "warning: index: skipping binary.json" in out
