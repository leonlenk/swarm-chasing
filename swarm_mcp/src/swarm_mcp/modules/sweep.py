"""Rubric sweeps: apply one yes/no rubric to many event records with an LLM, then hand-label a sample for precision.

Three tools: ``sweep_run`` (a dry run, the default, returns the cost estimate and a prompt preview;
``dry_run=false`` executes), ``sweep_get`` (one sweep, or the list) and ``sweep_review`` (items to
hand-label plus the current precision; with ``labels`` it records them and returns the updated precision).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from swarm_mcp import llm
from swarm_mcp import sweep as engine
from swarm_mcp.scope.records import from_store_record
from swarm_mcp.toolkit import ToolInputError

NAME = "sweep"
DESCRIPTION = (
    "LLM rubric sweeps over event records: estimate (dry run), run a capped yes/no/unclear sweep that cites event "
    "ids, then hand-label a sample and get precision with a 95% CI. Real runs need ANTHROPIC_API_KEY."
)
DEFAULT_PROVIDER = "store"

Rubric = Annotated[
    str,
    Field(
        description="The yes/no question applied to each record, e.g. 'Does the agent claim to have finished a task "
        "it did not actually finish?'. Say what counts as yes."
    ),
]
SweepId = Annotated[str, Field(description="A sweep id as returned by sweep_run or sweep_get.")]


class Label(BaseModel):
    event_id: str = Field(description="The event_id of a verdict in this sweep.")
    correct: bool = Field(description="True if the sweep's verdict for this record is right.")
    note: str | None = Field(default=None, description="Optional short reason.")


def _prices(ctx) -> dict[str, list[float]] | None:
    """``[llm] prices`` from swarm.toml, merged over the built-in table by the engine."""
    table = ctx.config.llm_prices
    return {k: [v[0], v[1]] for k, v in table.items()} if table else None


def register(mcp, ctx) -> None:
    config = ctx.config
    max_chars = engine.DEFAULT_RECORD_CHARS

    def directory() -> Path:
        return engine.sweeps_dir(config)

    def resolver() -> engine.Resolver:
        """Ids resolve through the store's get_record (the same path as core_get), as standard records."""
        api = getattr(ctx.registry, "store_api", None)
        if not api or "get_record" not in api:
            raise ToolInputError(
                "Cannot resolve ids: the SwarmScope store is not loaded (see core_info); "
                "add a dataset with `swarm-mcp add <path>`."
            )
        get_record = api["get_record"]
        return lambda eid: from_store_record(get_record(eid, max_chars=max_chars))

    def gather(ids: list[str] | None, filters: dict[str, Any] | None, limit: int):
        """Records for a sweep, plus resolution errors, notes, the provider used, how many records matched
        in all and the note to show if the cap leaves some unsent."""
        if ids and filters is not None:
            raise ToolInputError("Pass either ids or filters, not both.")
        if ids:
            if len(ids) > engine.MAX_CAP * 4:
                raise ToolInputError(f"At most {engine.MAX_CAP * 4} ids per call (got {len(ids)}).")
            records, errors = engine.resolve_ids(resolver(), ids, max_chars)
            n = min(limit, len(records))
            cap_note = (
                f"{n} of {len(records):,} resolved ids sent (the first {n} in the order given; "
                "pass the rest in another call or raise cap)"
            )
            return records, errors, [], None, len(records), cap_note
        if filters is not None:
            table = engine.providers(ctx.registry)
            if not table:
                raise ToolInputError(
                    "No record provider is registered on this server (the SwarmScope store is not loaded), so "
                    "`filters` cannot be used. Pass ids (from scope_search or other tools) instead."
                )
            name = DEFAULT_PROVIDER if DEFAULT_PROVIDER in table else sorted(table)[0]
            provider = table[name]
            count = getattr(provider, "count", None)
            total = count(filters) if callable(count) else None
            # without a count, one record past the cap shows that some were left out
            records = list(provider.iter_records(filters, limit if total is not None else limit + 1))
            n = min(limit, len(records))
            if total is None:
                cap_note = (
                    f"{n} of more than {n} matching records sent (the first {n}; narrow the filters or raise cap)"
                )
            else:
                cap_note = f"{n} of {total:,} matching records sent (the oldest {n}; narrow since/until or raise cap)"
            return records, [], [f"records from provider {name!r}"], name, total, cap_note
        raise ToolInputError(
            "Pass ids (from scope_search or other tools), or filters such as "
            "{'source': 'village', 'channel': 'general', 'since': '2026-01-05', 'until': '2026-01-12', "
            "'author': 'Opus 4.5', 'kind': 'msg', 'query': 'deadline'}."
        )

    @ctx.tool(read_only=False)
    def run(
        rubric: Rubric,
        ids: Annotated[
            list[str] | None,
            Field(description="Record ids to evaluate, exactly as returned by other tools."),
        ] = None,
        filters: Annotated[
            dict[str, Any] | None,
            Field(
                description="Instead of ids: select records from the SwarmScope store with keys source, kind "
                "('msg' or 'event'), type (the dataset's msg_type / action kind), channel, author, since, until, "
                "query (all optional; oldest first)."
            ),
        ] = None,
        dry_run: Annotated[
            bool,
            Field(
                description="True (default): estimate tokens and USD cost and preview the first prompt; no model "
                "calls, nothing written. False: run the sweep (needs ANTHROPIC_API_KEY)."
            ),
        ] = True,
        cap: Annotated[
            int, Field(description=f"Max records sent to the model (default 50, max {engine.MAX_CAP}).", ge=1)
        ] = engine.DEFAULT_CAP,
    ) -> dict[str, Any]:
        """Apply a yes/no rubric to each record with an LLM.

        Start with the default dry run: it returns the estimate (records, tokens, USD) and the first prompt.
        Then call again with dry_run=false to execute: each record is sent as delimited untrusted data and the
        model returns verdict (yes/no/unclear), confidence and a short rationale; every verdict cites its
        event_id (expand it with core_get). Results are saved under a sweep_id (sweep_get, sweep_review).
        Rationales are model output about untrusted text: verify before relying on them.
        """
        if cap > engine.MAX_CAP:
            raise ToolInputError(f"cap {cap} is above the maximum of {engine.MAX_CAP}; split the sweep.")
        client = None
        if not dry_run:
            try:
                client = llm.get_client(config)  # before any work: no key, nothing happens
            except llm.LLMUnavailable as e:
                raise ToolInputError(str(e)) from None
        records, errors, notes, provider, total, cap_note = gather(ids, filters, cap)
        if not records:
            if errors:
                raise ToolInputError(
                    "None of the ids resolved, so nothing was swept. Errors: "
                    + "; ".join(f"{e['event_id']}: {e['error']}" for e in errors[:5])
                )
            raise ToolInputError("No records match these filters, so nothing was swept.")
        model = llm.configured_model(config)
        if dry_run:
            out = engine.run(rubric, records, None, cap=cap, dry_run=True, model=model, prices=_prices(ctx),
                             total=total, cap_note=cap_note)  # fmt: skip
            out["notes"] = notes + out["notes"] + ["call again with dry_run=false to run it"]
        else:
            out = engine.run(
                rubric,
                records,
                client,
                cap=cap,
                directory=directory(),
                model=model,
                prices=_prices(ctx),
                concurrency=config.llm_concurrency,
                meta={"source_tool": "sweep_run", "unresolved": len(errors), "provider": provider},
                total=total,
                cap_note=cap_note,
            )
            out["notes"] = notes + out.get("notes", [])
        out["unresolved"] = errors
        if errors:
            out["notes"].append(f"{len(errors)} id(s) could not be resolved and were skipped (see unresolved)")
        return out

    @ctx.tool()
    def get(
        sweep_id: Annotated[str | None, Field(description="A sweep id; omit to list all saved sweeps.")] = None,
        verdict: Annotated[
            Literal["yes", "no", "unclear", "error"] | None, Field(description="Only verdicts of this kind.")
        ] = None,
        limit: Annotated[int | None, Field(description="Verdicts per page (default 20, max 200).")] = None,
        offset: Annotated[int, Field(ge=0, description="Skip this many verdicts (paging).")] = 0,
    ) -> dict[str, Any]:
        """Without sweep_id: the saved sweeps, newest first (id, rubric, model, verdict counts, labels so far).
        With sweep_id: its rubric, model, counts, token use and cost, plus a page of verdicts (each citing an
        event_id)."""
        if not sweep_id:
            sweeps = engine.list_sweeps(directory())
            return {"directory": str(directory()), "count": len(sweeps), "sweeps": sweeps}
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
    def review(
        sweep_id: SweepId,
        labels: Annotated[
            list[Label] | None,
            Field(
                description="Your judgements, [{event_id, correct, note?}], one per verdict you checked. Omit to get "
                "the items to label."
            ),
        ] = None,
        n: Annotated[int, Field(description="How many items to label (default 20).", ge=1, le=500)] = 20,
        seed: Annotated[int, Field(description="Random seed for drawing new items (reproducible sample).")] = 0,
        verdicts: Annotated[
            list[Literal["yes", "no", "unclear"]] | None,
            Field(description="Which verdicts to draw from (default ['yes'], what precision needs)."),
        ] = None,
    ) -> dict[str, Any]:
        """Check a sweep by hand and measure its precision.

        Without labels: up to n items to label (earlier-sampled but unlabeled items first, then a seeded random
        draw of new ones, recorded in the sweep's label file) plus the current precision. Read each item with
        core_get, decide whether the verdict is right, then call again with labels=[{event_id, correct}].
        With labels: records them (the latest label for an event wins) and returns the updated precision of the
        'yes' verdicts with a Wilson 95% CI."""
        d = directory()
        if labels:
            engine.check_labels(sweep_id, [lb.event_id for lb in labels], d)  # all or nothing
            recorded = [engine.label(sweep_id, lb.event_id, lb.correct, d, note=lb.note) for lb in labels]
            return {
                "sweep_id": sweep_id,
                "recorded": len(recorded),
                "labels": recorded,
                "precision": engine.precision(sweep_id, d),
            }
        pending = engine.pending_labels(sweep_id, d)
        items = pending[:n]
        drawn = None
        if len(items) < n:
            drawn = engine.sample_for_labeling(sweep_id, n - len(items), seed, d, tuple(verdicts or ("yes",)))
            items += drawn["items"]
        out: dict[str, Any] = {
            "sweep_id": sweep_id,
            "to_label": items,
            "pending_from_earlier": min(len(pending), n),
            "newly_drawn": drawn["sampled"] if drawn else 0,
            "precision": engine.precision(sweep_id, d),
            "next": "For each item: core_get(event_id), decide if the verdict is right, then "
            "sweep_review(sweep_id, labels=[{event_id, correct}, ...]).",
        }
        if drawn and drawn.get("notes"):
            out["notes"] = drawn["notes"]
        return out
