"""Self-contained agent-name matching (used at ingest time for recipient_ids and aliases).

Builds aliases from the roster (full names plus obvious short forms such as
"Opus 4.5" for "Claude Opus 4.5", "GPT 5.2"/"GPT5.2" for "GPT-5.2",
"Gemini 2.5" for "Gemini 2.5 Pro") and finds them in free text with
word-ish boundaries, so "Opus 4" never matches inside "Opus 4.5".
Aliases that would point at more than one agent are dropped.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

_DROP_SUFFIX = ("pro", "flash")


def norm(s: str) -> str:
    """Normalize for lookup: lowercase, drop spaces/hyphens/underscores/brackets."""
    return re.sub(r"[\s\-_\[\]()]+", "", s.lower())


def variants(name: str) -> set[str]:
    out = {name}
    base = re.sub(r"^\[[^\]]*\]\s*", "", name)  # "[Temporary] Fine-tuned Leader"
    out.add(base)
    if base.lower().startswith("claude "):
        rest = base[7:]
        out.add(rest)  # "Opus 4.5", "Fable 5", "3.7 Sonnet"
        m = re.fullmatch(r"(\d+(?:\.\d+)?) (\w+)", rest)
        if m:
            out.add(f"{m.group(2)} {m.group(1)}")  # "Sonnet 3.7"
    words = base.split()
    if len(words) >= 2 and words[-1].lower() in _DROP_SUFFIX:
        out.add(" ".join(words[:-1]))  # "Gemini 2.5", "GLM-5.3"
    if base.lower().endswith("-pro"):
        out.add(base[:-4])  # "DeepSeek-V4"
    m = re.fullmatch(r"Kimi (K\d+\.\d+)", base)
    if m:
        out.add(m.group(1))  # "K2.6"
    return {v.strip() for v in out if len(norm(v)) >= 2}


def _pattern(alias: str) -> str:
    tokens = [t for t in re.split(r"[\s\-]+", alias) if t]
    return r"[\s\-]?".join(re.escape(t) for t in tokens)


class NameMatcher:
    def __init__(self, agents: Iterable[tuple[str, str]]):
        """``agents``: (agent_id, display_name) pairs."""
        self.names: dict[str, str] = {}
        full: dict[str, str] = {}
        for aid, name in agents:
            self.names[aid] = name
            full[norm(name)] = aid
        candidates: dict[str, set[str]] = defaultdict(set)
        surface: dict[str, str] = {}
        for aid, name in self.names.items():
            for v in variants(name):
                candidates[norm(v)].add(aid)
                surface.setdefault(norm(v), v)
        self.alias_to_id: dict[str, str] = {}
        self.ambiguous: dict[str, list[str]] = {}
        for key, ids in candidates.items():
            if key in full:  # a real full name always wins
                self.alias_to_id[key] = full[key]
            elif len(ids) == 1:
                self.alias_to_id[key] = next(iter(ids))
            else:
                self.ambiguous[surface[key]] = sorted(self.names[i] for i in ids)
        # Matching runs on lower-cased text with case-sensitive patterns (much
        # faster than re.IGNORECASE). A cheap "head word" scan finds candidate
        # positions; the full alias alternation is then tried anchored there.
        forms = sorted({surface.get(k, k).lower() for k in self.alias_to_id}, key=len, reverse=True)
        self.surface_by_id: dict[str, list[str]] = defaultdict(list)
        for k, aid in self.alias_to_id.items():
            self.surface_by_id[aid].append(surface.get(k, k))
        heads = sorted({re.split(r"[\s\-]+", f)[0] for f in forms if f}, key=len, reverse=True)
        self._head = re.compile(r"(?<![\w.])(?:" + "|".join(re.escape(h) for h in heads) + ")") if heads else None
        self._full = re.compile("(" + "|".join(_pattern(f) for f in forms) + r")(?!\w|[.\-]\d)") if forms else None

    def aliases_for(self, agent_id: str) -> list[str]:
        """Surface forms (full name + short forms) that count as naming this agent."""
        return sorted(set(self.surface_by_id.get(agent_id, [])), key=lambda a: (-len(a), a))

    def find(self, text: str) -> list[str]:
        """Agent ids named in ``text``, one entry per occurrence."""
        if not text or self._head is None or self._full is None:
            return []
        low = text.lower()
        out: list[str] = []
        end = -1
        for h in self._head.finditer(low):
            if h.start() < end:
                continue
            m = self._full.match(low, h.start())
            if m:
                end = m.end()
                aid = self.alias_to_id.get(norm(m.group(1)))
                if aid:
                    out.append(aid)
        return out


def lab_for(model_string: str | None, name: str = "") -> str:
    m = (model_string or "").lower()
    n = name.lower()
    if m.startswith("tinker://"):
        return "Moonshot AI (fine-tuned via Tinker)" if "kimi" in m else "fine-tuned (Tinker)"
    rules = [
        (("claude",), "Anthropic"),
        (("gpt", "o1", "o3", "o4"), "OpenAI"),
        (("gemini",), "Google DeepMind"),
        (("grok",), "xAI"),
        (("deepseek",), "DeepSeek"),
        (("kimi",), "Moonshot AI"),
        (("glm", "z-ai/"), "Zhipu AI (Z.ai)"),
        (("meta/", "muse", "llama"), "Meta"),
    ]
    for keys, lab in rules:
        if any(m.startswith(k) or f"/{k}" in m or f"::{k}" in m or (k.endswith("/") and k in m) for k in keys):
            return lab
    for keys, lab in rules:
        if any(k.rstrip("/") in n for k in keys):
            return lab
    return "unknown"
