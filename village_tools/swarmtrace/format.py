"""Idea-trace format v0: types, constants, helpers and a validator.

A trace follows one idea (a belief, a norm, a coined term, ...) through a swarm of agents: who said or did
what with it (events), who could have seen it (exposures), when each agent took it up (adoptions), who
likely passed it to whom (edges), how long it stayed in agents' saved memory (persistence), plus dated
context (annotations), a chain of quotes showing how it changed, and a few summary metrics.

Nothing here is specific to any dataset; adapters in swarmtrace.adapters build traces from raw logs.
trace.schema.json (next to this file) is the JSON Schema for the same format. validate() also checks
the cross-references the schema cannot express (agent names, event ids, start <= end); check() returns
(errors, warnings), warning when data falls outside the start/end display window. Exporters should call
fit_window() so it never does.
"""

import datetime as dt
import json
import re
from typing import Literal, NotRequired, TypedDict

VERSION = 0
MAX_BYTES = 1_500_000
SNIPPET_MAX = 220
QUOTE_MAX = 160
METRICS_MAX = 10

KINDS = ("belief", "norm", "term")
CHANNELS = ("chat", "memory", "session", "search", "summary")
STANCES = ("originates", "endorses", "acts_on", "mentions", "questions", "rejects", "mutates", "uses")
CONFS = (1, 2, 3, None)
VIAS = ("room", "named", "search", "other")
EDGE_KINDS = ("transmission", "echo", "tip", "correction")
ANNOTATION_KINDS = ("goal", "intervention", "scaffolding", "artifact", "note")
WHERE = ("memory",)

Kind = Literal["belief", "norm", "term"]
Channel = Literal["chat", "memory", "session", "search", "summary"]
Stance = Literal["originates", "endorses", "acts_on", "mentions", "questions", "rejects", "mutates", "uses"]
Via = Literal["room", "named", "search", "other"]
EdgeKind = Literal["transmission", "echo", "tip", "correction"]
AnnotationKind = Literal["goal", "intervention", "scaffolding", "artifact", "note"]
ISO = str   # ISO 8601 UTC, e.g. "2025-12-03T18:04:11Z"


class Agent(TypedDict):
    name: str
    lab: str
    group: NotRequired[str]
    joined: ISO | None
    left: ISO | None


class Event(TypedDict):
    id: str
    t: ISO
    agent: str
    channel: Channel
    stance: Stance
    conf: Literal[1, 2, 3] | None
    room: str | None
    snippet: str


class Exposure(TypedDict):
    t: ISO
    agent: str
    source: str | None      # agent whose event exposed `agent`; None when not attributable to an agent
    via: Via
    event: str | None       # event id of the exposing item, if it is in `events`


class Adoption(TypedDict):
    agent: str
    t: ISO
    event: str
    independent: bool       # True when the agent had no recorded exposure before adopting
    sources: list[str]


Edge = TypedDict("Edge", {"from": str, "to": str, "t": ISO, "kind": EdgeKind, "evidence": str | None})


class Persistence(TypedDict):
    agent: str
    start: ISO
    end: ISO
    where: Literal["memory"]


class Annotation(TypedDict):
    t: ISO
    label: str
    kind: AnnotationKind


class Quote(TypedDict):
    t: ISO
    agent: str
    text: str
    note: str | None


class Trace(TypedDict):
    version: Literal[0]
    id: str
    title: str
    kind: Kind
    statement: str
    source: str
    start: ISO
    end: ISO
    agents: list[Agent]
    events: list[Event]
    exposures: list[Exposure]
    adoptions: list[Adoption]
    edges: list[Edge]
    persistence: list[Persistence]
    annotations: list[Annotation]
    quotes: list[Quote]
    metrics: dict[str, float | int | str]


class IndexEntry(TypedDict):
    id: str
    title: str
    kind: Kind
    file: str
    n_agents: int
    n_events: int
    start: ISO
    end: ISO


class Index(TypedDict):
    version: Literal[0]
    generated: ISO
    traces: list[IndexEntry]


# --- helpers -------------------------------------------------------------------------------------

