from __future__ import annotations

import networkx as nx

from auditor.loader import Claim, ClaimStatus, Flow, Step, StepStatus


def build_step_graph(flows: list[Flow]) -> nx.DiGraph:
    g = nx.DiGraph()
    flow_map = {f.id: f for f in flows}

    # Add all nodes and explicit step-level dependencies
    for flow in flows:
        for step in flow.steps:
            g.add_node(step.id, step=step)
            for dep in step.depends_on:
                g.add_edge(dep, step.id)

    # Wire flow-level depends_on:
    # last steps of dep_flow → first steps of current flow
    for flow in flows:
        if not flow.depends_on:
            continue
        cur_step_ids = {s.id for s in flow.steps}
        # First steps: steps whose depends_on has no overlap with this flow's steps
        first_steps = [s.id for s in flow.steps if not (set(s.depends_on) & cur_step_ids)]

        for dep_flow_id in flow.depends_on:
            dep_flow = flow_map.get(dep_flow_id)
            if not dep_flow:
                continue
            dep_step_ids = {s.id for s in dep_flow.steps}
            # Last steps: not listed as a dependency by any other step in the dep flow
            referenced = {d for s in dep_flow.steps for d in s.depends_on} & dep_step_ids
            last_steps = [s.id for s in dep_flow.steps if s.id not in referenced]

            for last in last_steps:
                for first in first_steps:
                    g.add_edge(last, first)

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
