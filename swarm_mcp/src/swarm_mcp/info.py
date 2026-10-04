"""One overview of the running server: modules, event sources (with store statistics),
findings health and the effective configuration.

``core_info`` returns ``server_info(...)``; ``swarm-mcp info`` prints ``format_info(...)``.
"""

from __future__ import annotations

import json
from typing import Any

from swarm_mcp import __version__
from swarm_mcp.config import Config

MAX_CHANNELS = 20


def _meta(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value if value is not None else {}


def store_sources(store: Any, max_channels: int = MAX_CHANNELS) -> list[dict[str, Any]]:
    """Per ingested source: adapter, path, ingest time, row counts, time ranges, channels, action kinds."""
    from swarm_mcp.scope.analysis.timeline import ts_iso

    out = []
    for r in store.all("SELECT source, adapter, path, ingested_at, meta FROM sources ORDER BY source"):
        src = r["source"]
        rows = {
            t: store.scalar(f"SELECT count(*) FROM {t} WHERE source = ?", [src])
            for t in ("agents", "messages", "actions", "periods")
        }
        mt = store.one("SELECT min(ts) AS lo, max(ts) AS hi FROM messages WHERE source = ?", [src]) or {}
        at = store.one("SELECT min(ts) AS lo, max(ts) AS hi FROM actions WHERE source = ?", [src]) or {}
        channels = store.all(
            "SELECT coalesce(channel, '(none)') AS channel, count(*) AS messages FROM messages "
            "WHERE source = ? GROUP BY 1 ORDER BY 2 DESC, 1",
            [src],
        )
        kinds = store.all(
            "SELECT kind, count(*) AS n FROM actions WHERE source = ? GROUP BY 1 ORDER BY 2 DESC, 1", [src]
        )
        meta = _meta(r["meta"]) or {}
        item: dict[str, Any] = {
            "source": src,
            "adapter": r["adapter"],
            "path": r["path"],
            "ingested_at": ts_iso(r["ingested_at"]),
            "row_counts": rows,
            "messages_ts": {"min": ts_iso(mt.get("lo")), "max": ts_iso(mt.get("hi"))},
            "actions_ts": {"min": ts_iso(at.get("lo")), "max": ts_iso(at.get("hi"))},
            "action_kinds": {k["kind"]: k["n"] for k in kinds},
            "channels": channels[:max_channels],
        }
        if len(channels) > max_channels:
            item["channels_total"] = len(channels)
        if meta.get("mapping"):
            item["mapping"] = meta["mapping"]
        out.append(item)
    return out


def findings_health(config: Config) -> dict[str, Any]:
    """Summary of ``check_findings`` on <findings dir>/findings.jsonl (``bad`` = unresolvable/corrupt)."""
    from swarm_mcp.scope.findings import check_findings

    ffile = config.findings_path / "findings.jsonl"
    try:
        res = check_findings(ffile, config.store_path)
    except Exception as e:  # noqa: BLE001 - info must never fail on a broken findings file
        return {"file": str(ffile), "ok": False, "bad": False, "message": f"check failed: {type(e).__name__}: {e}"}
    bad = not res["ok"] and not res.get("store_missing")
    return {
        "file": str(ffile),
        "ok": res["ok"],
        "bad": bad,
        "checked": res["checked"],
        "with_problems": len(res["problems"]),
        "corrupt_lines": len(res["parse_errors"]),
        "store_missing": res.get("store_missing", False),
        "message": res["message"],
        "problems": res["problems"][:10],
    }


def server_info(config: Config, registry: Any) -> dict[str, Any]:
    """Modules, event sources (+ store statistics), findings health and config, in one dict."""
    from swarm_mcp.scope import db

    sources: dict[str, dict[str, Any]] = {
        s.name: s.as_dict() for s in sorted(registry.events.by_name.values(), key=lambda s: s.name)
    }
    notes: list[str] = []
    if config.store_path.exists():
        try:
            with db.connect(config.store_path) as s:
                for st in store_sources(s):
                    sources.setdefault(st["source"], {"source": st["source"], "kinds": []})["store"] = st
        except Exception as e:  # noqa: BLE001 - e.g. the store is locked by an ingest
            notes.append(f"store statistics unavailable: {type(e).__name__}: {e}")
    else:
        notes.append(f"no SwarmScope store at {config.store_path}; add a dataset with `swarm-mcp add <path>`")
    notes += [
        "Every record id is '<source>:<kind>:<id>'; core_get expands one id or a batch.",
        "All timestamps are UTC.",
    ]
    return {
        "server": {"name": "swarm", "version": __version__},
        "modules": {
            "loaded": [r.as_dict() for r in registry.loaded],
            "skipped": [r.as_dict() for r in registry.skipped],
        },
        "module_notes": list(registry.notes),
        "sources": list(sources.values()),
        "findings": findings_health(config),
        "config": config.public(),
        "notes": notes,
    }


def format_info(info: dict[str, Any]) -> str:
    """Human-readable ``swarm-mcp info``."""
    srv, cfg = info["server"], info["config"]
    lines = [f"swarm-mcp {srv['version']}", ""]
    lines.append("Config")
    lines.append(f"  file       {cfg['config_file'] or '(none: defaults; see swarm.toml in the README)'}")
    lines.append(f"  data dir   {cfg['data_dir']}")
    lines.append(f"  store      {cfg['db_path']}{'' if cfg['db_exists'] else '  (missing)'}")
    lines.append(f"  findings   {cfg['findings_dir']}")
    lines.append(f"  sweeps     {cfg['sweeps_dir']}")
    llm = cfg["llm"]
    lines.append(f"  llm        {llm['model']} (effort {llm['effort']}; API key {'set' if llm['api_key_set'] else 'not set'})")
    lines.append("")
    lines.append("Modules")
    for m in info["modules"]["loaded"]:
        parts = []
        if m.get("tools"):
            parts.append(", ".join(m["tools"]))
        if m.get("prompts"):
            parts.append("prompts: " + ", ".join(m["prompts"]))
        lines.append(f"  + {m['name']:<12} {'; '.join(parts) or '(resources only)'}")
    for m in info["modules"]["skipped"]:
        lines.append(f"  - {m['name']:<12} skipped: {'; '.join(m.get('reasons') or [])}")
    for n in info.get("module_notes") or []:
        lines.append(f"  note: {n}")
    lines.append("")
    lines.append("Sources")
    if not info["sources"]:
        lines.append("  (none)")
    for s in info["sources"]:
        kinds = ", ".join(k["kind"] for k in s.get("kinds") or [])
        st = s.get("store")
        if st:
            rc = st["row_counts"]
            span = f"{st['messages_ts']['min'] or '?'} .. {st['messages_ts']['max'] or '?'}"
            lines.append(
                f"  {s['source']:<10} {rc['messages']:,} messages, {rc['actions']:,} actions, {rc['agents']:,} agents, "
                f"{rc['periods']:,} periods; {span} (adapter {st['adapter']})"
            )
        else:
            lines.append(f"  {s['source']:<10} kinds: {kinds}")
    lines.append("")
    f = info["findings"]
    status = "ok" if f["ok"] else ("BAD EVIDENCE" if f.get("bad") else "unchecked")
    lines.append(f"Findings  [{status}] {f['message']}")
    for p in f.get("problems") or []:
        bad = ", ".join(p.get("bad_evidence") or {}) or p.get("error", "")
        lines.append(f"  line {p['line']} {p['finding_id']}: {bad}")
    for n in info.get("notes") or []:
        if n.startswith("no SwarmScope store") or n.startswith("store statistics"):
            lines.append(f"note: {n}")
    return "\n".join(lines)
