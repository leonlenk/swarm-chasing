"""Findings: record evidence-backed claims, list them, and draw spot-check samples (``findings_list(sample=n)``).

A finding is a claim plus the evidence ids (copied from other tools' results)
that support it. ``findings_record`` refuses any finding whose ids do not all
resolve in the SwarmScope store. Findings live in
``<findings dir>/findings.jsonl`` (``[data] findings``; mirrored into the DuckDB
``findings`` table); the Stop hook ``hooks/require_evidence.py`` re-checks them.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from swarm_mcp.scope import findings as lib
from swarm_mcp.toolkit import HARD_MAX_CHARS, MIN_MAX_CHARS, ResponseBudget

NAME = "findings"
DESCRIPTION = (
    "Record claims that cite evidence ids (rejected unless every id resolves), list them, "
    "and draw a deterministic random sample of findings (with their cited evidence) for human spot-checks."
)

SNIPPET_CHARS = 200  # cap for the evidence snippets shown next to a finding
CLAIM_CHARS = 2000  # claims are investigator-written; show them (almost) whole


def requires(ctx) -> list[str]:
    if not ctx.store_path.exists():
        return [f"SwarmScope store not found at {ctx.store_path}; run `swarm-mcp add data/ai-village`"]
    return []


def _wrap_evidence(ctx, view: dict[str, Any], max_chars: int | None) -> dict[str, Any]:
    """Replace the raw ``text`` of an ``evidence_view`` with an untrusted snippet."""
    out = {k: v for k, v in view.items() if k != "text"}
    if "text" in view:
        out["content"] = ctx.untrusted(view["text"], max_chars)
    return out


def _wrap_finding(ctx, f: dict[str, Any]) -> dict[str, Any]:
    out = dict(f)
    out["claim"] = ctx.untrusted(str(f.get("claim") or ""), CLAIM_CHARS)
    return out


def register(mcp, ctx) -> None:
    findings_dir = ctx.config.findings_path

    @ctx.tool(read_only=False)
    def findings_record(
        claim: Annotated[str, Field(description="The claim, in one or two plain sentences.")],
        evidence_ids: Annotated[
            list[str],
            Field(
                description="Evidence ids supporting the claim, copied exactly from tool results "
                "(e.g. 'village:msg:<uuid>', 'village:agent:<uuid>', 'village:event:<uuid>', "
                "'village:goal:<uuid>'). Every id must resolve or nothing is recorded."
            ),
        ],
        confidence: Annotated[
            Literal["low", "medium", "high"], Field(description="How strongly the evidence supports the claim.")
        ] = "medium",
        author: Annotated[str, Field(description="Who is recording the finding.")] = "claude",
    ) -> dict[str, Any]:
        """Record a finding: a claim plus the evidence ids that support it.

        Every evidence id is resolved against the SwarmScope store first; if any id is
        malformed or unknown, nothing is written and the error lists each bad id. On
        success the finding is appended to findings.jsonl (and mirrored into DuckDB) and
        the cited records are returned as short untrusted snippets so you can check that
        they say what the claim says.
        """
        out = lib.record_finding(
            findings_dir,
            ctx.store_path,
            claim=claim,
            evidence_ids=evidence_ids,
            confidence=confidence,
            author=author,
        )
        return {
            "finding": _wrap_finding(ctx, out["finding"]),
            "evidence": [_wrap_evidence(ctx, v, SNIPPET_CHARS) for v in out["evidence"]],
            "findings_file": out["findings_file"],
            "db_synced": out["db_synced"],
            "notes": out["notes"],
        }

    @ctx.tool()
    def findings_list(
        status: Annotated[
            Literal["open", "confirmed", "rejected", "retracted"] | None,
            Field(description="Only findings with this status (default: all)."),
        ] = None,
        limit: Annotated[int, Field(description="Max findings to return (default 50, max 200).")] = 50,
        sample: Annotated[
            int | None,
            Field(
                description="Instead of the newest findings, draw this many at random (deterministic for a seed), "
                "each with its cited evidence as short snippets, for a human spot-check of claim vs evidence.",
                ge=1,
                le=200,
            ),
        ] = None,
        seed: Annotated[int, Field(description="Random seed for `sample`; same seed, same sample.")] = 0,
        max_chars: Annotated[
            int | None,
            Field(
                description=f"With sample: max chars per evidence snippet ({MIN_MAX_CHARS}..{HARD_MAX_CHARS}, "
                f"default {SNIPPET_CHARS}).",
                ge=MIN_MAX_CHARS,
                le=HARD_MAX_CHARS,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List recorded findings, newest first, with their evidence ids. With `sample`, return a seeded random
        sample instead, each finding with its cited records (or the error if an id no longer resolves).

        Corrupt lines in findings.jsonl are skipped and reported under parse_errors.
        """
        if sample is not None:
            n, note = ctx.limit(sample, default=5)
            with ctx.store() as store:
                picked = lib.spotcheck_sample(
                    store, kind="findings", n=n, seed=seed, findings_dir=findings_dir, status=status
                )
            snip = SNIPPET_CHARS if max_chars is None else max_chars
            items, budget = [], ResponseBudget()
            for s in picked:
                item = {
                    "line": s["line"],
                    "finding": _wrap_finding(ctx, s["finding"]),
                    "evidence": [_wrap_evidence(ctx, v, snip) for v in s["evidence"]],
                }
                if not budget.admit(item):
                    break
                items.append(item)
            notes = [x for x in [note] if x]
            if budget.exhausted:
                notes.append(budget.note("lower sample or max_chars"))
            elif len(items) < n:
                notes.append(f"only {len(items)} findings matched; returned all of them")
            return {"sample": n, "seed": seed, "returned": len(items), "items": items, "notes": notes}
        limit, note = ctx.limit(limit, default=50)
        out = lib.list_findings(findings_dir, status=status, limit=limit)
        notes = [n for n in [note] if n]
        if out["parse_errors"]:
            notes.append(
                f"{len(out['parse_errors'])} corrupt line(s) in findings.jsonl were skipped; "
                "run `swarm-mcp info` for details."
            )
        findings, budget = [], ResponseBudget()
        for f in out["findings"]:
            wrapped = _wrap_finding(ctx, f)
            if not budget.admit(wrapped):
                break
            findings.append(wrapped)
        if budget.exhausted:
            notes.append(budget.note(f"showing {len(findings)} of {out['total_matching']}; filter by status"))
        elif out["has_more"]:
            notes.append(f"showing {out['returned']} of {out['total_matching']}; raise limit to see more")
        return {
            "findings": findings,
            "total_matching": out["total_matching"],
            "returned": len(findings),
            "has_more": len(findings) < out["total_matching"],
            "parse_errors": out["parse_errors"],
            "findings_file": str(lib.findings_file(findings_dir)),
            "notes": notes,
        }
