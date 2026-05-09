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


class ClaimFingerprint(BaseModel):
    claim_id: str
    recorded_at: str
    run_id: str
    verdict: str
    confidence: str
    actions: list[ActionRecord] = Field(default_factory=list)
    setup_records: list[ActionRecord] = Field(default_factory=list)


class FingerprintStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._store: dict[str, ClaimFingerprint] = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
            for claim_id, data in (raw.get("claims") or {}).items():
                try:
                    self._store[claim_id] = ClaimFingerprint.model_validate(data)
                except Exception:
                    pass  # skip corrupt entries

    def get(self, claim_id: str) -> ClaimFingerprint | None:
        return self._store.get(claim_id)

    def record(self, fp: ClaimFingerprint) -> None:
        self._store[fp.claim_id] = fp

    def save(self) -> None:
        data = {
            "claims": {
                cid: fp.model_dump()
                for cid, fp in self._store.items()
            }
        }
        self._path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
        )


class FingerprintRouter:
    """Routes fingerprint reads across all stores, writes to the correct per-YAML store."""

    def __init__(self, stores: list[tuple[FingerprintStore, set[str]]]) -> None:
        # stores: list of (store, {claim_ids that belong to it})
        self._stores = stores
        self._id_to_store: dict[str, FingerprintStore] = {}
        for store, claim_ids in stores:
            for cid in claim_ids:
                self._id_to_store[cid] = store

    def get(self, claim_id: str) -> ClaimFingerprint | None:
        store = self._id_to_store.get(claim_id)
        return store.get(claim_id) if store else None

    def record(self, fp: ClaimFingerprint) -> None:
        store = self._id_to_store.get(fp.claim_id)
        if store:
            store.record(fp)
            store.save()

    def save(self) -> None:
        seen: set[int] = set()
        for store, _ in self._stores:
            if id(store) not in seen:
                store.save()
                seen.add(id(store))


def extract_assertions(reasoning: str, snapshot: str) -> list[str]:
    """Pull quoted terms from LLM reasoning that also appear in the page snapshot."""
    quoted = re.findall(r'"([^"]{3,60})"', reasoning)
    snapshot_lower = snapshot.lower()
    seen: set[str] = set()
    result: list[str] = []
    for q in quoted:
        if q not in seen and q.lower() in snapshot_lower:
            seen.add(q)
            result.append(q)
        if len(result) >= 4:
            break
    return result
