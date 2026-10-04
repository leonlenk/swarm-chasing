"""Profile an unknown multi-agent dataset: tables, fields, and guesses at what each field means.

``profile_path(path)`` walks a file or folder (``readers.discover``), samples the
first ``rows`` rows of every table (stopping early after ``byte_budget``
decompressed bytes, so a multi-GB file costs about as much as a small one), and
reports for each table:

- row count (exact when the whole file was read or the format stores it, else an estimate);
- every field as a dotted path (``speaker.id``; arrays of objects as ``mentions[].id``) with its
  types, null and empty rates, distinct values in the sample, and 3 masked, truncated examples;
- role guesses with scores in [0, 1] for ``id``, ``time`` (with the detected format), ``actor``,
  ``actor_type``, ``location``, ``text``, ``reply_to`` and ``recipients``.

Across tables it finds foreign keys by value overlap (``chat.agent_speaker_id -> agents.id``)
and picks the most likely agents table. Scores are heuristics: name tokens plus value
evidence (uniqueness, parse rates, lengths, overlaps); each guess lists its reasons.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from swarm_mcp.fence import safe_name
from swarm_mcp.setup import readers
from swarm_mcp.setup.masking import show
from swarm_mcp.setup.timeparse import detect_format

PROFILE_VERSION = 1
DEFAULT_ROWS = 1000
DEFAULT_BYTE_BUDGET = 8 * 1024 * 1024
DISTINCT_CAP = 5000
ROLES = ("id", "time", "actor", "actor_type", "location", "text", "reply_to", "recipients")

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def tokens(name: str) -> list[str]:
    """``agent_speaker_id`` -> [agent, speaker, id]; ``createdAt`` -> [created, at]."""
    out: list[str] = []
    for part in _TOKEN_SPLIT.split(name):
        out.extend(p.lower() for p in _CAMEL.sub(" ", part).split() if p)
    return out


def singular(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "ches", "shes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def table_stem(key: str) -> str:
    """``data/chat_messages.jsonl.gz`` -> chat_messages; ``forum.db#posts`` -> posts."""
    if "#" in key:
        return key.rsplit("#", 1)[1].split(".")[-1]
    name = key.rsplit("/", 1)[-1]
    for _ in range(3):
        stem, dot, ext = name.rpartition(".")
        if dot and ext.lower() in {
            "gz",
            "jsonl",
            "ndjson",
            "json",
            "csv",
            "tsv",
            "parquet",
            "pq",
            "db",
            "sqlite",
            "sqlite3",
        }:
            name = stem
    return name


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not s or not s[0].isalpha():
        s = "t_" + s
    return s


# --------------------------------------------------------------------------- per-field stats


def _flatten(obj: dict[str, Any], prefix: str, out: dict[str, Any], depth: int = 0) -> None:
    for k, v in obj.items():
        p = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v and depth < 4:
            _flatten(v, p, out, depth + 1)
        elif isinstance(v, list) and v and isinstance(v[0], dict) and depth < 4:
            out[p] = v
            sub: dict[str, list[Any]] = {}
            for item in v[:50]:
                if isinstance(item, dict):
                    flat: dict[str, Any] = {}
                    _flatten(item, "", flat, depth + 1)
                    for kk, vv in flat.items():
                        sub.setdefault(kk, []).append(vv)
            for kk, vals in sub.items():
                out[f"{p}[].{kk}"] = vals
        else:
            out[p] = v


