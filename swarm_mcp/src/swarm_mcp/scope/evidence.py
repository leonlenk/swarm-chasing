"""Evidence IDs: ``{source}:{kind}:{native_id}``.

``source`` is the ingested dataset; ``kind`` is a *schema* kind, the same for every dataset, so ids
behave identically whatever the source:

    msg       messages   (chat lines, wiki revisions, posts...)
    event     actions    (tool calls, commits, session goals, deletions...)
    agent     agents
    period    periods    (time periods and episodes of work: goals, pull requests, runs...)
    artifact  artifacts  (files, pages...)
    goal      periods    (AI Village weekly goals; kept for existing ids)

Examples: ``village:msg:<chat message uuid>``, ``rpg-game:event:<commit sha>``,
``rpg-game:period:pr-109``, ``collusion-wiki:msg:<revision id>``, ``rpg-game:artifact:src/talents.js``.
The dataset-specific type (commit, revision, pull request) is a field of the record, not part of the id.

Every record row's primary key *is* its evidence id, so ``resolve`` is one
indexed lookup. ``resolve`` raises ``EvidenceError`` with a clear message for
malformed or unknown ids; tools surface that message verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from swarm_mcp.scope.db import Store
from swarm_mcp.toolkit import DEFAULT_MAX_CHARS, ToolInputError, untrusted

# kind -> (table, primary key column)
KIND_TABLES: dict[str, tuple[str, str]] = {
    "msg": ("messages", "evidence_id"),
    "agent": ("agents", "agent_id"),
    "event": ("actions", "evidence_id"),
    "period": ("periods", "evidence_id"),
    "goal": ("periods", "evidence_id"),
    "artifact": ("artifacts", "artifact_id"),
}

_PART = re.compile(r"^[A-Za-z0-9_.\-]+$")


class EvidenceError(ToolInputError):
    """An evidence id is malformed or does not resolve to a record."""


@dataclass(frozen=True)
class EvidenceRef:
    source: str
    kind: str
    native_id: str

    def __str__(self) -> str:
        return f"{self.source}:{self.kind}:{self.native_id}"

    @property
    def table(self) -> str:
        return KIND_TABLES[self.kind][0]


def make(source: str, kind: str, native_id: str) -> str:
    """Build an evidence id. ``native_id`` may contain ':'; source and kind may not."""
    if not _PART.match(source or "") or not _PART.match(kind or ""):
        raise ValueError(f"bad evidence id parts: source={source!r} kind={kind!r}")
    if not native_id:
        raise ValueError("native_id must not be empty")
    return f"{source}:{kind}:{native_id}"


def parse(evidence_id: str) -> EvidenceRef:
    """Split an evidence id into (source, kind, native_id), validating the format."""
    eid = (evidence_id or "").strip()
    parts = eid.split(":", 2)
    if len(parts) != 3 or not all(parts) or not _PART.match(parts[0]) or not _PART.match(parts[1]):
        raise EvidenceError(
            f"Malformed evidence id {evidence_id!r}: expected '{{source}}:{{kind}}:{{native_id}}', "
            "e.g. 'village:msg:<uuid>'. Copy ids exactly from tool results."
        )
    if parts[1] not in KIND_TABLES:
        raise EvidenceError(
            f"Unknown evidence kind {parts[1]!r} in {evidence_id!r}; known kinds: {', '.join(sorted(KIND_TABLES))}"
        )
    return EvidenceRef(*parts)


def resolve(store: Store, evidence_id: str) -> dict[str, Any]:
    """Return ``{"evidence_id", "table", "record"}`` for an id, or raise EvidenceError."""
    ref = parse(evidence_id)
    table, pk = KIND_TABLES[ref.kind]
    if not store.has_table(table):
        raise EvidenceError(
            f"Evidence id {str(ref)!r} needs the {table!r} table, which this store predates; re-run ingest."
        )
    row = store.one(f"SELECT * FROM {table} WHERE {pk} = ?", [str(ref)])
    if row is None:
        raise EvidenceError(
            f"Evidence id {str(ref)!r} does not resolve: no {table} row with that id "
            f"(source {ref.source!r}). Check the id, or re-run ingest if the store is stale."
        )
    return {"evidence_id": str(ref), "table": table, "record": row}


def check(store: Store, evidence_ids: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    """Validate many ids. Returns (resolved_ids, {bad_id: reason})."""
    ok: list[str] = []
    bad: dict[str, str] = {}
    for eid in evidence_ids:
        try:
            ok.append(resolve(store, eid)["evidence_id"])
        except EvidenceError as e:
            bad[str(eid)] = str(e)
    return ok, bad


def snippet(
    text: str | None,
    max_chars: int = DEFAULT_MAX_CHARS,
    scrub: Callable[[str], str] | None = None,
    focus: re.Pattern[str] | None = None,
) -> dict[str, Any]:
    """A bounded, masked, delimited view of record text: ``{"content", "untrusted": True, ...}``."""
    return untrusted(text, scrub, max_chars, focus)
