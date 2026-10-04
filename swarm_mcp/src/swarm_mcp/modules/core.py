"""Server overview (modules, sources, findings health, config) and retrieval of any record by its id."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from swarm_mcp.events import EventNotFound
from swarm_mcp.info import server_info
from swarm_mcp.toolkit import ToolInputError

NAME = "core"
DESCRIPTION = (
    "Server overview (core_info: modules, sources with counts and date ranges, findings health, config) and "
    "shared retrieval (core_get: any '<source>:<kind>:<id>' id, or a batch, with optional surrounding context)."
)
MAX_BATCH = 50


def register(mcp, ctx) -> None:
    @ctx.tool()
    def info() -> dict[str, Any]:
        """Start here. What this server has loaded and what is in it:
        - modules: loaded ones with their tools/prompts, skipped ones with the reason;
        - sources: every record source with its kinds and id format, plus (for the SwarmScope store) row counts,
          message/action date ranges, channels and action kinds;
        - findings: whether every recorded finding still cites ids that resolve;
        - config: data dir, store, findings/sweeps dirs, limits, privacy and LLM settings (no secrets)."""
        return server_info(ctx.config, ctx.registry)

    def one(event_id: str, before: int, after: int, max_chars: int) -> dict[str, Any]:
        eid, src = ctx.registry.events.lookup(event_id)
        try:
            out = src.resolve(eid.kind, eid.local_id, before=before, after=after, max_chars=max_chars)
        except EventNotFound:
            raise ToolInputError(f"No {eid.kind!r} record with id {eid.local_id!r} in source {eid.source!r}.") from None
        out.setdefault("before", [])
        out.setdefault("after", [])
        return out

    @ctx.tool()
    def get(
        ids: Annotated[
            str | list[str],
            Field(
                description="One id, or a list of up to 50, exactly as returned by another tool "
                "(e.g. 'village:chat:<uuid>', 'village:agent:<uuid>', 'git:pr:<repo>#12')."
            ),
        ],
        before: Annotated[
            int,
            Field(description="Neighbouring records before each one (e.g. earlier messages in the room).", ge=0, le=50),
        ] = 0,
        after: Annotated[int, Field(description="Neighbouring records after each one.", ge=0, le=50)] = 0,
        max_chars: Annotated[
            int | None,
            Field(description="Truncate each record's text to this many characters (default 500).", ge=80, le=50000),
        ] = None,
    ) -> dict[str, Any]:
        """Retrieve the original record behind any id, optionally with surrounding context (`before`/`after`;
        `context` says what the neighbours are). One id returns {event, before, after, context}; a list returns
        {results: [...], errors: [...]}, where ids that cannot be resolved are listed under errors instead of
        failing the call. Store records carry extra fields (recipients, reply_to, period window, agent counts)."""
        cap = max_chars or ctx.config.max_text
        if isinstance(ids, str):
            out = one(ids, before, after, cap)
            notes = list(out.pop("notes", []) or [])
            cut = sum(1 for r in [out["event"], *out["before"], *out["after"]] if r.get("truncated"))
            if cut:
                notes.append(f"{cut} record(s) truncated; raise max_chars for full text")
            out["notes"] = notes
            return out
        if not ids:
            raise ToolInputError("ids must not be empty")
        if len(ids) > MAX_BATCH:
            raise ToolInputError(f"At most {MAX_BATCH} ids per call (got {len(ids)}); split the request.")
        results, errors = [], []
        for e in ids:
            try:
                r = one(e, before, after, cap)
                r.pop("notes", None)
                results.append(r if (before or after) else {"event": r["event"]})
            except ToolInputError as err:
                errors.append({"id": e, "error": str(err)})
        return {"requested": len(ids), "returned": len(results), "results": results, "errors": errors}