def flatten(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    _flatten(row, "", out)
    return out


def _type(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, (datetime, date)):
        return "datetime"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+", s):
            return "str(int)"
        if re.fullmatch(r"-?\d+\.\d+", s):
            return "str(float)"
        return "str"
    return type(v).__name__


def _key(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (dict, list)):
        return repr(v)[:200]
    return str(v)[:200]


@dataclass
class FieldStats:
    path: str
    rows: int = 0  # rows where the key exists (incl. null)
    nulls: int = 0
    empties: int = 0
    types: Counter = field(default_factory=Counter)
    distinct: set = field(default_factory=set)
    distinct_capped: bool = False
    elements: set = field(default_factory=set)  # array elements (scalars), for recipient/FK overlap
    examples: list = field(default_factory=list)
    str_len: int = 0
    str_n: int = 0
    spaces: int = 0
    time_fmt: Counter = field(default_factory=Counter)
    array_len: int = 0
    array_n: int = 0

    def add(self, v: Any) -> None:
        self.rows += 1
        if v is None:
            self.nulls += 1
            self.types["null"] += 1
            return
        t = _type(v)
        self.types[t] += 1
        if v == "" or v == [] or v == {}:
            self.empties += 1
            return
        if isinstance(v, list):
            self.array_n += 1
            self.array_len += len(v)
            for x in v[:50]:
                if x is not None and not isinstance(x, (dict, list)) and len(self.elements) < DISTINCT_CAP:
                    self.elements.add(_key(x))
        if isinstance(v, str):
            self.str_n += 1
            self.str_len += len(v)
            if " " in v.strip():
                self.spaces += 1
        fmt = detect_format(v) if not isinstance(v, (list, dict)) else None
        if fmt:
            self.time_fmt[fmt] += 1
        k = _key(v)
        if len(self.distinct) < DISTINCT_CAP:
            if k not in self.distinct and len(self.examples) < 3:
                self.examples.append(show(v))
            self.distinct.add(k)
        else:
            self.distinct_capped = True

    # derived
    @property
    def values(self) -> int:
        return self.rows - self.nulls - self.empties

    def present_rate(self, n_rows: int) -> float:
        return self.values / n_rows if n_rows else 0.0

    @property
    def unique_ratio(self) -> float:
        return len(self.distinct) / self.values if self.values else 0.0

    @property
    def avg_len(self) -> float:
        return self.str_len / self.str_n if self.str_n else 0.0

    @property
    def space_rate(self) -> float:
        return self.spaces / self.str_n if self.str_n else 0.0

    @property
    def time_rate(self) -> float:
        return sum(self.time_fmt.values()) / self.values if self.values else 0.0

    @property
    def time_format(self) -> str | None:
        if not self.time_fmt:
            return None
        fmt = self.time_fmt.most_common(1)[0][0]
        return "auto" if fmt == "native" else fmt

    @property
    def is_array(self) -> bool:
        return self.array_n > 0 and self.array_n >= self.values * 0.8

    @property
    def main_type(self) -> str:
        c = Counter({k: v for k, v in self.types.items() if k != "null"})
        return c.most_common(1)[0][0] if c else "null"

    def text_like(self) -> bool:
        return self.avg_len >= 25 and self.space_rate >= 0.4

    def as_dict(self, n_rows: int) -> dict[str, Any]:
        d: dict[str, Any] = {
            "path": self.path,
            "types": dict(self.types.most_common()),
            "null_rate": round(1 - (self.rows - self.nulls) / n_rows, 3) if n_rows else 1.0,
            "empty_rate": round(self.empties / n_rows, 3) if n_rows else 0.0,
            "distinct_in_sample": len(self.distinct),
            "distinct_capped": self.distinct_capped,
            "unique_ratio": round(self.unique_ratio, 3),
            "examples": self.examples,
        }
        if self.str_n:
            d["avg_len"] = round(self.avg_len, 1)
        if self.time_fmt:
            d["time_format"] = self.time_format
            d["time_parse_rate"] = round(self.time_rate, 3)
        if self.array_n:
            d["avg_array_len"] = round(self.array_len / self.array_n, 2)
        return d


# --------------------------------------------------------------------------- role scoring

ID_EXACT = {"id", "uuid", "guid", "pk", "key", "oid", "uid"}
TIME_WORDS = {
    "time",
    "ts",
    "timestamp",
    "date",
    "datetime",
    "created",
    "at",
    "sent",
    "posted",
    "when",
    "epoch",
    "utc",
    "dt",
}
TIME_GOOD = {"created", "sent", "posted", "timestamp", "ts", "time", "start", "started"}
TIME_BAD = {"updated", "modified", "edited", "deleted", "end", "ended", "last", "expires", "birth", "joined", "seen"}
ACTOR_WORDS = {
    "speaker",
    "author",
    "sender",
    "user",
    "from",
    "agent",
    "actor",
    "by",
    "poster",
    "who",
    "username",
    "handle",
    "nick",
    "persona",
    "participant",
    "member",
    "bot",
    "creator",
    "owner",
    "writer",
    "player",
}
ACTOR_STRONG = {"speaker", "author", "sender", "actor", "poster", "from", "by"}
ACTOR_NEG = {
    "to",
    "recipient",
    "recipients",
    "mention",
    "mentions",
    "reply",
    "parent",
    "room",
    "channel",
    "thread",
    "target",
    "type",
    "role",
    "kind",
    "model",
    "count",
    "lab",
}
ATYPE_WORDS = {"type", "role", "kind", "is", "bot", "human", "class", "category"}
ATYPE_VALUES = {
    "agent",
    "human",
    "bot",
    "user",
    "assistant",
    "system",
    "ai",
    "model",
    "person",
    "llm",
    "operator",
    "admin",
}
LOC_WORDS = {
    "channel",
    "room",
    "chan",
    "thread",
    "topic",
    "location",
    "place",
    "forum",
    "group",
    "space",
    "server",
    "board",
    "subreddit",
    "conversation",
    "chat",
    "venue",
    "guild",
    "conv",
    "village",
    "world",
    "map",
    "zone",
}
TEXT_WORDS = {
    "text",
    "content",
    "body",
    "message",
    "msg",
    "utterance",
    "post",
    "comment",
    "summary",
    "description",
    "caption",
    "note",
    "contents",
    "words",
    "said",
    "speech",
    "value",
}
REPLY_WORDS = {
    "reply",
    "replies",
    "parent",
    "respond",
    "responding",
    "replying",
    "quote",
    "quoted",
    "re",
    "ref",
    "in",
    "answer",
    "answers",
}
RECIP_WORDS = {
    "to",
    "recipients",
    "recipient",
    "mentions",
    "mention",
    "addressee",
    "addressees",
    "targets",
    "target",
    "cc",
    "audience",
    "tagged",
    "receivers",
    "receiver",
}
AGENT_TABLE_WORDS = {
    "agent",
    "user",
    "member",
    "participant",
    "persona",
    "bot",
    "speaker",
    "author",
    "people",
    "person",
    "account",
    "player",
    "profile",
    "actor",
    "character",
}
NAME_WORDS = {"name", "display", "displayname", "username", "handle", "nick", "nickname", "title", "label", "login"}
ALIAS_WORDS = {"alias", "aliases", "nicknames", "aka", "other", "names", "alt"}


@dataclass
class Guess:
    field: str
    score: float
    why: list[str]
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "score": round(max(0.0, min(1.0, self.score)), 2), "why": self.why, **self.extra}


