from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")


class ClaimType(str, Enum):
    existence = "existence"
    value = "value"
    behavioral = "behavioral"
    transition = "transition"
    persistence = "persistence"
    permission = "permission"
    constraint = "constraint"
    cross_module = "cross_module"


class ClaimStatus(str, Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    verified = "verified"
    failed = "failed"
    blocked = "blocked"
    unverifiable = "unverifiable"


class StepStatus(str, Enum):
    not_started = "not_started"
    verified = "verified"
    failed = "failed"
    blocked = "blocked"


class SetupStep(BaseModel):
    fill_field: dict[str, str] | None = None
    click: str | None = None
    hover: str | None = None


class Claim(BaseModel):
    id: str
    description: str
    type: ClaimType
    navigation: str = ""   # deprecated — use Step.navigation; kept for backward compat
    expected: str
    setup: list[SetupStep] = Field(default_factory=list)
    action: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    data: dict[str, str] = Field(default_factory=dict)   # test data injected into LLM context
    status: ClaimStatus = ClaimStatus.not_started
    evidence: dict[str, Any] | None = None
    unverifiable_reason: str | None = None


class OutputCapture(BaseModel):
    key: str
    strategy: str  # "current_url" | "page_title" | "url_segment:N"


class Step(BaseModel):
    id: str
    goal: str
    navigation: str = ""   # where the browser should be at the start of this step
    depends_on: list[str] = Field(default_factory=list)
    input: list[str] = Field(default_factory=list)        # session_data keys needed from prior steps
    output_capture: list[OutputCapture] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    status: StepStatus = StepStatus.not_started

    @property
    def claim_map(self) -> dict[str, Claim]:
        return {c.id: c for c in self.claims}


class Flow(BaseModel):
    id: str
    description: str
    depends_on: list[str] = Field(default_factory=list)  # flow IDs
    steps: list[Step] = Field(default_factory=list)

    @property
    def step_map(self) -> dict[str, Step]:
        return {s.id: s for s in self.steps}

    @property
    def all_claims(self) -> list[Claim]:
        return [c for s in self.steps for c in s.claims]


class FlowFile(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    flows: list[Flow] = Field(default_factory=list)

    @property
    def all_steps(self) -> list[Step]:
        return [s for f in self.flows for s in f.steps]

    @property
    def all_claims(self) -> list[Claim]:
        return [c for f in self.flows for c in f.all_claims]


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

    for claim in flow_file.all_claims:
        claim.data = {k: _sub(v) for k, v in claim.data.items()}


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
        merged.flows.extend(ff.flows)

    test_data = load_test_data(test_data_path)
    if test_data:
        _resolve_data(merged, test_data)

    return merged
