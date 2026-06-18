from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")


class StepType(str, Enum):
    existence = "existence"
    value = "value"
    behavioral = "behavioral"
    transition = "transition"
    persistence = "persistence"
    permission = "permission"
    constraint = "constraint"
    cross_module = "cross_module"


class StepStatus(str, Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    verified = "verified"
    failed = "failed"
    blocked = "blocked"
    unverifiable = "unverifiable"


class ConditionStatus(str, Enum):
    not_started = "not_started"
    verified = "verified"
    failed = "failed"
    blocked = "blocked"


class OutputCapture(BaseModel):
    key: str
    strategy: str  # "current_url" | "page_title" | "url_segment:N"


class ConditionExecution(BaseModel):
    """Developer/executor layer — fields a BA does not author."""
    navigation: str = ""        # URL path / page name the browser should start at
    navigation_mode: str = "fresh"  # "fresh"    → always navigate to `navigation` before starting
                                    # "continue" → stay on current browser state; ignore navigation hint
                                    # Default is "fresh" (backwards-compatible).
                                    # Use "continue" for TCs that pick up where the previous TC left off
                                    # (e.g. filling more fields on the same open form).
    input: list[str] = Field(default_factory=list)               # session_data keys from prior conditions
    output_capture: list[OutputCapture] = Field(default_factory=list)  # values to capture after this condition


class Step(BaseModel):
    # ── BA/QA layer (what a BA or QA analyst writes) ──────────────────────────
    id: str
    description: str
    type: StepType
    expected: str
    depends_on: list[str] = Field(default_factory=list)
    sequence: int = 0       # tiebreaker when multiple steps are at the same dependency level;
                            # lower runs first. 0 = "no preference" (file order preserved).
                            # Not part of the fingerprint hash — ordering ≠ intent.
    data: dict[str, str] = Field(default_factory=dict)   # test data injected into LLM context
    hints: list[str] = Field(default_factory=list)       # step-by-step action guidance for the LLM
    # ── Runtime state (set by loader/agent, not authored in YAML) ─────────────
    source_file: str = ""   # YAML stem this step belongs to — set by load_flows()
    status: StepStatus = StepStatus.not_started
    evidence: dict[str, Any] | None = None
    unverifiable_reason: str | None = None


class TestCondition(BaseModel):
    # ── BA/QA layer ───────────────────────────────────────────────────────────
    id: str
    goal: str
    depends_on: list[str] = Field(default_factory=list)
    sequence: int = 0       # tiebreaker when multiple test conditions are at the same dependency level
    steps: list[Step] = Field(default_factory=list)
    # ── Executor layer ────────────────────────────────────────────────────────
    execution: ConditionExecution = Field(default_factory=ConditionExecution)
    # ── Runtime state ─────────────────────────────────────────────────────────
    status: ConditionStatus = ConditionStatus.not_started

    @property
    def step_map(self) -> dict[str, Step]:
        return {s.id: s for s in self.steps}


class Flow(BaseModel):
    id: str
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)  # flow IDs
    test_conditions: list[TestCondition] = Field(default_factory=list)

    @property
    def test_condition_map(self) -> dict[str, TestCondition]:
        return {tc.id: tc for tc in self.test_conditions}

    @property
    def all_steps(self) -> list[Step]:
        return [s for tc in self.test_conditions for s in tc.steps]


class FlowFile(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    flows: list[Flow] = Field(default_factory=list)

    @property
    def all_test_conditions(self) -> list[TestCondition]:
        return [tc for f in self.flows for tc in f.test_conditions]

    @property
    def all_steps(self) -> list[Step]:
        return [s for f in self.flows for s in f.all_steps]


def _resolve_data(flow_file: FlowFile, test_data: dict[str, str]) -> None:
    """Resolve {{key}} placeholders in every claim's data values in-place.

    Claims may reference test data by key so that the values are kept in a
    separate environment-specific file (test_data.yaml) rather than hardcoded
    in claims.yaml.  Example in claims.yaml:

        data:
          requesting_agency: "{{requesting_agency}}"

    At runtime the placeholder is replaced with the value from test_data:

        data:
          requesting_agency: "Department of Homeless Services"

    Unknown keys are left as-is so that partial test_data files are safe.
    """
    if not test_data:
        return

    def _sub(value: str) -> str:
        return _TEMPLATE_RE.sub(
            lambda m: test_data.get(m.group(1), m.group(0)), value
        )

    for step in flow_file.all_steps:
        step.data = {k: _sub(v) for k, v in step.data.items()}


def load_test_data(path: str | Path | None) -> dict[str, str]:
    """Load test_data.yaml and return a flat string-to-string dict.

    Returns an empty dict if path is None or the file does not exist, so that
    callers never need to guard against a missing file.
    """
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text()) or {}
    return {str(k): str(v) for k, v in raw.items()}


def _validate_unique_ids(merged: FlowFile) -> None:
    """Raise ValueError if any test condition ID or step ID is duplicated.

    Duplicate IDs cause the dependency graph builder to create self-loops,
    which topological sort correctly rejects as a cycle. Catching this early
    produces a clear, actionable error instead of a cryptic NetworkXUnfeasible.
    """
    seen_tc: dict[str, str] = {}    # tc_id → flow_id
    seen_step: dict[str, str] = {}  # step_id → tc_id

    for flow in merged.flows:
        for tc in flow.test_conditions:
            if tc.id in seen_tc:
                raise ValueError(
                    f"Duplicate test condition id '{tc.id}' in flow '{flow.id}' "
                    f"— already defined in flow '{seen_tc[tc.id]}'. "
                    f"All test condition ids must be unique across the file."
                )
            seen_tc[tc.id] = flow.id
            for step in tc.steps:
                if step.id in seen_step:
                    raise ValueError(
                        f"Duplicate step id '{step.id}' in tc '{tc.id}' "
                        f"— already defined in tc '{seen_step[step.id]}'. "
                        f"All step ids must be unique across the file."
                    )
                seen_step[step.id] = tc.id


def load_flows(*paths: str | Path, test_data_path: str | Path | None = None) -> FlowFile:
    """Load and merge one or more YAML flow files.

    If *test_data_path* is provided, {{key}} placeholders in each claim's
    ``data`` values are resolved from the corresponding test_data.yaml file
    before the FlowFile is returned.
    """
    merged = FlowFile()
    for path in paths:
        raw = yaml.safe_load(Path(path).read_text())
        ff = FlowFile.model_validate(raw)
        if ff.config and not merged.config:
            merged.config = ff.config
        # Tag every step with the YAML stem so the fingerprint router can scope
        # fingerprints by (source_file, step_id) — avoids collisions when multiple
        # YAML files share step IDs like step_001.
        stem = Path(path).stem
        for step in ff.all_steps:
            step.source_file = stem
        merged.flows.extend(ff.flows)

    _validate_unique_ids(merged)

    test_data = load_test_data(test_data_path)
    if test_data:
        _resolve_data(merged, test_data)

    return merged
