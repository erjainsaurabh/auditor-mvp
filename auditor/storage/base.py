"""EvidenceStore — protocol for evidence persistence backends.

Any class that implements these methods satisfies the protocol via structural
subtyping (no explicit registration needed).

Current implementations:
  auditor.storage.filesystem.EvidenceCollector  — writes to local filesystem

Future:
  auditor.storage.postgres.PostgresEvidenceStore — V2
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EvidenceStore(Protocol):
    def log_action(self, action: str, result: str) -> None: ...
    def save_screenshot(self, page: Any, label: str) -> str: ...
    def set_verdict(self, verdict: str, confidence: str, reasoning: str) -> None: ...
    def set_fingerprint_status(self, status: str) -> None: ...
    def finalize(self) -> dict: ...
