"""BaseAgent — protocol that all agent implementations must satisfy.

To add a new agent type (e.g. ExtractionAgent for V1.4, VisualDiffAgent for V2):
1. Create a new module in flowprobe/agents/
2. Implement a class that satisfies this Protocol
3. Wire it into run.py

No registration, no subclassing required — structural subtyping via Protocol.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from flowprobe.loader import ConditionStatus, TestCondition


@runtime_checkable
class BaseAgent(Protocol):
    def run(
        self,
        tc: TestCondition,
        session: Any,                       # BrowserAdapter
        llm: Any,                           # LLMClient
        output_dir: Path,
        run_id: str,
        max_actions: int,
        session_data: dict[str, str] | None,
        fp_store: Any | None,               # FingerprintStore | FingerprintRouter
    ) -> tuple[ConditionStatus, list[dict], dict[str, str]]: ...
