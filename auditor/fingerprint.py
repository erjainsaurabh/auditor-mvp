from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class SelectorRecord(BaseModel):
    type: str  # "xpath" | "aria_label" | "text"
    value: str
    successes: int = 1
    failures: int = 0

    @property
    def confidence(self) -> float:
        total = self.successes + self.failures
        return self.successes / total if total > 0 else 0.0


class ActionRecord(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    selectors: list[SelectorRecord] = Field(default_factory=list)
    assertions: list[str] = Field(default_factory=list)


class StepFingerprint(BaseModel):
    step_id: str
    recorded_at: str
    run_id: str
    verdict: str
    confidence: str
    actions: list[ActionRecord] = Field(default_factory=list)
    setup_records: list[ActionRecord] = Field(default_factory=list)


class FingerprintStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._store: dict[str, StepFingerprint] = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
            for step_id, data in (raw.get("steps") or {}).items():
                try:
                    self._store[step_id] = StepFingerprint.model_validate(data)
                except Exception:
                    pass  # skip corrupt entries

    def get(self, step_id: str) -> StepFingerprint | None:
        return self._store.get(step_id)

    def record(self, fp: StepFingerprint) -> None:
        self._store[fp.step_id] = fp

    def save(self) -> None:
        data = {
            "steps": {
                sid: fp.model_dump()
                for sid, fp in self._store.items()
            }
        }
        self._path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
        )


class FingerprintRouter:
    """Routes fingerprint reads across all stores, writes to the correct per-YAML store."""

    def __init__(self, stores: list[tuple[FingerprintStore, set[str]]]) -> None:
        # stores: list of (store, {step_ids that belong to it})
        self._stores = stores
        self._id_to_store: dict[str, FingerprintStore] = {}
        for store, step_ids in stores:
            for sid in step_ids:
                self._id_to_store[sid] = store

    def get(self, step_id: str) -> StepFingerprint | None:
        store = self._id_to_store.get(step_id)
        return store.get(step_id) if store else None

    def record(self, fp: StepFingerprint) -> None:
        store = self._id_to_store.get(fp.step_id)
        if store:
            store.record(fp)
            store.save()

    def save(self) -> None:
        seen: set[int] = set()
        for store, _ in self._stores:
            if id(store) not in seen:
                store.save()
                seen.add(id(store))


_DYNAMIC_ID_RE = re.compile(r'\d{5,}')
"""Matches any sequence of 5+ consecutive digits.

Assertions containing such sequences are run-specific identifiers (record
numbers, ticket IDs, timestamps, etc.) that change on every run and must
never be stored in fingerprints.  Examples that are filtered out:
  "REQ262374"              → 6 digits
  "Requisition: REQ262374" → 6 digits embedded in a longer string
  "INC0001234"             → 7 digits
  "CNTR-2026-001234"       → 6 digits
  "2026-05-11"             → date (only 4 digits per segment — not filtered)
  "Create Sourcing Req."   → no digits — kept
"""


def is_dynamic_assertion(s: str) -> bool:
    """Return True if *s* contains a run-specific numeric ID and should not be stored."""
    return bool(_DYNAMIC_ID_RE.search(s))


def extract_assertions(reasoning: str, snapshot: str) -> list[str]:
    """Pull quoted terms from LLM reasoning that also appear in the page snapshot.

    Matches both double-quoted ("Foo") and single-quoted ('Foo') strings so that
    LLM phrasing style doesn't cause empty assertion lists.  Only terms that
    actually appear in the current snapshot are stored — this keeps assertions
    live and avoids storing stale strings.

    Dynamic IDs (strings containing 5+ consecutive digits — record numbers,
    ticket IDs, etc.) are filtered out at recording time so fingerprints stay
    stable across runs that create different records.
    """
    # Collect candidates from both quote styles; deduplicate by lower-case value
    double_quoted = re.findall(r'"([^"]{3,60})"', reasoning)
    single_quoted = re.findall(r"'([^']{3,60})'", reasoning)
    candidates = double_quoted + single_quoted

    snapshot_lower = snapshot.lower()
    seen: set[str] = set()
    result: list[str] = []
    for q in candidates:
        if is_dynamic_assertion(q):
            continue          # skip run-specific IDs — they'll never match next run
        q_lower = q.lower()
        if q_lower not in seen and q_lower in snapshot_lower:
            seen.add(q_lower)
            result.append(q)
        if len(result) >= 6:   # raised from 4 — single+double can give more candidates
            break
    return result
