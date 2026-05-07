from __future__ import annotations

import networkx as nx

from auditor.loader import Claim, ClaimStatus


def build_graph(claims: list[Claim]) -> nx.DiGraph:
    g = nx.DiGraph()
    for claim in claims:
        g.add_node(claim.id, claim=claim)
    for claim in claims:
        for dep in claim.depends_on:
            g.add_edge(dep, claim.id)
    return g


def execution_order(graph: nx.DiGraph) -> list[str]:
    return list(nx.topological_sort(graph))


def cascade_failure(graph: nx.DiGraph, failed_claim_id: str) -> list[str]:
    return list(nx.descendants(graph, failed_claim_id))


def mark_blocked(graph: nx.DiGraph, claim_ids: list[str]) -> None:
    for cid in claim_ids:
        graph.nodes[cid]["claim"].status = ClaimStatus.blocked
