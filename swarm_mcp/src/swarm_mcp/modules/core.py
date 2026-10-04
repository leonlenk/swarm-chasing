"""Server overview (modules, sources, findings health, config) and retrieval of any record by its id."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from swarm_mcp.info import server_info
from swarm_mcp.toolkit import ToolInputError

NAME = "core"
DESCRIPTION = (
    "Server overview (core_info: modules, sources with counts and date ranges, findings health, config) and "
    "retrieval (core_get: any '<source>:<kind>:<id>' id, or a batch, with optional surrounding context)."
)
MAX_BATCH = 50


def register(mcp, ctx) -> None:
    @ctx.tool()
    def info() -> dict[str, Any]:
        """Start here. What this server has loaded and what is in it:
        - modules: loaded ones with their tools/prompts, skipped ones with the reason;
        - sources: every ingested source with its adapter, row counts per table, message/action date ranges,
          channels, action kinds, the id kinds present (with id format) and ingest_meta.notes (its blind spots);
        - findings: whether every recorded finding still cites ids that resolve;
        - config: data dir, store, findings/sweeps dirs, limits, privacy and LLM settings (no secrets)."""
        return server_info(ctx.config, ctx.registry)

    def one(record_id: str, before: int, after: int, max_chars: int) -> dict[str, Any]:
        api = getattr(ctx.registry, "store_api", None)
        if not api:
            raise ToolInputError(
                f"Cannot resolve {record_id!r}: the SwarmScope store is not loaded (see core_info); "
                "add a dataset with `swarm-mcp add <path>`."
            )
        return api["get_record"](record_id, max_chars=max_chars, before=before, after=after)

    @ctx.tool()
    def get(
        ids: Annotated[
            str | list[str],
            Field(
                description="One id, or a list of up to 50, exactly as returned by another tool "
                "(e.g. 'village:msg:<uuid>', 'village:agent:<uuid>', 'rpg-game:period:pr-109')."
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
        """Resolve any id to its full record: a message (time, channel, author, named recipients, content), an
        action (kind, agent, content), an agent (aliases, counts), a period (label, start/end, member records) or
        an artifact (a file or page, with the records that created, changed or mentioned it). Messages and
        actions also list the artifacts they touched; `before`/`after` add neighbouring messages in the same
        channel or the same agent's adjacent actions (under `neighbors`). One id returns that record; a list
        returns {results: [...], errors: [...]}, where unresolvable ids are listed instead of failing the call."""
        cap = max_chars or ctx.config.max_text
        if isinstance(ids, str):
            return one(ids, before, after, cap)
        if not ids:
            raise ToolInputError("ids must not be empty")
        if len(ids) > MAX_BATCH:
            raise ToolInputError(f"At most {MAX_BATCH} ids per call (got {len(ids)}); split the request.")
        results, errors = [], []
        for e in ids:
            try:
                results.append(one(e, before, after, cap))
            except ToolInputError as err:
                errors.append({"id": e, "error": str(err)})
        return {"requested": len(ids), "returned": len(results), "results": results, "errors": errors}
