"""Who-talks-to-whom: a directed, weighted communication graph over messages.

Edge kinds (both aggregated in SQL; weight = number of messages):
  mention  author -> each agent named in the message text (``recipient_ids``).
  reply    in one channel, ordered by (ts, evidence_id): when message i+1 is by
           a different author and arrives within ``reply_window_minutes`` of
           message i, responder -> previous author. A temporal-adjacency
           heuristic, not an explicit reply link.

Each edge keeps one example evidence id (the earliest message that produced
it). Node metrics come from networkx: weighted in/out degree (sum of edge
weights, mention and reply combined) and betweenness centrality with
distance = 1/weight (normalized).
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from swarm_mcp.scope.analysis.timeline import record_filters, ts_iso
from swarm_mcp.scope.db import Store, label_for
from swarm_mcp.toolkit import ToolInputError

EDGE_TYPES = ("mentions", "replies", "both")
_KINDS = {"mentions": ("mention",), "replies": ("reply",), "both": ("mention", "reply")}


def _edge_rows(store: Store, sql: str, params: list[Any], kind: str) -> list[dict[str, Any]]:
    return [{**r, "type": kind} for r in store.all(sql, params)]


def comm_graph(
    store: Store,
    *,
    source: str | None = None,
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    reply_window_minutes: float = 5,
    edge_types: str = "both",
    include_humans: bool = False,
    top_nodes: int = 15,
    max_edges: int = 50,
    min_weight: int = 1,
) -> dict[str, Any]:
    """Build the graph for messages matching the filters; return top nodes, top edges and totals."""
    if edge_types not in EDGE_TYPES:
        raise ToolInputError(f"edge_types must be one of {', '.join(EDGE_TYPES)}, not {edge_types!r}")
    if reply_window_minutes <= 0:
        raise ToolInputError("reply_window_minutes must be > 0")
    where, params = record_filters("messages", source=source, channel=channel, since=since, until=until)
    base = " AND ".join(where) or "TRUE"
    # agents are tested positively (listed in the agents table): human:, external: and unknown actors are not agents
    agents_only = "(SELECT agent_id FROM agents)"
    no_humans = "" if include_humans else f" AND src IN {agents_only} AND dst IN {agents_only}"

    considered = store.scalar(f"SELECT count(*) FROM messages WHERE {base}", params) or 0
    edges: list[dict[str, Any]] = []
    if edge_types in ("mentions", "both"):
        edges += _edge_rows(
            store,
            f"""
            WITH m AS (
                SELECT evidence_id, ts, author_id AS src, unnest(recipient_ids) AS dst
                FROM messages WHERE {base} AND len(recipient_ids) > 0
            )
            SELECT src, dst, count(*) AS weight, arg_min(evidence_id, (ts, evidence_id)) AS evidence_id,
                   min(ts) AS first_ts
            FROM m WHERE src <> dst{no_humans}
            GROUP BY 1, 2
            """,
            params,
            "mention",
        )
    if edge_types in ("replies", "both"):
        # The window runs over every message in the filter (humans included), so an
        # agent answering a human is not mistaken for a reply to an earlier agent.
        edges += _edge_rows(
            store,
            f"""
            WITH m AS (
                SELECT evidence_id, ts, author_id AS src,
                       lag(author_id) OVER w AS dst, lag(ts) OVER w AS prev_ts
                FROM messages
                WHERE {base} AND channel IS NOT NULL AND ts IS NOT NULL
                WINDOW w AS (PARTITION BY channel ORDER BY ts, evidence_id)
            )
            SELECT src, dst, count(*) AS weight, arg_min(evidence_id, (ts, evidence_id)) AS evidence_id,
                   min(ts) AS first_ts
            FROM m
            WHERE dst IS NOT NULL AND src <> dst AND epoch_ms(ts) - epoch_ms(prev_ts) <= ?{no_humans}
            GROUP BY 1, 2
            """,
            [*params, int(round(reply_window_minutes * 60_000))],
            "reply",
        )

    kept = [e for e in edges if e["weight"] >= min_weight]
    g = nx.DiGraph()
    for e in kept:
        if g.has_edge(e["src"], e["dst"]):
            g[e["src"]][e["dst"]]["weight"] += e["weight"]
        else:
            g.add_edge(e["src"], e["dst"], weight=e["weight"])
    for _, _, d in g.edges(data=True):
        d["distance"] = 1.0 / d["weight"]
    between = nx.betweenness_centrality(g, weight="distance", normalized=True) if g.number_of_nodes() > 2 else {}
    in_w = dict(g.in_degree(weight="weight"))
    out_w = dict(g.out_degree(weight="weight"))

    names = store.display_names()
    authored: dict[str, int] = {}
    if g.number_of_nodes():
        authored = {
            r["author_id"]: r["n"]
            for r in store.all(
                f"SELECT author_id, count(*) AS n FROM messages "
                f"WHERE {base} AND list_contains(?, author_id) GROUP BY 1",
                [*params, list(g.nodes)],
            )
        }

    def node(n: str) -> dict[str, Any]:
        return {
            "agent_id": n,
            "name": label_for(n, names),
            "in_weight": in_w.get(n, 0),
            "out_weight": out_w.get(n, 0),
            "total_weight": in_w.get(n, 0) + out_w.get(n, 0),
            "betweenness": round(between.get(n, 0.0), 4),
            "messages": authored.get(n, 0),
        }

    by_degree = sorted(g.nodes, key=lambda n: (-(in_w.get(n, 0) + out_w.get(n, 0)), label_for(n, names)))
    by_between = sorted(g.nodes, key=lambda n: (-between.get(n, 0.0), label_for(n, names)))
    kept.sort(key=lambda e: (-e["weight"], e["type"], e["src"], e["dst"]))
    by_type = {t: sum(1 for e in kept if e["type"] == t) for t in ("mention", "reply")}
    return {
        "nodes": [node(n) for n in by_degree[:top_nodes]],
        "top_betweenness": [
            {"agent_id": n, "name": label_for(n, names), "betweenness": round(between.get(n, 0.0), 4)}
            for n in by_between[:top_nodes]
        ],
        "edges": [
            {
                "source": e["src"],
                "source_name": label_for(e["src"], names),
                "target": e["dst"],
                "target_name": label_for(e["dst"], names),
                "type": e["type"],
                "weight": e["weight"],
                "evidence_id": e["evidence_id"],
                "first_ts": ts_iso(e["first_ts"]),
            }
            for e in kept[:max_edges]
        ],
        "totals": {
            "messages_considered": considered,
            "nodes": g.number_of_nodes(),
            "edges": len(kept),
            "edges_by_type": {t: n for t, n in by_type.items() if t in _KINDS[edge_types]},
            "edges_below_min_weight": len(edges) - len(kept),
            "mention_links": sum(e["weight"] for e in kept if e["type"] == "mention"),
            "reply_links": sum(e["weight"] for e in kept if e["type"] == "reply"),
            "edges_returned": min(len(kept), max_edges),
            "nodes_returned": min(g.number_of_nodes(), top_nodes),
        },
    }


NOTES = [
    "mention edge: author -> each agent named in the message text (recipient_ids, self excluded); weight = messages.",
    "reply edge: in one channel ordered by (ts, evidence_id), message i+1 by a different author within "
    "reply_window_minutes of message i gives responder -> previous author. A temporal-adjacency heuristic, "
    "not an explicit reply link.",
    "Each edge's evidence_id is the earliest message that produced it; resolve it with core_get.",
    "Node weights sum both edge types; betweenness uses distance = 1/weight (normalized). Edges below "
    "min_weight are dropped before the metrics are computed.",
]
