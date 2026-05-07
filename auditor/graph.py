from __future__ import annotations

import networkx as nx

from auditor.loader import Claim, ClaimStatus, Flow, Step, StepStatus


def build_step_graph(flows: list[Flow]) -> nx.DiGraph:
    g = nx.DiGraph()
    for flow in flows:
        for step in flow.steps:
            g.add_node(step.id, step=step)
            for dep in step.depends_on:
                g.add_edge(dep, step.id)
    return g


def build_claim_graph(claims: list[Claim]) -> nx.DiGraph:
    g = nx.DiGraph()
    for claim in claims:
        g.add_node(claim.id, claim=claim)
        for dep in claim.depends_on:
            g.add_edge(dep, claim.id)
    return g


def step_execution_order(g: nx.DiGraph) -> list[str]:
    return list(nx.topological_sort(g))


def claim_execution_order(g: nx.DiGraph) -> list[str]:
    return list(nx.topological_sort(g))


def cascade_step_failure(g: nx.DiGraph, step_id: str) -> list[str]:
    return list(nx.descendants(g, step_id))


def cascade_claim_failure(g: nx.DiGraph, claim_id: str) -> list[str]:
    return list(nx.descendants(g, claim_id))


def mark_steps_blocked(flows: list[Flow], step_ids: set[str]) -> None:
    for flow in flows:
        for step in flow.steps:
            if step.id in step_ids:
                step.status = StepStatus.blocked
                for claim in step.claims:
                    claim.status = ClaimStatus.blocked


def mark_claims_blocked(claims: list[Claim], claim_ids: set[str]) -> None:
    claim_map = {c.id: c for c in claims}
    for cid in claim_ids:
        if cid in claim_map:
            claim_map[cid].status = ClaimStatus.blocked