def _score_field(role: str, f: FieldStats, n: int, stem_tokens: set[str]) -> Guess:
    toks = tokens(f.path)
    tset = set(toks)
    last = toks[-1] if toks else ""
    depth = f.path.count(".")
    s, why = 0.0, []
    present = f.present_rate(n)
    is_time = f.time_rate >= 0.8 and f.values > 0
    text = f.text_like()

    def add(v: float, reason: str) -> None:
        nonlocal s
        s += v
        why.append(f"{'+' if v >= 0 else ''}{v:.2f} {reason}")

    if role == "id":
        if len(toks) == 1 and last in ID_EXACT:
            add(0.45, "named id")
        elif last in ("id", "uuid", "guid", "key") and stem_tokens & {singular(t) for t in toks[:-1]}:
            add(0.4, "named <table>_id")
        elif last in ("id", "uuid", "guid"):
            add(0.15, "ends in id")
        if f.unique_ratio >= 0.99 and present >= 0.99:
            add(0.4, "unique and always present")
        elif f.unique_ratio >= 0.9 and present >= 0.9:
            add(0.2, "mostly unique")
        if is_time:
            add(-0.5, "looks like a timestamp")
        if text or f.avg_len > 80:
            add(-0.4, "looks like free text")
        if depth:
            add(-0.1, "nested")
        if f.is_array or f.main_type == "object":
            add(-0.6, "not a scalar")
    elif role == "time":
        if f.values:
            r = f.time_rate
            add(0.55 * r if r >= 0.8 else 0.25 * r, f"{r:.0%} parse as {f.time_format or 'time'}")
        if tset & TIME_WORDS:
            add(0.3, "time-like name")
            if tset & TIME_GOOD:
                add(0.1, "creation/send time name")
            if tset & TIME_BAD:
                add(-0.15, "update/end/other time name")
        if f.values and f.time_rate < 0.5:
            add(-0.3, "values mostly not timestamps")
        s *= 0.5 + 0.5 * present
    elif role == "actor":
        if tset & ACTOR_WORDS:
            add(0.35, "actor-like name")
            if tset & ACTOR_STRONG:
                add(0.1, "speaker/author/sender name")
        if tset & ACTOR_NEG:
            add(-0.3, "recipient/room/type-like name")
        if f.values and 2 <= len(f.distinct) and f.unique_ratio < 0.5 and not text:
            add(0.2, f"repeating values ({len(f.distinct)} distinct)")
        if text:
            add(-0.4, "free text")
        if is_time:
            add(-0.5, "timestamp")
        if f.is_array or f.main_type in ("object", "bool", "float"):
            add(-0.4, "not a scalar id/name")
    elif role == "actor_type":
        if tset & ATYPE_WORDS and (tset & ACTOR_WORDS or tset & {"type", "role"}):
            add(0.35, "actor type/role name")
        vals = {v.lower() for v in f.distinct if isinstance(v, str)}
        if f.values and len(f.distinct) <= 8:
            add(0.15, f"few values ({len(f.distinct)})")
            if vals & ATYPE_VALUES:
                add(0.35, "values like agent/human/bot")
        if f.main_type == "bool" and tset & {"bot", "human", "agent", "ai"}:
            add(0.3, "is_bot-style flag")
        if len(f.distinct) > 20:
            add(-0.4, "too many values")
    elif role == "location":
        if tset & LOC_WORDS:
            add(0.4, "room/channel-like name")
        if f.values and f.unique_ratio < 0.5 and not text:
            add(0.15, f"repeating values ({len(f.distinct)} distinct)")
        if tset & {"parent", "reply"}:
            add(-0.3, "reply-like name")
        if text:
            add(-0.3, "free text")
        if is_time:
            add(-0.5, "timestamp")
        if f.is_array or f.main_type == "object":
            add(-0.4, "not a scalar")
    elif role == "text":
        if f.str_n:
            if f.avg_len >= 20:
                add(0.25, f"long strings (avg {f.avg_len:.0f} chars)")
            if f.space_rate >= 0.5:
                add(0.15, "contains spaces")
            if f.unique_ratio >= 0.8:
                add(0.1, "mostly distinct")
        if tset & TEXT_WORDS and last not in ("id", "at", "ts", "time", "type", "count", "len", "length"):
            add(0.35, "text-like name")
        if last in ("id", "at", "ts", "time", "type", "url", "hash"):
            add(-0.4, "id/time/url-like name")
        if is_time:
            add(-0.5, "timestamp")
        if f.main_type != "str":
            add(-0.3, "not a string")
    elif role == "reply_to":
        if tset & REPLY_WORDS and (last in ("id", "to", "uuid") or "reply" in tset or "parent" in tset):
            add(0.4, "reply/parent-like name")
        if text:
            add(-0.4, "free text")
        if is_time:
            add(-0.5, "timestamp")
        if f.is_array or f.main_type in ("object", "bool"):
            add(-0.4, "not a scalar")
    elif role == "recipients":
        if f.is_array:
            add(0.25, "array")
        if tset & RECIP_WORDS:
            add(0.4, "recipient/mention-like name")
        if tset & {"reply", "parent"}:
            add(-0.3, "reply-like name")
        if text:
            add(-0.4, "free text")
        if is_time:
            add(-0.5, "timestamp")
    extra: dict[str, Any] = {}
    if role == "time" and f.time_format:
        extra["format"] = f.time_format
    return Guess(f.path, s, why, extra)


