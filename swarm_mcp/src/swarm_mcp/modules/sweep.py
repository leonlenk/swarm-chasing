"""Rubric sweeps: apply one yes/no rubric to many event records with an LLM, then hand-label a sample for precision."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from swarm_mcp import llm
from swarm_mcp import sweep as engine
from swarm_mcp.toolkit import ToolInputError

NAME = "sweep"
DESCRIPTION = (
    "LLM rubric sweeps over event records: estimate cost, run a capped yes/no/unclear sweep that cites event ids, "
    "then sample, hand-label and report precision with a 95% CI. Needs ANTHROPIC_API_KEY for real runs."
)

Rubric = Annotated[
    str,
    Field(
        description="The yes/no question applied to each record, e.g. 'Does the agent claim to have finished a task "
        "it did not actually finish?'. Say what counts as yes."
    ),
]
EventIds = Annotated[
    list[str] | None,
    Field(description="Event ids to evaluate, exactly as returned by other tools (see core_event_sources)."),
]
Filters = Annotated[
    dict[str, Any] | None,
    Field(
        description="Instead of event_ids: a filter object for a registered record provider (see `provider`). "
        "Only available when a module has registered one."
    ),
]
Provider = Annotated[str | None, Field(description="Name of the registered record provider that interprets `filters`.")]
SweepId = Annotated[str, Field(description="A sweep id as returned by sweep_run or sweep_list.")]


def _prices(ctx) -> dict[str, list[float]] | None:
    """``[llm] prices`` from swarm.toml, merged over the built-in table by the engine."""
    table = ctx.config.llm_prices
    return {k: [v[0], v[1]] for k, v in table.items()} if table else None


def register(mcp, ctx) -> None:
    config = ctx.config
    max_chars = engine.DEFAULT_RECORD_CHARS
    concurrency = config.llm_concurrency

    def directory() -> Path:
        return engine.sweeps_dir(config)

    def gather(event_ids: list[str] | None, filters: dict[str, Any] | None, provider: str | None, limit: int):
        """Records for a sweep, plus resolution errors and notes."""
        if event_ids and filters is not None:
            raise ToolInputError("Pass either event_ids or filters, not both.")
        if event_ids:
            if len(event_ids) > engine.MAX_CAP * 4:
                raise ToolInputError(f"At most {engine.MAX_CAP * 4} event_ids per call (got {len(event_ids)}).")
            records, errors = engine.resolve_event_ids(ctx.registry.events, event_ids, max_chars)
            return records, errors, []
        if filters is not None:
            table = engine.providers(ctx.registry)
            if not table:
                raise ToolInputError(
                    "No record provider is registered on this server, so `filters` cannot be used yet. "
                    "Pass event_ids (from search/timeline tools) instead."
                )
            name = provider or (next(iter(table)) if len(table) == 1 else None)
            if name not in table:
                raise ToolInputError(
                    f"Unknown or missing provider {provider!r}. Registered: {', '.join(sorted(table))}."
                )
            records = list(table[name].iter_records(filters, limit + 1))
            return records, [], [f"records from provider {name!r}"]
        raise ToolInputError("Pass event_ids (or filters for a registered provider).")

    @ctx.tool()
    def estimate(
        rubric: Rubric,
        event_ids: EventIds = None,
        filters: Filters = None,
        provider: Provider = None,
        cap: Annotated[int, Field(description="Records that would be sent (default 50).", ge=1)] = engine.DEFAULT_CAP,
        model: Annotated[str | None, Field(description="Model to price (default: the configured one).")] = None,
    ) -> dict[str, Any]:
        """Estimate the tokens and USD cost of sweeping these records (chars/4 input tokens, a fixed output
        allowance per record, times a price table). Makes no model calls."""
        records, errors, notes = gather(event_ids, filters, provider, cap)
        out = engine.estimate(rubric, records[:cap], model=model or llm.configured_model(config), prices=_prices(ctx))
        if len(records) > cap:
            notes.append(f"cap {cap} applied: only the first {cap} of {len(records)} records are priced")
        out["unresolved"] = errors
        out["notes"] = notes + out["notes"]
        return out

    @ctx.tool(read_only=False)
    def run(
        rubric: Rubric,
        event_ids: EventIds = None,
        cap: Annotated[
            int, Field(description=f"Max records sent to the model (default 50, max {engine.MAX_CAP}).", ge=1)
        ] = engine.DEFAULT_CAP,
        dry_run: Annotated[
            bool, Field(description="Only estimate and preview the first prompt; no model calls, nothing written.")
        ] = False,
        filters: Filters = None,
        provider: Provider = None,
    ) -> dict[str, Any]:
        """Apply a yes/no rubric to each record with an LLM and store the verdicts.

        Each record is sent as delimited untrusted data; the model returns verdict (yes/no/unclear),
        confidence and a short rationale. Every verdict cites its event_id; expand any of them with
        core_get_event. Results are saved under a sweep_id (sweep_get, sweep_sample, sweep_precision).
        Real runs need ANTHROPIC_API_KEY on the server; dry_run works without it. Rationales are model
        output about untrusted text: verify before relying on them.
        """
        if cap > engine.MAX_CAP:
            raise ToolInputError(f"cap {cap} is above the maximum of {engine.MAX_CAP}; split the sweep.")
        client = None
        if not dry_run:
            try:
                client = llm.get_client(config)  # before any work: no key, nothing happens
            except llm.LLMUnavailable as e:
                raise ToolInputError(str(e)) from None
        records, errors, notes = gather(event_ids, filters, provider, cap)
        if not records:
            raise ToolInputError(
                "None of the event_ids resolved, so nothing was swept. Errors: "
                + "; ".join(f"{e['event_id']}: {e['error']}" for e in errors[:5])
            )
        out = engine.run(
            rubric,
            records,
            client,
            cap=cap,
            dry_run=dry_run,
            directory=directory(),
            model=llm.configured_model(config),
            prices=_prices(ctx),
            concurrency=concurrency,
            meta={"source_tool": "sweep_run", "unresolved": len(errors), "provider": provider if filters else None},
        )
        out["unresolved"] = errors
        out["notes"] = notes + out.get("notes", [])
        if errors:
            out["notes"].append(f"{len(errors)} event id(s) could not be resolved and were skipped (see unresolved)")
        return out

    @ctx.tool(name="list")
    def list_sweeps() -> dict[str, Any]:
        """List saved sweeps, newest first: id, rubric, model, verdict counts and how many are hand-labeled."""
        sweeps = engine.list_sweeps(directory())
        return {"directory": str(directory()), "count": len(sweeps), "sweeps": sweeps}

    @ctx.tool()
    def get(
        sweep_id: SweepId,
        verdict: Annotated[
            Literal["yes", "no", "unclear", "error"] | None, Field(description="Only verdicts of this kind.")
        ] = None,
        limit: Annotated[int | None, Field(description="Verdicts per page (default 20, max 200).")] = None,
        offset: Annotated[int, Field(ge=0, description="Skip this many verdicts (paging).")] = 0,
    ) -> dict[str, Any]:
        """One sweep: its rubric, model, counts, token use and cost, plus a page of verdicts (each citing an
        event_id)."""
        limit, note = ctx.limit(limit)
        s = engine.load(sweep_id, directory())
        rows = s["verdicts"]
        if verdict == "error":
            rows = [r for r in rows if r.get("error")]
        elif verdict:
            rows = [r for r in rows if r.get("verdict") == verdict and not r.get("error")]
        page = rows[offset : offset + limit]
        summary = s["summary"] or engine._summarize(s["verdicts"], _prices(ctx))
        meta = {k: s["meta"].get(k) for k in ("created", "rubric", "model", "cap", "n_input", "n_sent")}
        return {
            "sweep_id": sweep_id,
            **meta,
            "finished": s["summary"] is not None,
            "counts": summary["counts"],
            "tokens": summary["tokens"],
            "cost_usd": summary["cost_usd"],
            "total_matches": len(rows),
            "returned": len(page),
            "has_more": offset + len(page) < len(rows),
            "verdicts": [engine._public(r) for r in page],
            "notes": [n for n in [note] if n],
        }

    @ctx.tool(read_only=False)
    def sample(
        sweep_id: SweepId,
        n: Annotated[int, Field(description="How many verdicts to draw (default 20).", ge=1, le=500)] = 20,
        seed: Annotated[int, Field(description="Random seed, so the sample is reproducible.")] = 0,
        verdicts: Annotated[
            list[Literal["yes", "no", "unclear"]] | None,
            Field(description="Which verdicts to sample from (default ['yes'], what precision needs)."),
        ] = None,
    ) -> dict[str, Any]:
        """Draw a seeded random sample of verdicts to hand-label and write it to the sweep's label file.
        Then read each item with core_get_event and record your judgement with sweep_label."""
        out = engine.sample_for_labeling(sweep_id, n, seed, directory(), tuple(verdicts or ("yes",)))
        out["next"] = "For each item: core_get_event(event_id), decide if the verdict is right, then sweep_label."
        return out

    @ctx.tool(read_only=False)
    def label(
        sweep_id: SweepId,
        event_id: Annotated[str, Field(description="The event_id of a verdict in this sweep.")],
        correct: Annotated[bool, Field(description="True if the sweep's verdict for this record is right.")],
        note: Annotated[str | None, Field(description="Optional short reason.")] = None,
    ) -> dict[str, Any]:
        """Record whether the sweep's verdict for one event was correct. The latest label for an event wins."""
        return engine.label(sweep_id, event_id, correct, directory(), note=note)

    @ctx.tool()
    def precision(sweep_id: SweepId) -> dict[str, Any]:
        """Precision of the sweep's 'yes' verdicts from hand labels, with a Wilson 95% confidence interval, the
        number of labels it rests on, and the implied number of true positives among all 'yes' verdicts."""
        return engine.precision(sweep_id, directory())
