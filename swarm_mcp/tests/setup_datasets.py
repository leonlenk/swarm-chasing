"""Synthetic datasets for the setup tests, generated in tmp_path (never committed).

(a) ``make_nested_jsonl``: gzipped JSONL with nested fields plus an agents file
    participants.jsonl.gz  {pid, profile.display_label, nicknames[], lab}
    utterances.jsonl.gz    {uttId, meta.stampedAt (ISO), meta.chan, speaker.ref, speaker.kind,
                            payload.body, inReplyTo, to[]}
(b) ``make_csv_chat``: one CSV, no agents file
    chatlog_export.csv     MsgNo, sent_epoch_ms (epoch ms), from_nick, ChannelName, said, parent_no, mentions ("a;b")
(c) ``make_sqlite_board``: a forum database
    board.sqlite           members(member_key, screen_name, is_bot), threads(thread_key, title, opened_by, opened_ts),
                           posts(post_key, thread_ref, author_ref, posted_unix (epoch s), body_md, reply_to_post)
"""

from __future__ import annotations

import csv
import gzip
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORDS = ("plan", "deploy", "review", "the", "build", "fix", "test", "ship", "docs", "merge", "idea", "agree", "later")
T0 = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)


def _sentence(rng: random.Random, n: int = 8) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."


def _jsonl_gz(path: Path, rows: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


AGENTS_A = [
    ("p-nova", "Nova", ["nov"]),
    ("p-orion", "Orion", ["ori"]),
    ("p-lyra", "Lyra", []),
    ("p-vega", "Vega", ["veg"]),
]


def make_nested_jsonl(root: Path, n: int = 300, humans: int = 6) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(1)
    _jsonl_gz(
        root / "participants.jsonl.gz",
        [
            {"pid": pid, "profile": {"display_label": name, "lab": "lab-" + name[0]}, "nicknames": nicks}
            for pid, name, nicks in AGENTS_A
        ],
    )
    rows = []
    for i in range(n):
        human = i % (n // humans) == 7 if humans else False
        pid, name, _ = rng.choice(AGENTS_A)
        others = [a[0] for a in AGENTS_A if a[0] != pid]
        rows.append(
            {
                "uttId": f"u-{i:05d}",
                "meta": {
                    "stampedAt": (T0 + timedelta(minutes=3 * i)).isoformat().replace("+00:00", "Z"),
                    "chan": rng.choice(["lobby", "lab", "ops"]),
                },
                "speaker": {"ref": "visitor-1" if human else pid, "kind": "human" if human else "agent"},
                "payload": {"body": _sentence(rng) + f" @{rng.choice(AGENTS_A)[1]}"},
                "inReplyTo": f"u-{i - 1:05d}" if i and i % 3 == 0 else None,
                "to": rng.sample(others, k=rng.randint(0, 2)),
            }
        )
    _jsonl_gz(root / "utterances.jsonl.gz", rows)
    (root / "README.md").write_text("# Synthetic crew chat\n\nutterances + participants.\n")
    return root


NICKS_B = ["ada", "bob", "cyd", "dee", "eli"]


def make_csv_chat(root: Path, n: int = 250) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(2)
    base_ms = int(T0.timestamp() * 1000)
    with open(root / "chatlog_export.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["MsgNo", "sent_epoch_ms", "from_nick", "ChannelName", "said", "parent_no", "mentions"])
        for i in range(1, n + 1):
            who = rng.choice(NICKS_B)
            ment = ";".join(rng.sample([x for x in NICKS_B if x != who], k=rng.randint(0, 2)))
            w.writerow(
                [
                    i,
                    base_ms + i * 45_000,
                    who,
                    rng.choice(["#general", "#random", "#ops"]),
                    _sentence(rng, 10),
                    i - 1 if i > 1 and i % 4 == 0 else "",
                    ment,
                ]
            )
    return root


MEMBERS_C = [("m-ada", "ada_lovelace", 1), ("m-bab", "babbage", 1), ("m-hop", "grace_h", 1), ("m-tur", "turing", 0)]


def make_sqlite_board(root: Path, threads: int = 12, posts: int = 240) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(3)
    con = sqlite3.connect(root / "board.sqlite")
    con.executescript(
        """
        CREATE TABLE members (member_key TEXT PRIMARY KEY, screen_name TEXT, is_bot INTEGER);
        CREATE TABLE threads (thread_key INTEGER PRIMARY KEY, title TEXT, opened_by TEXT REFERENCES members(member_key),
                              opened_ts INTEGER);
        CREATE TABLE posts (post_key INTEGER PRIMARY KEY, thread_ref INTEGER REFERENCES threads(thread_key),
                            author_ref TEXT REFERENCES members(member_key), posted_unix INTEGER, body_md TEXT,
                            reply_to_post INTEGER);
        """
    )
    con.executemany("INSERT INTO members VALUES (?, ?, ?)", MEMBERS_C)
    t0 = int(T0.timestamp())
    for t in range(1, threads + 1):
        con.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?)",
            (t, f"Thread about {rng.choice(WORDS)} number {t}", rng.choice(MEMBERS_C)[0], t0 + t * 3600),
        )
    for p in range(1, posts + 1):
        con.execute(
            "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
            (
                1000 + p,
                rng.randint(1, threads),
                rng.choice(MEMBERS_C)[0],
                t0 + p * 600,
                _sentence(rng, 12),
                1000 + p - 1 if p > 1 and p % 2 == 0 else None,
            ),
        )
    con.commit()
    con.close()
    return root
