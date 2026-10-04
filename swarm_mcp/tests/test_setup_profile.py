"""Profiler: formats, sampling, masking, role guesses and foreign keys on synthetic datasets."""

from __future__ import annotations

import gzip
import json
import random

from setup_datasets import make_csv_chat, make_nested_jsonl, make_sqlite_board

from swarm_mcp.setup import readers
from swarm_mcp.setup.masking import show
from swarm_mcp.setup.profile import profile_path, summarize, tokens


def _table(profile, key):
    return next(t for t in profile["tables"] if t["table"] == key)


def _top(t, role):
    return t["roles"][role][0]


def test_tokens_split_snake_camel_dotted():
    assert tokens("agent_speaker_id") == ["agent", "speaker", "id"]
    assert tokens("meta.stampedAt") == ["meta", "stamped", "at"]
    assert tokens("ChannelName") == ["channel", "name"]


def test_nested_jsonl_roles_rank_right_fields_first(tmp_path):
    p = profile_path(make_nested_jsonl(tmp_path / "a"))
    t = _table(p, "utterances.jsonl.gz")
    assert t["looks_like"] == "records" and t["rows_total"] == 300 and t["rows_total_exact"]
    want = {
        "id": "uttId",
        "time": "meta.stampedAt",
        "actor": "speaker.ref",
        "actor_type": "speaker.kind",
        "location": "meta.chan",
        "text": "payload.body",
        "reply_to": "inReplyTo",
        "recipients": "to",
    }
    assert {r: _top(t, r)["field"] for r in want} == want
    assert _top(t, "time")["format"] == "iso"
    assert _top(t, "actor")["join"] == {"table": "participants.jsonl.gz", "field": "pid"}
    assert _top(t, "reply_to")["target"]["table"] == "utterances.jsonl.gz"
    ag = p["agents_table"]
    assert (ag["table"], ag["id"], ag["display_name"], ag["aliases"]) == (
        "participants.jsonl.gz",
        "pid",
        "profile.display_label",
        ["nicknames"],
    )
    assert p["docs"][0]["path"] == "README.md"
    fields = {f["path"]: f for f in t["fields"]}
    assert fields["inReplyTo"]["null_rate"] > 0.5 and fields["uttId"]["unique_ratio"] == 1.0
    assert len(fields["payload.body"]["examples"]) == 3


def test_csv_epoch_ms_and_channel(tmp_path):
    p = profile_path(make_csv_chat(tmp_path / "b"))
    t = _table(p, "chatlog_export.csv")
    assert _top(t, "time")["field"] == "sent_epoch_ms" and _top(t, "time")["format"] == "epoch_ms"
    assert _top(t, "id")["field"] == "MsgNo"
    assert _top(t, "actor")["field"] == "from_nick"
    assert _top(t, "location")["field"] == "ChannelName"
    assert _top(t, "text")["field"] == "said"
    assert _top(t, "reply_to")["field"] == "parent_no"
    assert _top(t, "recipients")["field"] == "mentions"
    assert p["agents_table"] is None


def test_sqlite_tables_and_foreign_keys(tmp_path):
    p = profile_path(make_sqlite_board(tmp_path / "c"))
    keys = {t["table"] for t in p["tables"]}
    assert keys == {"board.sqlite#members", "board.sqlite#threads", "board.sqlite#posts"}
    posts = _table(p, "board.sqlite#posts")
    assert posts["rows_total"] == 240 and posts["declared_foreign_keys"]
    want = {
        "id": "post_key",
        "time": "posted_unix",
        "actor": "author_ref",
        "location": "thread_ref",
        "text": "body_md",
        "reply_to": "reply_to_post",
    }
    assert {r: _top(posts, r)["field"] for r in want} == want
    assert _top(posts, "time")["format"] == "epoch_s"
    fks = {(fk["from"], fk["to"]) for fk in p["foreign_keys"]}
    assert ("board.sqlite#posts::author_ref", "board.sqlite#members::member_key") in fks
    assert ("board.sqlite#posts::thread_ref", "board.sqlite#threads::thread_key") in fks
    assert p["agents_table"]["table"] == "board.sqlite#members"
    assert p["agents_table"]["display_name"] == "screen_name"


def test_examples_are_masked_and_truncated(tmp_path):
    rows = [{"id": i, "note": f"mail me at person{i}@example.com or +1 415 555 0101 " + "x" * 200} for i in range(5)]
    (tmp_path / "notes.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    p = profile_path(tmp_path)
    ex = next(f for f in p["tables"][0]["fields"] if f["path"] == "note")["examples"]
    assert ex and all(len(e) <= 80 for e in ex)
    assert all("@example.com" not in e and "[email]" in e and "[phone]" in e for e in ex)
    assert show("a" * 500).endswith("…") and len(show("a" * 500)) == 80


def test_big_file_is_sampled_not_loaded(tmp_path):
    path = tmp_path / "huge.jsonl.gz"
    rng = random.Random(0)
    with gzip.open(path, "wt") as f:
        for i in range(20_000):
            body = " ".join(
                rng.choice(["alpha", "beta", "gamma", "delta", "eps"]) + str(rng.randint(0, 999)) for _ in range(12)
            )
            f.write(json.dumps({"id": i, "ts": 1767621600 + i, "body": body}) + "\n")
    p = profile_path(tmp_path, byte_budget=64 * 1024)
    t = p["tables"][0]
    assert t["rows_sampled"] < 2000 and not t["rows_total_exact"]
    assert 10_000 < t["rows_total"] < 40_000  # estimate from compressed position


def test_formats_json_array_object_tsv_and_bad_lines(tmp_path):
    (tmp_path / "arr.json").write_text(json.dumps([{"k": i, "v": f"x{i}"} for i in range(30)]))
    (tmp_path / "obj.json").write_text(
        json.dumps({"meta": {"n": 2}, "data": {"items": [{"a": 1}, {"a": 2}]}, "users": [{"id": "u1"}]})
    )
    (tmp_path / "t.tsv").write_text("a\tb\n1\thello there\n2\t\n")
    (tmp_path / "broken.jsonl").write_text('{"a": 1}\nnot json\n{"a": 2}\n')
    (tmp_path / "x.bin").write_bytes(b"\x00\x01")
    tables, docs, skipped = readers.discover(tmp_path)
    keys = {t.key: t for t in tables}
    assert {"arr.json", "obj.json#data.items", "obj.json#users", "t.tsv", "broken.jsonl"} <= set(keys)
    assert len(list(keys["arr.json"].rows())) == 30
    assert list(keys["t.tsv"].rows()) == [{"a": "1", "b": "hello there"}, {"a": "2", "b": None}]
    b = keys["broken.jsonl"]
    assert len(list(b.rows())) == 2 and b.bad_rows == 1
    assert any(s["path"] == "x.bin" for s in skipped)




def test_summary_lists_tables_and_fields(tmp_path):
    root = make_csv_chat(tmp_path / "b")
    text = summarize(profile_path(root))
    assert "chatlog_export.csv" in text and "sent_epoch_ms" in text
