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


class SetupStep(BaseModel):
    fill_field: dict[str, str] | None = None
    click: str | None = None
    hover: str | None = None


class Claim(BaseModel):
    id: str
    description: str
    type: ClaimType
    navigation: str
    expected: str
    setup: list[SetupStep] = Field(default_factory=list)
    action: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    status: ClaimStatus = ClaimStatus.not_started
    evidence: dict[str, Any] | None = None
    unverifiable_reason: str | None = None


class ClaimsFile(BaseModel):
    config: dict[str, Any]
    claims: list[Claim]


def load_claims(path: str | Path) -> ClaimsFile:
    raw = yaml.safe_load(Path(path).read_text())
    return ClaimsFile.model_validate(raw)
