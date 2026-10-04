"""Evidence IDs: ``{source}:{kind}:{native_id}``.

Examples: ``village:chat:<chat message uuid>``, ``village:agent:<agent uuid>``,
``village:event:<event uuid>`` (actions), ``village:goal:<goal uuid>`` (periods).

Every record row's primary key *is* its evidence id, so ``resolve`` is one
indexed lookup. ``resolve`` raises ``EvidenceError`` with a clear message for
malformed or unknown ids; tools surface that message verbatim.

Sources ingested from a declarative mapping (``scope.adapters.mapped``) keep the
mapping's own kinds (``forum:post:<id>``, ``crew:utterance:<id>``). Their kinds are
recorded in ``sources.meta.kinds``; ``resolve`` finds such ids by primary key in
messages, actions and periods.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from swarm_mcp.scope.db import Store
from swarm_mcp.toolkit import DEFAULT_MAX_CHARS, ToolInputError, untrusted

# kind -> (table, primary key column)
KIND_TABLES: dict[str, tuple[str, str]] = {
    "chat": ("messages", "evidence_id"),
    "agent": ("agents", "agent_id"),
    "event": ("actions", "evidence_id"),
    "goal": ("periods", "evidence_id"),
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
    def table(self) -> str | None:
        """The table for a built-in kind; None for a mapped kind (see ``resolve``)."""
        return KIND_TABLES[self.kind][0] if self.kind in KIND_TABLES else None


def make(source: str, kind: str, native_id: str) -> str:
    """Build an evidence id. ``native_id`` may contain ':'; source and kind may not."""
    if not _PART.match(source or "") or not _PART.match(kind or ""):
        raise ValueError(f"bad evidence id parts: source={source!r} kind={kind!r}")
    if not native_id:
        raise ValueError("native_id must not be empty")
    return f"{source}:{kind}:{native_id}"


# tables that hold records of mapped (non-built-in) kinds, all keyed by evidence_id
RECORD_TABLES = ("messages", "actions", "periods")


def parse(evidence_id: str, *, any_kind: bool = False) -> EvidenceRef:
    """Split an evidence id into (source, kind, native_id), validating the format.

    Unless ``any_kind``, the kind must be one of the built-in ``KIND_TABLES`` kinds."""
    eid = (evidence_id or "").strip()
    parts = eid.split(":", 2)
    if len(parts) != 3 or not all(parts) or not _PART.match(parts[0]) or not _PART.match(parts[1]):
        raise EvidenceError(
            f"Malformed evidence id {evidence_id!r}: expected '{{source}}:{{kind}}:{{native_id}}', "
            "e.g. 'village:chat:<uuid>'. Copy ids exactly from tool results."
        )
    if not any_kind and parts[1] not in KIND_TABLES:
        raise EvidenceError(
            f"Unknown evidence kind {parts[1]!r} in {evidence_id!r}; known kinds: {', '.join(sorted(KIND_TABLES))}"
        )
    return EvidenceRef(*parts)


def source_kinds(store: Store, source: str) -> dict[str, str]:
    """Extra kinds a mapped source declared at ingest (``sources.meta.kinds``): kind -> description."""
    raw = store.scalar("SELECT meta FROM sources WHERE source = ?", [source])
    try:
        meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return {}
    kinds = meta.get("kinds") if isinstance(meta, dict) else None
    return {str(k): str(v) for k, v in kinds.items()} if isinstance(kinds, dict) else {}


def resolve(store: Store, evidence_id: str) -> dict[str, Any]:
    """Return ``{"evidence_id", "table", "record"}`` for an id, or raise EvidenceError."""
    ref = parse(evidence_id, any_kind=True)
    eid = str(ref)
    if ref.kind in KIND_TABLES:
        table, pk = KIND_TABLES[ref.kind]
        row = store.one(f"SELECT * FROM {table} WHERE {pk} = ?", [eid])
        if row is not None:
            return {"evidence_id": eid, "table": table, "record": row}
    extra = source_kinds(store, ref.source)
    if ref.kind not in KIND_TABLES and ref.kind not in extra:
        known = sorted({*KIND_TABLES, *extra})
        raise EvidenceError(
            f"Unknown evidence kind {ref.kind!r} in {evidence_id!r}; known kinds: {', '.join(known)}"
        )
    if extra:  # a mapped source may use any kind name in any record table
        for table in RECORD_TABLES:
            row = store.one(f"SELECT * FROM {table} WHERE evidence_id = ?", [eid])
            if row is not None:
                return {"evidence_id": eid, "table": table, "record": row}
    where = KIND_TABLES[ref.kind][0] if ref.kind in KIND_TABLES else "/".join(RECORD_TABLES)
    raise EvidenceError(
        f"Evidence id {eid!r} does not resolve: no {where} row with that id "
        f"(source {ref.source!r}). Check the id, or re-run ingest if the store is stale."
    )


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