# --------------------------------------------------------------------------- tables


@dataclass
class TableProfile:
    ref: readers.Table
    fields: dict[str, FieldStats]
    rows_sampled: int
    seconds: float
    error: str | None = None

    @property
    def key(self) -> str:
        return self.ref.key

    def rows_total(self) -> tuple[int | None, bool]:
        """(count, exact?)."""
        if self.ref.eof:
            return self.rows_sampled, True
        if self.ref.total_rows is not None:
            return self.ref.total_rows, True
        if self.ref.raw_pos and self.ref.size and self.rows_sampled:
            return int(self.ref.size * self.rows_sampled / self.ref.raw_pos), False
        return None, False


def profile_table(t: readers.Table, rows: int = DEFAULT_ROWS, byte_budget: int = DEFAULT_BYTE_BUDGET) -> TableProfile:
    t0 = time.perf_counter()
    stats: dict[str, FieldStats] = {}
    n = 0
    err = None
    try:
        for row in t.rows(limit=rows, byte_budget=byte_budget):
            n += 1
            flat = flatten(row)
            for p, v in flat.items():
                fs = stats.get(p)
                if fs is None:
                    fs = stats[p] = FieldStats(p)
                fs.add(v)
    except Exception as e:  # noqa: BLE001 - a broken file is reported, never fatal
        err = f"{type(e).__name__}: {e}"
    if t.format == "sqlite":
        try:
            info = readers.sqlite_info(t.path, t.table or "")
            t.total_rows = info["rows"]
            t._extra["declared_foreign_keys"] = info["foreign_keys"]
            for c in info["columns"]:
                stats.setdefault(c["name"], FieldStats(c["name"]))
        except Exception as e:  # noqa: BLE001
            err = err or f"{type(e).__name__}: {e}"
    return TableProfile(t, stats, n, time.perf_counter() - t0, err)


