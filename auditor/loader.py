from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


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


def load_flows(*paths: str | Path) -> FlowFile:
    """Load and merge one or more YAML flow files."""
    merged = FlowFile()
    for path in paths:
        raw = yaml.safe_load(Path(path).read_text())
        ff = FlowFile.model_validate(raw)
        if ff.config and not merged.config:
            merged.config = ff.config
        merged.flows.extend(ff.flows)
    return merged
