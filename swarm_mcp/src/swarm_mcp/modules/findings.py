"""Findings: record evidence-backed claims, list them, and draw spot-check samples.

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

NAME = "findings"
DESCRIPTION = (
    "Record claims that cite evidence ids (rejected unless every id resolves), list them, "
    "and draw deterministic random spot-check samples of findings, messages or actions for human review."
)

SNIPPET_CHARS = 200  # cap for the evidence snippets shown next to a finding
CLAIM_CHARS = 2000  # claims are investigator-written; show them (almost) whole


def requires(ctx) -> list[str]:
    if not ctx.store_path.exists():
        return [f"SwarmScope store not found at {ctx.store_path}; run `swarm-mcp ingest ai_village data/ai-village`"]
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
                "(e.g. 'village:chat:<uuid>', 'village:agent:<uuid>', 'village:event:<uuid>', "
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
    ) -> dict[str, Any]:
        """List recorded findings, newest first, with their evidence ids.

        Corrupt lines in findings.jsonl are skipped and reported under parse_errors.
        """
        limit, note = ctx.limit(limit, default=50)
        out = lib.list_findings(findings_dir, status=status, limit=limit)
        notes = [n for n in [note] if n]
        if out["parse_errors"]:
            notes.append(
                f"{len(out['parse_errors'])} corrupt line(s) in findings.jsonl were skipped; "
                "run `swarm-mcp check-findings` for details."
            )
        if out["has_more"]:
            notes.append(f"showing {out['returned']} of {out['total_matching']}; raise limit to see more")
        return {
            "findings": [_wrap_finding(ctx, f) for f in out["findings"]],
            "total_matching": out["total_matching"],
            "returned": out["returned"],
            "has_more": out["has_more"],
            "parse_errors": out["parse_errors"],
            "findings_file": str(lib.findings_file(findings_dir)),
            "notes": notes,
        }

    @ctx.tool()
    def findings_spotcheck(
        kind: Annotated[
            Literal["findings", "messages", "actions"],
            Field(description="What to sample: recorded findings (with their cited evidence), messages, or actions."),
        ] = "findings",
        n: Annotated[int, Field(description="Sample size (default 5, max 200).")] = 5,
        seed: Annotated[int, Field(description="Random seed; the same seed and filters give the same sample.")] = 0,
        max_chars: Annotated[
            int | None, Field(description="Max chars per sampled text (default: server max_text, 500).")
        ] = None,
        source: Annotated[str | None, Field(description="Only this source (e.g. 'village').")] = None,
        channel: Annotated[str | None, Field(description="Only this channel (messages only), e.g. 'general'.")] = None,
        since: Annotated[str | None, Field(description="Inclusive start, ISO date/datetime (UTC).")] = None,
        until: Annotated[
            str | None, Field(description="Exclusive end, ISO date/datetime (UTC); a bare date includes that day.")
        ] = None,
    ) -> dict[str, Any]:
        """Draw a deterministic random sample for human spot-checking.

        kind='findings' returns sampled findings with each cited record's snippet so a human
        can compare claim and evidence. kind='messages'/'actions' returns sampled records
        (with evidence ids) to sanity-check ingest and coverage. Filters: source, channel
        (messages), since/until (record time; finding creation time for findings).
        """
        n, note = ctx.limit(n, default=5)
        with ctx.store() as store:
            sample = lib.spotcheck_sample(
                store,
                kind=kind,
                n=n,
                seed=seed,
                findings_dir=findings_dir,
                source=source,
                channel=channel,
                since=since,
                until=until,
            )
        if kind == "findings":
            snip = SNIPPET_CHARS if max_chars is None else min(SNIPPET_CHARS, max_chars)
            items = [
                {
                    "line": s["line"],
                    "finding": _wrap_finding(ctx, s["finding"]),
                    "evidence": [_wrap_evidence(ctx, v, snip) for v in s["evidence"]],
                }
                for s in sample
            ]
        else:  # max_chars=None -> ctx.untrusted's default (config.max_text)
            items = [_wrap_evidence(ctx, s, max_chars) for s in sample]
        notes = [x for x in [note] if x]
        if len(items) < n:
            notes.append(f"only {len(items)} {kind} matched; returned all of them")
        return {"kind": kind, "seed": seed, "returned": len(items), "items": items, "notes": notes}