def _role_guesses(tp: TableProfile) -> dict[str, list[Guess]]:
    stem_tokens = {singular(x) for x in tokens(table_stem(tp.key))}
    out: dict[str, list[Guess]] = {}
    for role in ROLES:
        gs = [_score_field(role, f, tp.rows_sampled, stem_tokens) for f in tp.fields.values() if f.values or f.rows]
        out[role] = sorted((g for g in gs if g.score >= 0.15), key=lambda g: (-g.score, g.field))
    return out


def _key_candidates(tp: TableProfile) -> list[FieldStats]:
    c = [
        f
        for f in tp.fields.values()
        if f.values
        and f.unique_ratio >= 0.95
        and f.present_rate(tp.rows_sampled) >= 0.9
        and f.time_rate < 0.8
        and f.avg_len <= 80
        and not f.is_array
        and f.main_type not in ("object", "bool", "float")
    ]
    return sorted(c, key=lambda f: (f.path.count("."), len(f.path)))[:6]


def _values(f: FieldStats) -> set[str]:
    return f.elements if f.is_array else f.distinct


def _foreign_keys(tps: list[TableProfile]) -> list[dict[str, Any]]:
    keys = {tp.key: _key_candidates(tp) for tp in tps}
    out = []
    for a in tps:
        for f in a.fields.values():
            vals = _values(f)
            if not vals or f.time_rate >= 0.8 or f.text_like() or f.main_type in ("bool", "float"):
                continue
            ftoks = {singular(x) for x in tokens(f.path)}
            for b in tps:
                for g in keys[b.key]:
                    if b is a and g.path == f.path:
                        continue
                    overlap = len(vals & g.distinct) / len(vals)
                    if overlap == 0:
                        continue
                    btoks = {singular(x) for x in tokens(table_stem(b.key))}
                    hint = bool(ftoks & btoks) or (b is a and bool(ftoks & {"reply", "parent"}))
                    if g.main_type in ("int", "str(int)") and not hint:
                        continue  # small integers overlap by accident
                    if overlap >= 0.5 or (overlap >= 0.2 and hint):
                        score = 0.7 * overlap + (0.3 if hint else 0.0)
                        out.append(
                            {
                                "from": f"{a.key}::{f.path}",
                                "to": f"{b.key}::{g.path}",
                                "from_table": a.key,
                                "from_field": f.path,
                                "to_table": b.key,
                                "to_field": g.path,
                                "overlap": round(overlap, 3),
                                "name_hint": hint,
                                "score": round(min(1.0, score), 2),
                                "self": b is a,
                                "to_sample_complete": b.ref.eof or b.ref.total_rows == b.rows_sampled,
                            }
                        )
    # keep the best target per source field
    best: dict[str, dict[str, Any]] = {}
    for fk in sorted(out, key=lambda d: -d["score"]):
        best.setdefault(fk["from"], fk)
    return sorted(best.values(), key=lambda d: (-d["score"], d["from"]))