_ISO_RX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_SLUG_RX = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def iso(t):
    """datetime (naive = UTC) or date -> "YYYY-MM-DDTHH:MM:SSZ"; None stays None."""
    if t is None:
        return None
    if isinstance(t, str):
        t = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    if not isinstance(t, dt.datetime):
        t = dt.datetime(t.year, t.month, t.day)
    if t.tzinfo is not None:
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    """Strict parse of a v0 timestamp; raises ValueError on anything else."""
    if not isinstance(s, str) or not _ISO_RX.match(s):
        raise ValueError(f"not an ISO 8601 UTC timestamp ending in Z: {s!r}")
    return dt.datetime.fromisoformat(s[:-1])


_EDGE_DIGITS = re.compile(r"^[\d\s().+\-/]+")       # digits and phone separators left at a cut edge


def _drop_edge(flat, lead):
    """Drop the word fragment at a cut edge of `flat` (lead: the start, else the end), plus any digits and phone
    separators next to it, so a cut never leaves part of an email address or phone number. Returns the new text
    and how many characters were dropped."""
    if lead:
        m = re.match(r"^\S*", flat)
        rest = flat[m.end():]
        d = _EDGE_DIGITS.match(rest)
        rest = rest[d.end() if d else 0:].lstrip()
        return rest, len(flat) - len(rest)
    rev, n = _drop_edge(flat[::-1], True)
    return rev[::-1], n


