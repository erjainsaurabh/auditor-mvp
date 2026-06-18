from __future__ import annotations

import hashlib
import json
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
    source_file: str = ""   # YAML stem — used by FingerprintRouter to write to the right store
    recorded_at: str
    run_id: str
    verdict: str
    confidence: str
    actions: list[ActionRecord] = Field(default_factory=list)
    step_hash: str = ""     # SHA-1 of the step's definition fields — empty means "untracked"


def step_definition_hash(description: str, expected: str, navigation: str, data_keys: list[str]) -> str:
    """Return a short SHA-1 digest of the stable, intent-defining fields of a Step.

    Only fields that change the *meaning* of the step are included:
      - description  — what the step is verifying
      - expected     — the observable state being checked
      - navigation   — where the browser should be
      - data_keys    — which test-data keys are used (not the values — those are in test_data.yaml)

    Intentionally excluded:
      - depends_on   — execution order, not intent
      - type         — rarely changes meaning
      - data values  — live in test_data.yaml, not the YAML spec
    """
    payload = json.dumps(
        {
            "description": description.strip(),
            "expected": expected.strip(),
            "navigation": navigation.strip(),
            "data_keys": sorted(data_keys),
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


class FingerprintStore:
    def __init__(self, path: Path, source_file: str = "") -> None:
        self._path = path
        self.source_file = source_file or path.stem.split(".")[0]
        self._store: dict[str, StepFingerprint] = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
            for step_id, data in (raw.get("steps") or {}).items():
                try:
                    self._store[step_id] = StepFingerprint.model_validate(data)
                except Exception:
                    pass  # skip corrupt entries

    def get(self, step_id: str, source_file: str = "") -> StepFingerprint | None:
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
    """Routes fingerprint reads/writes across per-YAML stores.

    Uses (source_file, step_id) as the compound key so that step_001 in
    login.yaml and step_001 in requisition_claims.yaml are stored and
    retrieved independently — no collision regardless of ID reuse across files.
    """

    def __init__(self, stores: list[tuple[FingerprintStore, set[str]]]) -> None:
        # stores: list of (store, {step_ids that belong to it})
        self._stores = stores
        # Compound key: (source_file_stem, step_id)
        self._id_to_store: dict[tuple[str, str], FingerprintStore] = {}
        for store, step_ids in stores:
            for sid in step_ids:
                self._id_to_store[(store.source_file, sid)] = store

    def get(self, step_id: str, source_file: str = "") -> StepFingerprint | None:
        store = self._id_to_store.get((source_file, step_id))
        return store.get(step_id) if store else None

    def record(self, fp: StepFingerprint) -> None:
        store = self._id_to_store.get((fp.source_file, fp.step_id))
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


def _snapshot_url_path(snapshot: str) -> str:
    """Return the URL path from the first line of a read_page snapshot, or ''."""
    for line in snapshot.splitlines():
        if line.startswith("url: "):
            url = line[5:].strip()
            try:
                from urllib.parse import urlparse
                path = urlparse(url).path.rstrip("/")
                # Skip paths that contain dynamic IDs (e.g. /order_manage/74770)
                if path and not _DYNAMIC_ID_RE.search(path):
                    return path
            except Exception:
                pass
            break
    return ""


def _snapshot_title(snapshot: str) -> str:
    """Return the page title (without app suffix) from a read_page snapshot, or ''."""
    for line in snapshot.splitlines():
        if line.startswith("title: "):
            title = line[7:].strip()
            # Strip ": AppName" suffix (e.g. "Browse Contract Budgets: PASSPort")
            if ": " in title:
                title = title.split(": ")[0].strip()
            if title and not _DYNAMIC_ID_RE.search(title) and len(title) >= 5:
                return title
            break
    return ""


def extract_assertions(reasoning: str, snapshot: str) -> list[str]:
    """Pull quoted terms from LLM reasoning that also appear in the page snapshot,
    plus structural assertions (URL path, page title) that verify the replay is
    on the correct page.

    Structural assertions are prepended so they are always stored even if the
    LLM's reasoning contains few or no quoted terms.  Quoted terms from reasoning
    are appended after to capture step-specific content (e.g. selected values).

    Dynamic IDs (strings containing 5+ consecutive digits) are filtered so
    fingerprints stay stable across runs that create different records.
    """
    result: list[str] = []
    seen: set[str] = set()
    snapshot_lower = snapshot.lower()

    def _add(candidate: str) -> None:
        if is_dynamic_assertion(candidate):
            return
        c_lower = candidate.lower()
        if c_lower not in seen and c_lower in snapshot_lower:
            seen.add(c_lower)
            result.append(candidate)

    # --- Structural assertions (page identity) — always attempted first ---
    url_path = _snapshot_url_path(snapshot)
    if url_path:
        _add(url_path)

    page_title = _snapshot_title(snapshot)
    if page_title:
        _add(page_title)

    # --- Content assertions from LLM reasoning ---
    double_quoted = re.findall(r'"([^"]{3,60})"', reasoning)
    single_quoted = re.findall(r"'([^']{3,60})'", reasoning)
    for q in double_quoted + single_quoted:
        _add(q)
        if len(result) >= 8:
            break

    return result