def _agents_table(
    tps: list[TableProfile], fks: list[dict[str, Any]], roles: dict[str, dict[str, list[Guess]]]
) -> dict[str, Any] | None:
    cands = []
    for tp in tps:
        if not tp.rows_sampled:
            continue
        s, why = 0.0, []
        stoks = {singular(x) for x in tokens(table_stem(tp.key))}
        if stoks & AGENT_TABLE_WORDS:
            s += 0.4
            why.append("agent/user-like table name")
        keys = _key_candidates(tp)
        name_fields = [
            f
            for f in tp.fields.values()
            if set(tokens(f.path)) & NAME_WORDS and f.main_type == "str" and not f.text_like() and f.values
        ]
        if keys and name_fields:
            s += 0.2
            why.append("has an id and a name field")
        total, _ = tp.rows_total()
        if total is not None and total <= 10_000:
            s += 0.1
            why.append("small table")
        incoming = [fk for fk in fks if fk["to_table"] == tp.key and not fk["self"]]
        actorish = [fk for fk in incoming if set(tokens(fk["from_field"])) & ACTOR_WORDS]
        if actorish:
            s += 0.3
            why.append(f"referenced by actor-like fields ({', '.join(fk['from'] for fk in actorish[:3])})")
        has_time_text = roles[tp.key]["text"] and roles[tp.key]["text"][0].score >= 0.6
        if has_time_text and not actorish:
            s -= 0.3
            why.append("looks like a message table")
        if s < 0.3 or not keys:
            continue
        id_field = next((fk["to_field"] for fk in actorish), None) or _pick_id(tp, keys)
        disp = sorted(
            name_fields,
            key=lambda f: (
                -("display" in tokens(f.path)),
                -("name" in tokens(f.path)),
                -f.unique_ratio,
                f.path.count("."),
            ),
        )
        aliases = [
            f.path
            for f in tp.fields.values()
            if set(tokens(f.path)) & ALIAS_WORDS and f.values and f.path != (disp[0].path if disp else None)
        ]
        cands.append(
            {
                "table": tp.key,
                "score": round(min(1.0, s), 2),
                "why": why,
                "id": id_field,
                "display_name": disp[0].path if disp else id_field,
                "aliases": aliases[:3],
            }
        )
    cands.sort(key=lambda d: -d["score"])
    if not cands:
        return None
    best = dict(cands[0])
    best["alternatives"] = [{"table": c["table"], "score": c["score"]} for c in cands[1:3]]
    return best


def _pick_id(tp: TableProfile, keys: list[FieldStats]) -> str:
    stem_tokens = {singular(x) for x in tokens(table_stem(tp.key))}
    return max(keys, key=lambda f: _score_field("id", f, tp.rows_sampled, stem_tokens).score).path


