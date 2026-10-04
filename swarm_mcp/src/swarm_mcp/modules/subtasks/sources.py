"""Build work units for ``infer`` from the SwarmScope store, for any ingested source.

Nothing here knows about git or wikis: everything comes from the generic tables.

  units      periods whose records point at them (``run_id``, or ``meta.members``): pull requests,
             runs... Otherwise, when a source has no such periods, *sessions*: one actor's records with
             gaps of at most ``session_gap`` seconds.
  actions    the unit's actions and messages; their ``touches`` say what they did to which artifact
  artifacts  touched artifacts; ``meta.hub`` / ``meta.role == 'test'`` / ``meta.category`` refine signals
  refs       'mention' touches of an artifact another unit created, and '#N'-style references to a
             period's ``meta.number``
  chat       messages from *other* sources that name a period's number ('PR #12', '#152') in its time
             window, e.g. AI Village chat about the rpg-game repo's pull requests
  identity   an actor is shown under the name of the agent in another source that shares a normalised
             name or alias (git author 'claude-opus-4-6' -> village agent 'Claude Opus 4.6')
"""

from __future__ import annotations

import collections
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from swarm_mcp.modules.subtasks.infer import PR_RX, Action, Change, ChatMsg, Inference, Unit, infer
from swarm_mcp.scope.db import Store, norm
from swarm_mcp.toolkit import TS_FORMAT, safe_label

SESSION_GAP = 30 * 60


@dataclass
class Corpus:
    name: str  # the store source
    unit_noun: str
    action_noun: str
    artifact_noun: str
    units: list[Unit]
    chat: list[ChatMsg]
    artifact_meta: dict[str, dict[str, Any]]  # artifact id -> meta (+ "name")
    notes: list[str] = field(default_factory=list)
    tag_name: str | None = None
    dup_min: float = 0.5
    aliases: dict[str, list[str]] = field(default_factory=dict)  # shown actor name -> names that resolve to it


