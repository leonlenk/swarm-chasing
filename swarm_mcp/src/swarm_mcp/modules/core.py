"""Server introspection (modules, config) and shared retrieval of any event by its event_id."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from swarm_mcp import __version__
from swarm_mcp.events import EventNotFound
from swarm_mcp.toolkit import ToolInputError

NAME = "core"
DESCRIPTION = (
    "Server introspection (loaded/skipped modules, config) and shared retrieval: core_get_event expands any "
    "event_id returned by another tool into the original record plus its surrounding context."
)


def register(mcp, ctx) -> None:
    @ctx.tool()
    def list_modules(
        include_skipped: Annotated[
            bool, Field(description="Also list modules that were skipped, with reasons.")
        ] = True,
    ) -> dict[str, Any]:
        """List the server's modules: loaded ones with their tool names, skipped ones with the reason they did not load."""
        reg = ctx.registry
        out: dict[str, Any] = {"loaded": [r.as_dict() for r in reg.loaded]}
        if include_skipped:
            out["skipped"] = [r.as_dict() for r in reg.skipped]
        if reg.notes:
            out["notes"] = reg.notes
        return out

    @ctx.tool()
    def server_info() -> dict[str, Any]:
        """Server version, resolved data directory, effective configuration and cache status."""
        reg = ctx.registry
        return {
            "name": "swarm",
            "version": __version__,
            "data_dir": str(ctx.config.data_dir),
            "config": ctx.config.public(),
            "modules": {"loaded": [r.name for r in reg.loaded], "skipped": [r.name for r in reg.skipped]},
            "cache": ctx.cache.stats(),
        }

    # ------------------------------------------------------------------ shared retrieval

    def resolve(event_id: str, before: int, after: int, max_chars: int) -> dict[str, Any]:
        eid, src = ctx.registry.events.lookup(event_id)
        try:
            return src.resolve(eid.kind, eid.local_id, before=before, after=after, max_chars=max_chars)
        except EventNotFound:
            raise ToolInputError(f"No {eid.kind!r} record with id {eid.local_id!r} in source {eid.source!r}.") from None

    @ctx.tool()
    def event_sources() -> dict[str, Any]:
        """List the event sources loaded on this server: the kinds of record each can return and the event_id
        format. Every event_id is '<source>:<kind>:<id>' and can be expanded with core_get_event."""
        srcs = [s.as_dict() for s in sorted(ctx.registry.events.by_name.values(), key=lambda s: s.name)]
        return {"id_format": "<source>:<kind>:<id>", "sources": srcs, "count": len(srcs)}

    @ctx.tool()
    def get_event(
        event_id: Annotated[
            str, Field(description="An event_id exactly as returned by another tool, e.g. 'village:chat:<uuid>'.")
        ],
        before: Annotated[
            int, Field(description="How many neighbouring records before it to include.", ge=0, le=50)
        ] = 3,
        after: Annotated[int, Field(description="How many neighbouring records after it to include.", ge=0, le=50)] = 3,
        max_chars: Annotated[
            int | None,
            Field(description="Truncate each record's text to this many characters (default 1000).", ge=80, le=50000),
        ] = None,
    ) -> dict[str, Any]:
        """Retrieve the original record for any event_id, plus surrounding context (e.g. the previous and next
        messages in the same room). Works for every source listed by core_event_sources. `context` says what
        the neighbouring records are."""
        out = resolve(event_id, before, after, max_chars or ctx.config.max_text)
        out.setdefault("before", [])
        out.setdefault("after", [])
        notes = list(out.pop("notes", []) or [])
        cut = sum(1 for r in [out["event"], *out["before"], *out["after"]] if r.get("truncated"))
        if cut:
            notes.append(f"{cut} record(s) truncated; raise max_chars for full text")
        out["notes"] = notes
        return out

    @ctx.tool()
    def get_events(
        event_ids: Annotated[list[str], Field(description="Up to 50 event_ids, exactly as returned by other tools.")],
        max_chars: Annotated[
            int | None,
            Field(description="Truncate each record's text to this many characters (default 1000).", ge=80, le=50000),
        ] = None,
    ) -> dict[str, Any]:
        """Retrieve several records by event_id in one call, without surrounding context. Ids that cannot be
        resolved are listed under `errors` instead of failing the whole call."""
        if not event_ids:
            raise ToolInputError("event_ids must not be empty")
        if len(event_ids) > 50:
            raise ToolInputError(f"At most 50 event_ids per call (got {len(event_ids)}); split the request.")
        events, errors = [], []
        for e in event_ids:
            try:
                events.append(resolve(e, 0, 0, max_chars or ctx.config.max_text)["event"])
            except ToolInputError as err:
                errors.append({"event_id": e, "error": str(err)})
        return {"returned": len(events), "events": events, "errors": errors}