def _attach_fk_hints(
    key: str, guesses: dict[str, list[Guess]], fks: list[dict[str, Any]], agents: dict[str, Any] | None
) -> None:
    by_field = {fk["from_field"]: fk for fk in fks if fk["from_table"] == key}
    agent_table = agents["table"] if agents else None
    for role in ("actor", "location", "reply_to", "recipients"):
        for g in guesses[role]:
            fk = by_field.get(g.field)
            if not fk:
                continue
            if role in ("actor", "recipients") and fk["to_table"] == agent_table:
                bonus = 0.35 * fk["overlap"]
                g.score += bonus
                g.why.append(f"+{bonus:.2f} values match agents {fk['to_field']} ({fk['overlap']:.0%})")
                g.extra["join"] = {"table": fk["to_table"], "field": fk["to_field"]}
            elif role == "location" and fk["to_table"] != agent_table and not fk["self"]:
                g.score += 0.1
                g.why.append(f"+0.10 references {fk['to_table']}")
                g.extra["join"] = {"table": fk["to_table"], "field": fk["to_field"]}
            elif role == "reply_to" and (fk["self"] or fk["to_table"] != agent_table):
                bonus = 0.35 * max(fk["overlap"], 0.5)
                g.score += bonus
                g.why.append(f"+{bonus:.2f} values are ids of {'this table' if fk['self'] else fk['to_table']}")
                g.extra["target"] = {"table": fk["to_table"], "field": fk["to_field"]}
            elif role == "location" and fk["to_table"] == agent_table:
                g.score -= 0.3
                g.why.append("-0.30 values are agent ids")
        guesses[role] = sorted((g for g in guesses[role] if g.score >= 0.15), key=lambda g: (-g.score, g.field))


def profile_path(
    path: str | Path,
    *,
    rows: int = DEFAULT_ROWS,
    byte_budget: int = DEFAULT_BYTE_BUDGET,
) -> dict[str, Any]:
    """Profile every table under ``path``. Returns a JSON-friendly dict (see module doc)."""
    t0 = time.perf_counter()
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"no such file or folder: {root}")
    tables, docs, skipped = readers.discover(root)
    tps = [profile_table(t, rows, byte_budget) for t in tables]
    roles = {tp.key: _role_guesses(tp) for tp in tps}
    fks = _foreign_keys(tps)
    agents = _agents_table(tps, fks, roles)
    for tp in tps:
        _attach_fk_hints(tp.key, roles[tp.key], fks, agents)
    out_tables = []
    for tp in tps:
        total, exact = tp.rows_total()
        d: dict[str, Any] = {
            "table": tp.key,
            "file": tp.ref.path.relative_to(root if root.is_dir() else root.parent).as_posix(),
            "format": tp.ref.format + (".gz" if tp.ref.gz else ""),
            "bytes": tp.ref.size,
            "rows_sampled": tp.rows_sampled,
            "rows_total": total,
            "rows_total_exact": exact,
            "seconds": round(tp.seconds, 3),
            "kind_guess": slug(singular(table_stem(tp.key))),
            "fields": [f.as_dict(tp.rows_sampled) for f in tp.fields.values()],
            "roles": {r: [g.as_dict() for g in gs[:3]] for r, gs in roles[tp.key].items()},
        }
        if tp.ref.table and tp.ref.format == "sqlite":
            d["sqlite_table"] = tp.ref.table
        if tp.ref.note:
            d["note"] = tp.ref.note
        if tp.ref.bad_rows:
            d["bad_rows_in_sample"] = tp.ref.bad_rows
        if tp.ref._extra.get("declared_foreign_keys"):
            d["declared_foreign_keys"] = tp.ref._extra["declared_foreign_keys"]
        if tp.error:
            d["error"] = tp.error
        d["looks_like"] = _looks_like(d, agents)
        out_tables.append(d)
    return {
        "profile_version": PROFILE_VERSION,
        "root": str(root.resolve()),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sample_rows": rows,
        "byte_budget": byte_budget,
        "seconds": round(time.perf_counter() - t0, 3),
        "tables": out_tables,
        "foreign_keys": fks,
        "agents_table": agents,
        "docs": docs,
        "skipped": skipped,
        "notes": [
            "Examples are the first distinct values, truncated to 80 chars, with emails/phones masked.",
            "Role scores are heuristics in [0, 1]; check every guess against the docs.",
        ],
    }