def _meta(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v:
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return {}


def _ts(v: Any) -> str:
    return v.strftime(TS_FORMAT) if isinstance(v, datetime) else (v or "")


def corpus_sources(s: Store) -> list[str]:
    """Sources with artifacts that records touched: the ones subtasks can analyse."""
    if not s.has_table("touches"):
        return []
    return [r["source"] for r in s.all("SELECT DISTINCT source FROM touches ORDER BY source")]


def identity_map(s: Store, source: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """agent_id (of ``source``) -> display name, preferring the name of a matching agent in another source."""
    rows = s.all("SELECT agent_id, source, display_name, aliases FROM agents")
    keys: dict[str, set[str]] = collections.defaultdict(set)  # norm name -> display names in other sources
    other_aliases: dict[str, set[str]] = collections.defaultdict(set)
    for r in rows:
        if r["source"] == source:
            continue
        for n in [r["display_name"], *(r["aliases"] or [])]:
            if n:
                keys[norm(n)].add(r["display_name"])
                other_aliases[r["display_name"]].add(n)
    out, aliases = {}, collections.defaultdict(list)
    for r in rows:
        if r["source"] != source:
            continue
        own = [r["display_name"], *(r["aliases"] or [])]
        matches = set().union(*(keys.get(norm(n), set()) for n in own if n))
        raw = next(iter(matches)) if len(matches) == 1 else r["display_name"]
        name = safe_label(raw)  # shown bare in results
        out[r["agent_id"]] = name
        aliases[name].extend(n for n in own if n)
        aliases[name].extend(sorted(other_aliases.get(raw, ())))
    return out, dict(aliases)


def load_corpus(s: Store, source: str, session_gap: int = SESSION_GAP) -> Corpus:
    who, aliases = identity_map(s, source)
    notes: list[str] = []
    src_meta = _meta((s.one("SELECT meta FROM sources WHERE source = ?", [source]) or {}).get("meta"))
    notes += list(src_meta.get("notes") or [])

    # ---- artifacts and touches
    artifact_meta: dict[str, dict[str, Any]] = {}
    kinds = collections.Counter()
    for r in s.all("SELECT artifact_id, kind, name, meta FROM artifacts WHERE source = ?", [source]):
        artifact_meta[r["artifact_id"]] = {**_meta(r["meta"]), "name": r["name"]}
        kinds[r["kind"]] += 1
    touches: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in s.all(
        "SELECT record_id, artifact_id, op, meta FROM touches WHERE source = ? ORDER BY ts, touch_id", [source]
    ):
        touches[r["record_id"]].append({"artifact": r["artifact_id"], "op": r["op"], "meta": _meta(r["meta"])})

    # ---- records (actions + messages) of the source
    recs: dict[str, dict[str, Any]] = {}
    rkinds = collections.Counter()
    for r in s.all(
        "SELECT evidence_id, agent_id AS actor, ts, kind, content, run_id, meta FROM actions WHERE source = ?", [source]
    ):
        recs[r["evidence_id"]] = {**r, "meta": _meta(r["meta"])}
        rkinds[r["kind"]] += 1
    for r in s.all(
        "SELECT evidence_id, author_id AS actor, ts, coalesce(msg_type, 'message') AS kind, content, NULL AS run_id, "
        "meta, channel FROM messages WHERE source = ?",
        [source],
    ):
        recs[r["evidence_id"]] = {**r, "meta": _meta(r["meta"])}
        rkinds[r["kind"]] += 1

    def action(eid: str) -> Action:
        r = recs[eid]
        changes, mentions = [], []
        for t in touches.get(eid, []):
            if t["op"] == "mention":
                mentions.append(t["artifact"])
                continue
            m = t["meta"]
            changes.append(
                Change(
                    t["artifact"],
                    new=t["op"] == "create",
                    size=int(m.get("added", 0) or 0) + int(m.get("removed", 0) or 0),
                    lines=list(m.get("lines") or []),
                    imports=list(m.get("imports") or []),
                    rewrite=bool(m.get("rewrite")),
                )
            )
        meta = r["meta"]
        text = meta.get("summary") or (r["content"] or "").split("\n", 1)[0]
        if r.get("channel"):
            text = f"{r['channel'].split(':', 1)[-1]} {text}"
        a = Action(eid, who.get(r["actor"], r["actor"] or "?"), _ts(r["ts"]), text[:300], changes)
        a.mentions = mentions  # type: ignore[attr-defined]
        return a

    # ---- units: periods that group records, else sessions
    periods = s.all(
        "SELECT evidence_id, kind, label, start_ts, end_ts, meta FROM periods WHERE source = ? ORDER BY start_ts",
        [source],
    )
    members: dict[str, list[str]] = collections.defaultdict(list)
    for eid, r in recs.items():
        if r.get("run_id"):
            members[r["run_id"]].append(eid)
    for p in periods:
        for eid in _meta(p["meta"]).get("members") or []:
            if eid in recs and eid not in members[p["evidence_id"]]:
                members[p["evidence_id"]].append(eid)
    grouping = [p for p in periods if members.get(p["evidence_id"])]
    units: list[Unit] = []
    numbers: dict[int, str] = {}
    if grouping:
        noun = collections.Counter(p["kind"] for p in grouping).most_common(1)[0][0].replace("_", " ")
        grouped = {e for p in grouping for e in members[p["evidence_id"]]}
        loose = sum(1 for e, r in recs.items() if e not in grouped and touches.get(e))
        if loose:
            notes.append(f"{loose} records that touched artifacts belong to no {noun} and are left out")
        for p in grouping:
            m = _meta(p["meta"])
            mids = sorted(members[p["evidence_id"]], key=lambda e: (_ts(recs[e]["ts"]), e))
            acts = [action(e) for e in mids]
            roles = {who.get(a, a): set(rs) for a, rs in (m.get("roles") or {}).items()}
            if isinstance(m.get("number"), int):
                numbers[m["number"]] = p["evidence_id"]
            units.append(
                Unit(
                    event_id=p["evidence_id"],
                    short=m.get("short") or (p["label"] or p["evidence_id"])[:60],
                    title=p["label"] or "",
                    start=_ts(p["start_ts"]) or (acts[0].time if acts else ""),
                    end=_ts(p["end_ts"]) or None,
                    state=str(m.get("state") or p["kind"]),
                    completed=m.get("completed"),
                    actions=acts,
                    roles=roles,
                )
            )
    else:
        noun = "session"
        by_actor: dict[str, list[str]] = collections.defaultdict(list)
        for eid in sorted(recs, key=lambda e: (_ts(recs[e]["ts"]), e)):
            if touches.get(eid) and recs[eid]["ts"] is not None:
                by_actor[recs[eid]["actor"]].append(eid)
        for eids in by_actor.values():
            cur: list[str] = []
            for eid in eids + [None]:  # type: ignore[list-item]
                if cur and (eid is None or (recs[eid]["ts"] - recs[cur[-1]]["ts"]).total_seconds() > session_gap):
                    acts = [action(e) for e in cur]
                    created = any(c.new for a in acts for c in a.changes)
                    units.append(
                        Unit(
                            event_id=cur[0],
                            short=f"session from {cur[0].split(':', 2)[-1]}",
                            title=" ".join(a.text for a in acts)[:400],
                            start=acts[0].time,
                            end=acts[-1].time,
                            state="created" if created else "edited",
                            completed=None,
                            actions=acts,
                        )
                    )
                    cur = []
                if eid is not None:
                    cur.append(eid)
        notes.append(
            f"units are sessions: one actor's records with gaps <= {session_gap // 60} min (the session id is its "
            "first record); an actor is whatever the source calls one, which may be many agents"
        )

    # ---- tags (artifact categories), refs (mentions + '#N')
    creator_unit: dict[str, str] = {}
    for u in sorted(units, key=lambda u: u.start):
        for a in u.actions:
            for c in a.changes:
                if c.new:
                    creator_unit.setdefault(c.artifact, u.event_id)
    has_cat = False
    for u in units:
        tags: collections.Counter = collections.Counter()
        for a in u.actions:
            for c in a.changes:
                cat = artifact_meta.get(c.artifact, {}).get("category")
                if cat:
                    tags[cat] += 1
                    has_cat = True
            for art in getattr(a, "mentions", []):
                cu = creator_unit.get(art)
                if cu and cu != u.event_id:
                    u.refs.add(cu)
        u.tags = dict(tags)
        if numbers:
            text = " ".join([u.title] + [a.text for a in u.actions])
            for n in re.findall(r"#(\d+)", text):
                if int(n) in numbers and numbers[int(n)] != u.event_id:
                    u.refs.add(numbers[int(n)])

    # ---- chat from other sources naming period numbers
    chat: list[ChatMsg] = []
    if numbers and units:
        lo = min(u.start for u in units if u.start)
        hi = max((u.end or u.start) for u in units if u.start)
        lo_dt = datetime.strptime(lo, TS_FORMAT) - timedelta(days=1)
        hi_dt = datetime.strptime(hi, TS_FORMAT) + timedelta(days=1)
        names = s.display_names()
        rows = s.all(
            "SELECT evidence_id, source, author_id, ts, content FROM messages WHERE source <> ? AND ts >= ? AND ts < ? "
            "AND regexp_matches(content, '(?i)(PRs?\\s*#?|pull/|#)\\d') ORDER BY ts, evidence_id",
            [source, lo_dt, hi_dt],
        )
        for r in rows:
            nums = sorted({int(a or b) for a, b in PR_RX.findall(r["content"] or "")} & set(numbers))
            if nums:
                actor = safe_label(names.get(r["author_id"], r["author_id"]))
                chat.append(
                    ChatMsg(r["evidence_id"], _ts(r["ts"]), actor, r["content"] or "", [numbers[n] for n in nums])
                )
        if chat:
            srcs = sorted({c.event_id.split(":", 1)[0] for c in chat})
            notes.append(f"chat signal: {len(chat)} messages from {', '.join(srcs)} that name a {noun} number")
    return Corpus(
        name=source,
        unit_noun=noun,
        action_noun=rkinds.most_common(1)[0][0] if rkinds else "record",
        artifact_noun=kinds.most_common(1)[0][0] if kinds else "artifact",
        units=units,
        chat=chat,
        artifact_meta=artifact_meta,
        notes=notes,
        tag_name="category (the dataset's own label for artifacts)" if has_cat else None,
        dup_min=0.8 if noun == "session" else 0.5,
        aliases=aliases,
    )


def build(s: Store, source: str) -> tuple[Corpus, Inference]:
    """Load ``source`` from the store and run inference: what the subtasks tools and the renderer both use."""
    c = load_corpus(s, source)
    if not c.units:
        raise ValueError(f"Source {source!r} has no work units (no records that touch artifacts).")
    inf = infer(c.name, c.units, c.chat, dup_min=c.dup_min, artifact_meta=c.artifact_meta)
    inf.notes.extend(c.notes)
    return c, inf
