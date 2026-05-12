from __future__ import annotations

import networkx as nx

from auditor.loader import ConditionStatus, Flow, Step, StepStatus, TestCondition


def build_condition_graph(flows: list[Flow]) -> nx.DiGraph:
    g = nx.DiGraph()
    flow_map = {f.id: f for f in flows}

    # Add all nodes and explicit test_condition-level dependencies
    for flow in flows:
        for tc in flow.test_conditions:
            g.add_node(tc.id, test_condition=tc)
            for dep in tc.depends_on:
                g.add_edge(dep, tc.id)

    # Wire flow-level depends_on:
    # last test_conditions of dep_flow → first test_conditions of current flow
    for flow in flows:
        if not flow.depends_on:
            continue
        cur_tc_ids = {tc.id for tc in flow.test_conditions}
        # First test_conditions: those whose depends_on has no overlap with this flow's test_conditions
        first_tcs = [tc.id for tc in flow.test_conditions if not (set(tc.depends_on) & cur_tc_ids)]

        for dep_flow_id in flow.depends_on:
            dep_flow = flow_map.get(dep_flow_id)
            if not dep_flow:
                continue
            dep_tc_ids = {tc.id for tc in dep_flow.test_conditions}
            # Last test_conditions: not listed as a dependency by any other tc in the dep flow
            referenced = {d for tc in dep_flow.test_conditions for d in tc.depends_on} & dep_tc_ids
            last_tcs = [tc.id for tc in dep_flow.test_conditions if tc.id not in referenced]

            for last in last_tcs:
                for first in first_tcs:
                    g.add_edge(last, first)

    return g


def build_step_graph(steps: list[Step]) -> nx.DiGraph:
    g = nx.DiGraph()
    for step in steps:
        g.add_node(step.id, step=step)
        for dep in step.depends_on:
            g.add_edge(dep, step.id)
    return g


def condition_execution_order(g: nx.DiGraph) -> list[str]:
    return list(nx.topological_sort(g))


def step_execution_order(g: nx.DiGraph) -> list[str]:
    return list(nx.topological_sort(g))


def cascade_condition_failure(g: nx.DiGraph, tc_id: str) -> list[str]:
    return list(nx.descendants(g, tc_id))


def cascade_step_failure(g: nx.DiGraph, step_id: str) -> list[str]:
    return list(nx.descendants(g, step_id))


def mark_conditions_blocked(flows: list[Flow], tc_ids: set[str]) -> None:
    for flow in flows:
        for tc in flow.test_conditions:
            if tc.id in tc_ids:
                tc.status = ConditionStatus.blocked
                for step in tc.steps:
                    step.status = StepStatus.blocked


def mark_steps_blocked(steps: list[Step], step_ids: set[str]) -> None:
    step_map = {s.id: s for s in steps}
    for sid in step_ids:
        if sid in step_map:
            step_map[sid].status = StepStatus.blocked