def _top(d: dict[str, Any], role: str) -> dict[str, Any] | None:
    gs = d["roles"].get(role) or []
    return gs[0] if gs else None


def _looks_like(d: dict[str, Any], agents: dict[str, Any] | None) -> str:
    """'agents' | 'periods' (start + end times) | 'records' (timestamped, with text or actor) | 'lookup' | 'other'."""
    if agents and agents["table"] == d["table"]:
        return "agents"
    t, x, a = _top(d, "time"), _top(d, "text"), _top(d, "actor")
    times = [set(tokens(g["field"])) for g in d["roles"].get("time") or [] if g["score"] >= 0.4]
    if any(ts & {"start", "begin", "started"} for ts in times) and any(
        ts & {"end", "stop", "ended", "finish"} for ts in times
    ):
        return "periods"
    if d["rows_total"] is not None and d["rows_total"] < 2:
        return "other"
    if t and t["score"] >= 0.5 and ((x and x["score"] >= 0.5) or (a and a["score"] >= 0.5)):
        return "records"
    if d["rows_total"] is not None and d["rows_total"] <= 5000 and _top(d, "id"):
        return "lookup"
    return "other"


def summarize(profile: dict[str, Any], top: int = 1) -> str:
    """Readable text summary of a profile (no example values).

    Names from the dataset (tables, fields, paths, errors) go through ``fence.safe_name``, so a
    name with a newline or backticks stays on one line and can't escape a markdown fence.
    """
    q = safe_name
    lines = [f"Profile of {q(profile['root'])} ({len(profile['tables'])} tables, {profile['seconds']}s)"]
    ag = profile.get("agents_table")
    if ag:
        lines.append(
            f"agents table: {q(ag['table'])} (score {ag['score']}): id={q(ag['id'])} display_name={q(ag['display_name'])}"
            + (f" aliases={','.join(q(a) for a in ag['aliases'])}" if ag["aliases"] else "")
        )
    else:
        lines.append("agents table: none found (agents can be derived from distinct actors)")
    for t in profile["tables"]:
        total = t["rows_total"]
        count = "?" if total is None else (f"{total:,}" if t["rows_total_exact"] else f"~{total:,}")
        lines.append(f"\n[{t['looks_like']}] {q(t['table'])}  ({t['format']}, {count} rows, {len(t['fields'])} fields)")
        if t.get("error"):
            lines.append(f"  error: {q(t['error'])}")
        for role in ROLES:
            gs = t["roles"].get(role) or []
            if not gs:
                continue
            parts = []
            for g in gs[:top]:
                extra = ""
                if g.get("format"):
                    extra = f" [{q(g['format'])}]"
                if g.get("join"):
                    extra += f" -> {q(g['join']['table'])}.{q(g['join']['field'])}"
                if g.get("target"):
                    extra += f" -> {q(g['target']['table'])}.{q(g['target']['field'])}"
                parts.append(f"{q(g['field'])} ({g['score']}){extra}")
            lines.append(f"  {role:<11} {'; '.join(parts)}")
    if profile["foreign_keys"]:
        lines.append("\nforeign keys (by value overlap in the samples):")
        for fk in profile["foreign_keys"][:20]:
            lines.append(f"  {q(fk['from'])} -> {q(fk['to'])}  overlap {fk['overlap']:.0%} score {fk['score']}")
    if profile["docs"]:
        lines.append("\ndocs: " + ", ".join(q(d["path"]) for d in profile["docs"]))
    if profile["skipped"]:
        lines.append("skipped: " + ", ".join(f"{q(s['path'])} ({q(s['reason'])})" for s in profile["skipped"][:10]))
    return "\n".join(lines)


def iter_guesses(profile: dict[str, Any], table: str, role: str) -> Iterable[dict[str, Any]]:
    for t in profile["tables"]:
        if t["table"] == table:
            return t["roles"].get(role) or []
    return []
