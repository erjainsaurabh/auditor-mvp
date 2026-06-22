"""Filesystem-backed evidence store.

Writes action logs, screenshots, and verdict JSON to:
  <output_dir>/<run_id>/<claim_id>/
    evidence.json
    <claim_id>_*.png
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class EvidenceCollector:
    run_id: str
    claim_id: str
    output_dir: Path
    produces_type: str | None = None   # artifact type declared in step YAML (produces.type)
    _actions: list[str] = field(default_factory=list, init=False)
    _screenshots: list[str] = field(default_factory=list, init=False)
    _verdict: dict | None = field(default=None, init=False)
    # "hit"  — fingerprint replayed successfully (zero LLM calls)
    # "miss" — fingerprint attempted but failed; fell back to ReAct
    # "none" — no fingerprint existed for this claim
    _fingerprint_status: str = field(default="none", init=False)
    _artifact: dict | None = field(default=None, init=False)

    @property
    def claim_dir(self) -> Path:
        d = self.output_dir / self.run_id / self.claim_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log_action(self, action: str, result: str) -> None:
        self._actions.append(f"{action} → {result}")

    def save_screenshot(self, page: Any, label: str) -> str:
        path = self.claim_dir / f"{label}.png"
        try:
            page.screenshot(path=str(path))
            self._screenshots.append(str(path))
        except Exception as exc:
            self.log_action(f"take_screenshot(label={label!r})", f"FAILED: {exc}")
            return ""
        return str(path)

    def set_fingerprint_status(self, status: str) -> None:
        """status: 'hit' | 'miss' | 'none'"""
        self._fingerprint_status = status

    def set_artifact(self, filename: str, url: str) -> None:
        """Record the artifact produced by this step (called after object-store upload)."""
        self._artifact = {
            "type": self.produces_type,
            "filename": filename,
            "url": url,
        }

    def set_verdict(self, verdict: str, confidence: str, reasoning: str) -> None:
        self._verdict = {
            "verdict": verdict,
            "confidence": confidence,
            "reasoning": reasoning,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def finalize(self) -> dict:
        record = {
            "claim_id": self.claim_id,
            "run_id": self.run_id,
            "fingerprint_status": self._fingerprint_status,
            "action_sequence": self._actions,
            "screenshots": self._screenshots,
            "artifact": self._artifact,
            **(self._verdict or {"verdict": "blocked", "confidence": "low", "reasoning": "no verdict recorded"}),
        }
        (self.claim_dir / "evidence.json").write_text(json.dumps(record, indent=2))
        return record
