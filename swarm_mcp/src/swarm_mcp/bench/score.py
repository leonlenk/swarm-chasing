"""Score tool outputs against a generated ``truth.json``.

``score(truth, outputs)`` returns per-task precision / recall / F1 plus a summary.
``outputs`` holds the three tools' results in the shapes from ``contracts.md``::

    {"diffusion": {term: {...}}, "coordinators": {...} | [...], "integrity": {...}}
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from swarm_mcp.bench._common import normalize_eid, parse_ts

LABELS = ("likely_copier", "possibly_independent")
_LABEL_ALIASES = {
    "copier": "likely_copier",
    "likely_copy": "likely_copier",
    "copied": "likely_copier",
    "independent": "possibly_independent",
    "possibly_independent_invention": "possibly_independent",
    "parallel": "possibly_independent",
}
_ISSUE_ALIASES = {
    "missing_event": "missing_event",
    "missing_talk_event": "missing_event",
    "missing_agent_talk": "missing_event",
    "no_event": "missing_event",
    "unmatched_message": "missing_event",
    "speaker_mismatch": "speaker_mismatch",
    "agent_mismatch": "speaker_mismatch",
    "actor_mismatch": "speaker_mismatch",
    "attribution_mismatch": "speaker_mismatch",
}
GAP_IOU = 0.5


def prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    p = tp / (tp + fp) if tp + fp else (1.0 if tp + fn == 0 else 0.0)
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4)}


def _set_prf(truth: set, pred: set) -> dict[str, Any]:
    return prf(len(truth & pred), len(pred - truth), len(truth - pred))


class Actors:
    """Resolve an actor reference (agent eid, bare uuid, or exact display name) to its agent eid."""

    def __init__(self, truth: dict[str, Any]):
        self.map: dict[str, str] = {}
        for eid, a in (truth.get("agents") or {}).items():
            self.map[eid] = eid
            self.map[a["native_id"]] = eid
            self.map.setdefault(a["name"], eid)

    def __call__(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, dict):  # tolerate {"id": ..., "name": ...}
            value = value.get("agent_id") or value.get("id") or value.get("actor") or value.get("name")
        s = str(value).strip()
        return self.map.get(s) or self.map.get(normalize_eid(s) or "") or s


def _label(value: Any) -> str:
    s = str(value or "").strip().lower()
    return _LABEL_ALIASES.get(s, s)


def _ids(values: Iterable[Any] | None) -> list[str]:
    return [e for e in (normalize_eid(v) for v in (values or [])) if e]


# ---------------------------------------------------------------- diffusion
def score_diffusion(truth: dict[str, Any], output: Any) -> dict[str, Any]:
    actors = Actors(truth)
    terms = truth.get("diffusion") or {}
    out = output if isinstance(output, dict) else {}
    by_term = {str(k).lower(): v for k, v in out.items()}
    t_items: set[tuple] = set()
    p_items: set[tuple] = set()
    first_ok = first_n = 0
    fe_ok = basis_ok = matched = 0
    per_term = {}
    for term, t in terms.items():
        t_first = normalize_eid(t["first"])
        t_ad = {a["actor"]: a for a in t["adopters"]}
        ti = {("first", term, t_first)} | {("adopter", term, a, x["label"]) for a, x in t_ad.items()}
        pred = by_term.get(term.lower())
        pi: set[tuple] = set()
        if isinstance(pred, dict):
            p_first = normalize_eid(pred.get("first") or pred.get("first_event_id"))
            if p_first:
                pi.add(("first", term, p_first))
            first_ok += int(p_first == t_first)
            for ad in pred.get("adopters") or []:
                actor = actors(ad.get("actor"))
                pi.add(("adopter", term, actor, _label(ad.get("label"))))
                truth_ad = t_ad.get(actor)
                if truth_ad and _label(ad.get("label")) == truth_ad["label"]:
                    matched += 1
                    fe_ok += int(normalize_eid(ad.get("first_event_id")) == normalize_eid(truth_ad["first_event_id"]))
                    basis = normalize_eid(ad.get("basis_event_id"))
                    ok = set(_ids(truth_ad.get("acceptable_basis_event_ids")))
                    basis_ok += int(basis in ok if ok else basis is None)
        first_n += 1
        per_term[term] = {"kind": t.get("kind"), "present": isinstance(pred, dict), **_set_prf(ti, pi)}
        t_items |= ti
        p_items |= pi
    res = _set_prf(t_items, p_items)
    res["first_accuracy"] = round(first_ok / first_n, 4) if first_n else None
    res["adopter_first_event_accuracy"] = round(fe_ok / matched, 4) if matched else None
    res["basis_accuracy"] = round(basis_ok / matched, 4) if matched else None
    res["per_term"] = per_term
    res["present"] = isinstance(output, dict) and any(isinstance(by_term.get(t.lower()), dict) for t in terms)
    return res


# ---------------------------------------------------------------- coordinators
def _coord_list(output: Any) -> list[dict[str, Any]] | None:
    if isinstance(output, dict):
        output = output.get("coordinators", output.get("ranked", output.get("results")))
    if not isinstance(output, list):
        return None
    items = [x if isinstance(x, dict) else {"actor": x} for x in output]
    if all(isinstance(x.get("score"), (int, float)) for x in items):
        items = sorted(items, key=lambda x: -x["score"])  # stable: ties keep the tool's order
    return items


def score_coordinators(truth: dict[str, Any], output: Any) -> dict[str, Any]:
    actors = Actors(truth)
    t = truth.get("coordinators") or {}
    true = list(t.get("ranked") or [])
    items = _coord_list(output)
    if items is None:
        return {**prf(0, 0, len(true)), "present": False, "ranks": {}, "mrr": 0.0, "evidence_precision": None}
    ranked = []
    for x in items:
        a = actors(x.get("actor"))
        if a not in ranked:
            ranked.append(a)
    k = len(true)
    res = _set_prf(set(true), set(ranked[:k]))
    ranks = {a: (ranked.index(a) + 1 if a in ranked else None) for a in true}
    res["ranks"] = ranks
    res["mrr"] = round(sum(1 / r for r in ranks.values() if r) / k, 4) if k else None
    planted = set(_ids(t.get("directive_event_ids"))) | set(_ids(t.get("reply_event_ids")))
    planted |= set(_ids(t.get("session_goal_event_ids")))
    ev = [e for x in items if actors(x.get("actor")) in true for e in _ids(x.get("example_event_ids"))]
    res["evidence_precision"] = round(sum(e in planted for e in ev) / len(ev), 4) if ev else None
    res["present"] = True
    return res


# ---------------------------------------------------------------- integrity
def _pairs(entry: dict[str, Any], actors: Actors) -> set[frozenset]:
    group = entry.get("agents") or entry.get("actors") or entry.get("pair") or []
    ids = sorted({actors(a) for a in group if a is not None})
    return {frozenset((a, b)) for i, a in enumerate(ids) for b in ids[i + 1 :]}


def _iou(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> float:
    inter = (min(a[1], b[1]) - max(a[0], b[0])).total_seconds()
    union = (max(a[1], b[1]) - min(a[0], b[0])).total_seconds()
    return max(0.0, inter) / union if union > 0 else 0.0


def _score_gaps(truth_gaps: list[dict], pred_gaps: list[dict], actors: Actors) -> dict[str, Any]:
    tg = [(g["actor"], (parse_ts(g["start"]), parse_ts(g["end"]))) for g in truth_gaps]
    used: set[int] = set()
    tp = fp = 0
    for p in pred_gaps:
        actor = actors(p.get("actor"))
        win = (parse_ts(p.get("start")), parse_ts(p.get("end")))
        hit = None
        if win[0] and win[1]:
            for i, (ta, tw) in enumerate(tg):
                if i not in used and ta == actor and _iou(win, tw) >= GAP_IOU:
                    hit = i
                    break
        if hit is None:
            fp += 1
        else:
            used.add(hit)
            tp += 1
    return prf(tp, fp, len(tg) - tp)


def score_integrity(truth: dict[str, Any], output: Any) -> dict[str, Any]:
    actors = Actors(truth)
    t = truth.get("integrity") or {}
    out = output if isinstance(output, dict) else {}
    # name collisions: unordered agent pairs
    t_pairs = set().union(*[_pairs(e, actors) for e in t.get("name_collisions") or []] or [set()])
    p_pairs = set().union(
        *[_pairs(e, actors) for e in out.get("name_collisions") or [] if isinstance(e, dict)] or [set()]
    )
    names = _set_prf(t_pairs, p_pairs)
    # gaps: same actor, IoU >= 0.5
    gaps = _score_gaps(t.get("gaps") or [], [g for g in out.get("gaps") or [] if isinstance(g, dict)], actors)
    # attribution: (chat id, issue); a talk event id maps back to its chat message
    talk_to_chat = {
        normalize_eid(x["talk_event_id"]): x["event_id"]
        for x in t.get("attribution_issues") or []
        if x.get("talk_event_id")
    }
    t_attr = {(x["event_id"], x["issue"]) for x in t.get("attribution_issues") or []}
    p_attr = set()
    for x in out.get("attribution_issues") or []:
        if not isinstance(x, dict):
            continue
        eid = normalize_eid(x.get("event_id") or x.get("message_id") or x.get("chat_event_id"))
        eid = talk_to_chat.get(eid, eid)
        issue = _ISSUE_ALIASES.get(str(x.get("issue") or x.get("kind") or "").strip().lower(), str(x.get("issue")))
        p_attr.add((eid, issue))
    attr = _set_prf(t_attr, p_attr)
    subs = {"name_collisions": names, "gaps": gaps, "attribution_issues": attr}
    res = prf(*(sum(s[k] for s in subs.values()) for k in ("tp", "fp", "fn")))
    res["subtasks"] = subs
    res["present"] = isinstance(output, dict)
    return res


# ---------------------------------------------------------------- all
def score(truth: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    """Per-task precision/recall/F1 and a summary (macro F1 over diffusion, coordinators, integrity)."""
    outputs = outputs or {}
    tasks = {
        "diffusion": score_diffusion(truth, outputs.get("diffusion")),
        "coordinators": score_coordinators(truth, outputs.get("coordinators")),
        "integrity": score_integrity(truth, outputs.get("integrity")),
    }
    f1s = {k: v["f1"] for k, v in tasks.items()}
    f1s |= {f"integrity.{k}": v["f1"] for k, v in tasks["integrity"]["subtasks"].items()}
    summary = {
        "macro_f1": round(sum(tasks[k]["f1"] for k in ("diffusion", "coordinators", "integrity")) / 3, 4),
        "f1": f1s,
        "missing": [k for k, v in tasks.items() if not v.get("present")],
        "seed": truth.get("seed"),
        "params": truth.get("params"),
    }
    return {"summary": summary, "tasks": tasks}