def clip(text, start=None, end=None, limit=SNIPPET_MAX, allow_domains=(), cut_before=False, cut_after=False):
    """Scrub PII from `text`, collapse whitespace and cut it to <= limit chars, keeping text[start:end] (the key
    phrase) in view, with an ellipsis on each cut side.

    Scrubbing (see scrub(); emails in allow_domains are kept) happens before the cut, so a cut can't leave part of
    an email or phone number that scrub() would no longer recognise. A cut also drops the word fragment at its edge
    and any digits next to it. cut_before / cut_after say `text` was already cut on that side (a stored excerpt):
    its edge fragment is dropped the same way, and no ellipsis is added for it (the caller keeps its own)."""
    text = text or ""
    if start is None:
        start, end = 0, 0
    end = start if end is None else end
    text, (start, end) = scrub_tracking(text, (start, end), allow_domains)
    if cut_before and text:
        text, n = _drop_edge(text, True)
        start, end = max(0, start - n), max(0, end - n)
    if cut_after and text:
        text, _ = _drop_edge(text, False)
        start, end = min(start, len(text)), min(end, len(text))
    # collapse whitespace while tracking where the key phrase moves to
    out, pos_map = [], []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space or not out:
                continue
            ch, prev_space = " ", True
        else:
            prev_space = False
        out.append(ch)
        pos_map.append(i)
    flat = "".join(out).rstrip()
    if len(flat) <= limit:
        return flat
    s = next((j for j, i in enumerate(pos_map) if i >= start), len(flat))
    e = next((j for j, i in enumerate(pos_map) if i >= end), len(flat))
    room = limit - 2                                  # two ellipses at most
    key = min(e - s, room)
    lo = max(0, s - (room - key) // 3)                # a third of the spare room before the phrase
    lo = min(lo, max(0, len(flat) - room))
    hi = min(len(flat), lo + room)
    snip = flat[lo:hi]
    if lo > 0:
        snip, _ = _drop_edge(snip, True) if not flat[lo - 1].isspace() else _drop_edge(" " + snip, True)
    if hi < len(flat):
        snip, _ = _drop_edge(snip, False) if not flat[hi].isspace() else _drop_edge(snip + " ", False)
    snip = snip.strip()
    return ("…" if lo > 0 else "") + snip + ("…" if hi < len(flat) else "")


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# --- PII scrubbing -------------------------------------------------------------------------------

# Boundaries are ASCII (Python's \w is Unicode, so a CJK or accented letter next to an address or number used to hide
# it). The email local part takes letters of scripts written with spaces (jöhn, иван) but not Han, kana, Hangul, Thai
# and the like, so in "連絡はbob@example.comまで" the address starts at "bob".
_NO_SPACE_SCRIPTS = "\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff66-\uff9f"
_LOCAL = f"(?:[^\\W{_NO_SPACE_SCRIPTS}]|[.%+-])"
EMAIL_RX = re.compile(rf"(?<!{_LOCAL}){_LOCAL}+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{{2,}})(?![A-Za-z0-9-])")
# Phone-like: 3-3-4 digit groups with separators (optionally +country / (area)), or +country followed by 2-4
# separated groups. Separators are required so dates, versions, IPs, ids and hashes don't match.
PHONE_RX = re.compile(r"(?<![A-Za-z0-9_.+/-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}"
                      r"(?![A-Za-z0-9_.-]*\d)"
                      r"|(?<![A-Za-z0-9_+])\+\d{1,3}(?:[\s.-]\d{2,4}){2,4}(?![A-Za-z0-9_-])")
# Free-text fields that can carry raw agent/human content.
_TEXT_FIELDS = {"events": ("snippet",), "quotes": ("text", "note"), "edges": ("evidence",), "annotations": ("label",)}
_TEXT_LIMITS = {("events", "snippet"): SNIPPET_MAX, ("quotes", "text"): QUOTE_MAX}


def _allowed(domain, allow_domains):
    d = domain.lower()
    return any(d == a.lower() or d.endswith("." + a.lower()) for a in allow_domains)


def scrub(text, allow_domains=()):
    """Replace email addresses with [email] (unless their domain, or a parent domain, is in allow_domains) and
    phone-number-like strings with [phone]."""
    if not text:
        return text
    return scrub_tracking(text, (), allow_domains)[0]


def scrub_tracking(text, marks, allow_domains=()):
    """scrub(text), also moving the character offsets in `marks` (a tuple) along with the text. An offset inside a
    replaced match moves to the start of its placeholder (the end, for the last mark of a (start, end) pair)."""
    marks = list(marks)
    for rx, repl in ((EMAIL_RX, lambda m: m.group(0) if _allowed(m.group(1), allow_domains) else "[email]"),
                     (PHONE_RX, lambda m: "[phone]")):
        out, pos, delta, moved = [], 0, 0, list(marks)
        for m in rx.finditer(text):
            r = repl(m)
            out += [text[pos:m.start()], r]
            for i, k in enumerate(marks):
                if m.start() < k < m.end():
                    moved[i] = m.start() + delta + (len(r) if i == len(marks) - 1 and i > 0 else 0)
                elif k >= m.end():
                    moved[i] += len(r) - (m.end() - m.start())
            delta += len(r) - (m.end() - m.start())
            pos = m.end()
        out.append(text[pos:])
        text, marks = "".join(out), moved
    return text, tuple(marks)


def pii_hits(text, allow_domains=()):
    """Kinds of PII patterns still present in text ("email", "phone")."""
    out = []
    if text and any(not _allowed(m.group(1), allow_domains) for m in EMAIL_RX.finditer(text)):
        out.append("email")
    if text and PHONE_RX.search(text):
        out.append("phone")
    return out


def scrub_trace(trace, allow_domains=()):
    """Scrub every free-text field that can hold raw content (snippets, quotes, edge evidence, annotation labels),
    in place, re-trimming to the field's length limit if a replacement made it longer."""
    for k, fields in _TEXT_FIELDS.items():
        for x in trace.get(k) or []:
            for f in fields:
                if isinstance(x.get(f), str):
                    s = scrub(x[f], allow_domains)
                    lim = _TEXT_LIMITS.get((k, f))
                    if lim and len(s) > lim:
                        s = s[:lim - 1].rstrip() + "…"
                    x[f] = s
    return trace


# Timestamped data that should fall inside the display window (agents' joined/left are context, not data).
_DATA_TIMES = {"events": ("t",), "exposures": ("t",), "adoptions": ("t",), "edges": ("t",),
               "persistence": ("start", "end"), "annotations": ("t",), "quotes": ("t",)}


def data_times(trace):
    """Yield (path, timestamp string) for every timestamp in the trace's data lists."""
    for k, fields in _DATA_TIMES.items():
        items = trace.get(k) if isinstance(trace.get(k), list) else []
        for i, x in enumerate(items):
            if isinstance(x, dict):
                for f in fields:
                    if isinstance(x.get(f), str):
                        yield f"{k}[{i}].{f}", x[f]


def data_span(trace):
    """(earliest, latest) parseable data timestamp as datetimes, or None when there is no data."""
    ts = []
    for _, s in data_times(trace):
        try:
            ts.append(parse_iso(s))
        except ValueError:
            pass
    return (min(ts), max(ts)) if ts else None


def fit_window(trace):
    """Widen trace start/end (in place) so every data timestamp lies inside the display window."""
    span = data_span(trace)
    if span:
        lo, hi = span
        try:
            start, end = parse_iso(trace["start"]), parse_iso(trace["end"])
        except (KeyError, ValueError):
            start, end = lo, hi
        trace["start"], trace["end"] = iso(min(start, lo)), iso(max(end, hi))
    return trace


def index_entry(trace, file):
    return {"id": trace["id"], "title": trace["title"], "kind": trace["kind"], "file": file,
            "n_agents": len(trace["agents"]), "n_events": len(trace["events"]),
            "start": trace["start"], "end": trace["end"]}


# --- validation ----------------------------------------------------------------------------------

_TOP = {"version", "id", "title", "kind", "statement", "source", "start", "end", "agents", "events", "exposures",
        "adoptions", "edges", "persistence", "annotations", "quotes", "metrics"}
_ITEM = {
    "agents": ({"name", "lab", "joined", "left"}, {"group"}),
    "events": ({"id", "t", "agent", "channel", "stance", "conf", "room", "snippet"}, set()),
    "exposures": ({"t", "agent", "source", "via", "event"}, set()),
    "adoptions": ({"agent", "t", "event", "independent", "sources"}, set()),
    "edges": ({"from", "to", "t", "kind", "evidence"}, set()),
    "persistence": ({"agent", "start", "end", "where"}, set()),
    "annotations": ({"t", "label", "kind"}, set()),
    "quotes": ({"t", "agent", "text", "note"}, set()),
}


class _Errors(list):
    def at(self, path, msg):
        self.append(f"{path}: {msg}")

    def time(self, path, v, nullable=False):
        if v is None and nullable:
            return None
        try:
            return parse_iso(v)
        except ValueError as e:
            self.at(path, str(e))
            return None

    def enum(self, path, v, allowed):
        if v not in allowed:
            self.at(path, f"{v!r} not one of {list(allowed)}")

    def text(self, path, v, nullable=False, limit=None, nonempty=False):
        if v is None and nullable:
            return
        if not isinstance(v, str):
            self.at(path, f"expected string, got {type(v).__name__}")
        elif nonempty and not v.strip():
            self.at(path, "empty string")
        elif limit is not None and len(v) > limit:
            self.at(path, f"{len(v)} chars > {limit}")


def validate(trace, max_bytes=MAX_BYTES):
    """Return a list of human-readable problems with `trace` (empty when it is valid v0). See check() for warnings."""
    return check(trace, max_bytes)[0]


def check(trace, max_bytes=MAX_BYTES, allow_domains=()):
    """(errors, warnings) for `trace`. Errors make it invalid v0; warnings flag valid but questionable data: email
    addresses (outside allow_domains) or phone-like strings left in free text, and data timestamps outside the
    start/end display window. Warnings name the field, never the matched text."""
    errors = _validate(trace, max_bytes)
    warnings = []
    if isinstance(trace, dict):
        pii = []
        for k, fields in _TEXT_FIELDS.items():
            items = trace.get(k) if isinstance(trace.get(k), list) else []
            for i, x in enumerate(items):
                if isinstance(x, dict):
                    for f in fields:
                        if isinstance(x.get(f), str):
                            pii += [f"{k}[{i}].{f} ({kind})" for kind in pii_hits(x[f], allow_domains)]
        if pii:
            warnings.append(f"{len(pii)} free-text fields still contain an email address or phone number: "
                            f"{', '.join(pii[:5])}")
        try:
            start, end = parse_iso(trace.get("start")), parse_iso(trace.get("end"))
        except ValueError:
            return errors, warnings
        outside = []
        for path, s in data_times(trace):
            try:
                t = parse_iso(s)
            except ValueError:
                continue
            if t < start or t > end:
                outside.append(f"{path} = {s}")
        if outside:
            warnings.append(f"{len(outside)} data timestamps outside the window {trace['start']} .. {trace['end']}, "
                            f"e.g. {', '.join(outside[:3])}")
    return errors, warnings


def _validate(trace, max_bytes):
    err = _Errors()
    if not isinstance(trace, dict):
        return ["trace: expected an object"]
    for k in sorted(_TOP - trace.keys()):
        err.at(k, "missing")
    for k in sorted(trace.keys() - _TOP):
        err.at(k, "unknown field")
    if trace.get("version") != VERSION or isinstance(trace.get("version"), bool):     # false == 0 in Python
        err.at("version", f"expected {VERSION}, got {trace.get('version')!r}")
    if not (isinstance(trace.get("id"), str) and _SLUG_RX.match(trace["id"])):
        err.at("id", f"not a lowercase slug: {trace.get('id')!r}")
    err.enum("kind", trace.get("kind"), KINDS)
    for k in ("title", "statement", "source"):
        if k in trace:
            err.text(k, trace[k], nonempty=True)
    t0, t1 = err.time("start", trace.get("start")), err.time("end", trace.get("end"))
    if t0 and t1 and t0 > t1:
        err.at("start", "after end")

    lists = {}
    for k in _ITEM:
        v = trace.get(k, [])
        if not isinstance(v, list):
            err.at(k, "expected a list")
            v = []
        lists[k] = v
        need, opt = _ITEM[k]
        for i, x in enumerate(v):
            if not isinstance(x, dict):
                err.at(f"{k}[{i}]", "expected an object")
                continue
            for f in sorted(need - x.keys()):
                err.at(f"{k}[{i}].{f}", "missing")
            for f in sorted(x.keys() - need - opt):
                err.at(f"{k}[{i}].{f}", "unknown field")

    def items(k):
        return [(f"{k}[{i}]", x) for i, x in enumerate(lists[k]) if isinstance(x, dict)]

    names = set()                                   # string names only, so a list or dict can't break the lookups
    for p, a in items("agents"):
        err.text(f"{p}.name", a.get("name"), nonempty=True)
        if isinstance(a.get("name"), str):
            if a["name"] in names:
                err.at(f"{p}.name", f"duplicate agent {a['name']!r}")
            names.add(a["name"])
        err.text(f"{p}.lab", a.get("lab"), nonempty=True)
        if "group" in a:
            err.text(f"{p}.group", a["group"])
        j = err.time(f"{p}.joined", a.get("joined"), nullable=True)
        l_ = err.time(f"{p}.left", a.get("left"), nullable=True)
        if j and l_ and j > l_:
            err.at(f"{p}", "joined after left")

    def agent_ref(path, v, nullable=False):
        if v is None and nullable:
            return
        if not isinstance(v, str) or v not in names:
            err.at(path, f"{v!r} not in agents")

    ids = set()
    for p, e in items("events"):
        if not isinstance(e.get("id"), str) or not e["id"].strip():
            err.at(f"{p}.id", "expected a non-empty string")
        else:
            if e["id"] in ids:
                err.at(f"{p}.id", f"duplicate event id {e['id']!r}")
            ids.add(e["id"])
        err.time(f"{p}.t", e.get("t"))
        agent_ref(f"{p}.agent", e.get("agent"))
        err.enum(f"{p}.channel", e.get("channel"), CHANNELS)
        err.enum(f"{p}.stance", e.get("stance"), STANCES)
        if e.get("conf") not in CONFS or isinstance(e.get("conf"), bool):
            err.at(f"{p}.conf", f"{e.get('conf')!r} not one of [1, 2, 3, null]")
        err.text(f"{p}.room", e.get("room"), nullable=True)
        err.text(f"{p}.snippet", e.get("snippet"), limit=SNIPPET_MAX)

    def event_ref(path, v, nullable=False):
        if v is None and nullable:
            return
        if not isinstance(v, str) or v not in ids:
            err.at(path, f"{v!r} not an event id")

    for p, x in items("exposures"):
        err.time(f"{p}.t", x.get("t"))
        agent_ref(f"{p}.agent", x.get("agent"))
        agent_ref(f"{p}.source", x.get("source"), nullable=True)
        err.enum(f"{p}.via", x.get("via"), VIAS)
        event_ref(f"{p}.event", x.get("event"), nullable=True)
    for p, x in items("adoptions"):
        agent_ref(f"{p}.agent", x.get("agent"))
        err.time(f"{p}.t", x.get("t"))
        event_ref(f"{p}.event", x.get("event"))
        if not isinstance(x.get("independent"), bool):
            err.at(f"{p}.independent", "expected true/false")
        src = x.get("sources")
        if not isinstance(src, list):
            err.at(f"{p}.sources", "expected a list")
        else:
            for j, s in enumerate(src):
                agent_ref(f"{p}.sources[{j}]", s)
    for p, x in items("edges"):
        agent_ref(f"{p}.from", x.get("from"))
        agent_ref(f"{p}.to", x.get("to"))
        err.time(f"{p}.t", x.get("t"))
        err.enum(f"{p}.kind", x.get("kind"), EDGE_KINDS)
        err.text(f"{p}.evidence", x.get("evidence"), nullable=True)
    for p, x in items("persistence"):
        agent_ref(f"{p}.agent", x.get("agent"))
        a, b = err.time(f"{p}.start", x.get("start")), err.time(f"{p}.end", x.get("end"))
        if a and b and a > b:
            err.at(p, "start after end")
        err.enum(f"{p}.where", x.get("where"), WHERE)
    for p, x in items("annotations"):
        err.time(f"{p}.t", x.get("t"))
        err.text(f"{p}.label", x.get("label"), nonempty=True)
        err.enum(f"{p}.kind", x.get("kind"), ANNOTATION_KINDS)
    for p, x in items("quotes"):
        err.time(f"{p}.t", x.get("t"))
        agent_ref(f"{p}.agent", x.get("agent"))
        err.text(f"{p}.text", x.get("text"), limit=QUOTE_MAX, nonempty=True)
        err.text(f"{p}.note", x.get("note"), nullable=True)

    m = trace.get("metrics", {})
    if not isinstance(m, dict):
        err.at("metrics", "expected an object")
    else:
        if len(m) > METRICS_MAX:
            err.at("metrics", f"{len(m)} entries > {METRICS_MAX}")
        for k, v in m.items():
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                err.at(f"metrics.{k}", f"expected number or string, got {type(v).__name__}")

    if max_bytes is not None:
        try:
            n = len(dumps(trace).encode())
            if n > max_bytes:
                err.at("trace", f"{n:,} bytes serialized > {max_bytes:,}")
        except (TypeError, ValueError) as e:
            err.at("trace", f"not JSON-serializable: {e}")
    return list(err)


def validate_index(index):
    """Problems with an index.json object (empty list when valid)."""
    err = _Errors()
    if not isinstance(index, dict):
        return ["index: expected an object"]
    if index.get("version") != VERSION or isinstance(index.get("version"), bool):
        err.at("version", f"expected {VERSION}, got {index.get('version')!r}")
    err.time("generated", index.get("generated"))
    tr = index.get("traces")
    if not isinstance(tr, list):
        return list(err) + ["traces: expected a list"]
    seen = set()
    need = {"id", "title", "kind", "file", "n_agents", "n_events", "start", "end"}
    for i, x in enumerate(tr):
        p = f"traces[{i}]"
        if not isinstance(x, dict):
            err.at(p, "expected an object")
            continue
        for f in sorted(need - x.keys()):
            err.at(f"{p}.{f}", "missing")
        for f in sorted(x.keys() - need):
            err.at(f"{p}.{f}", "unknown field")
        err.text(f"{p}.id", x.get("id"), nonempty=True)
        if isinstance(x.get("id"), str):
            if x["id"] in seen:
                err.at(f"{p}.id", f"duplicate id {x['id']!r}")
            seen.add(x["id"])
        err.enum(f"{p}.kind", x.get("kind"), KINDS)
        err.text(f"{p}.file", x.get("file"), nonempty=True)
        for f in ("n_agents", "n_events"):
            if isinstance(x.get(f), bool) or not isinstance(x.get(f), int) or x.get(f) < 0:
                err.at(f"{p}.{f}", "expected a non-negative integer")
        err.time(f"{p}.start", x.get("start"))
        err.time(f"{p}.end", x.get("end"))
    return list(err)
