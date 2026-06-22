"""Pattern Inventory — machine-learned action priors per (platform, step_type, verb).

Records which interaction tool sequences succeeded for a given aria role pattern,
keyed by platform + step_type + verb extracted from the step description.

At query time, given a new step with no fingerprint, the inventory:
  1. Matches by (platform, step_type, verb)
  2. Scans the current aria snapshot for known roles
  3. Returns the interaction_sequence for the best matching observation

This sits between human hints and fingerprint replay in the execution lifecycle:
  hints (human, static) → pattern inventory (machine, dynamic) → fingerprint (exact replay)
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

# Roles that appear in aria snapshots — ordered by specificity
_ARIA_ROLE_RE = re.compile(
    r"^\s*-\s+(\w[\w\-]*)\s",   # matches "- combobox [name=...]" lines
    re.MULTILINE,
)

# Common English stopwords to drop when extracting verb from description
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "to", "of", "in", "on", "at", "by", "for", "with", "about", "from",
    "into", "through", "during", "that", "this", "these", "those",
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class AriaObservation(BaseModel):
    role: str                              # e.g. "combobox", "textbox", "grid"
    interaction_sequence: list[str]        # tools used after this role was seen, e.g. ["click", "select_option"]
    success_count: int = 1


class PatternEntry(BaseModel):
    platform: str
    step_type: str                         # StepType value: "behavioral", "existence", etc.
    verb: str                              # first meaningful verb from step description
    observations: list[AriaObservation] = Field(default_factory=list)


class PatternInventory:
    """Loads, queries, records, and saves the pattern inventory YAML."""

    MIN_SUCCESS_COUNT = 3   # minimum observations before injecting as a suggestion

    def __init__(self, path: Path, platform: str = "generic") -> None:
        self._path = path
        self._platform = platform
        self._entries: list[PatternEntry] = []
        self._dirty = False
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
            for e in raw.get("entries", []):
                try:
                    self._entries.append(PatternEntry.model_validate(e))
                except Exception:
                    pass

    # ── write ──────────────────────────────────────────────────────────────

    def record(
        self,
        step_type: str,
        description: str,
        action_records: list[Any],         # list[ActionRecord] from fingerprint.py
        last_snapshot: str,
    ) -> None:
        """Record a successful interaction sequence into the inventory.

        Extracts verb from description, aria roles from last_snapshot,
        and the interaction tools (excluding navigate + read_page) from action_records.
        """
        verb = _extract_verb(description)
        if not verb:
            return

        interaction_seq = _extract_interaction_sequence(action_records)
        if not interaction_seq:
            return

        roles = _extract_roles(last_snapshot)

        entry = self._find_entry(step_type, verb)
        if entry is None:
            entry = PatternEntry(
                platform=self._platform,
                step_type=step_type,
                verb=verb,
            )
            self._entries.append(entry)

        for role in roles if roles else [""]:
            obs = _find_observation(entry, role)
            if obs is None:
                entry.observations.append(AriaObservation(
                    role=role,
                    interaction_sequence=interaction_seq,
                    success_count=1,
                ))
            else:
                # Merge: update sequence if new one is different, always bump count
                obs.interaction_sequence = interaction_seq
                obs.success_count += 1

        self._dirty = True

    # ── read ───────────────────────────────────────────────────────────────

    def query(
        self,
        step_type: str,
        description: str,
        current_snapshot: str = "",
    ) -> str:
        """Return a suggestion string to inject into the LLM message, or "" if no match.

        Only returns a suggestion when success_count >= MIN_SUCCESS_COUNT.
        """
        verb = _extract_verb(description)
        if not verb:
            return ""

        entry = self._find_entry(step_type, verb)
        if entry is None:
            return ""

        # Try to match an observation by role found in snapshot
        best: AriaObservation | None = None
        if current_snapshot:
            roles_in_snap = _extract_roles(current_snapshot)
            for obs in entry.observations:
                if obs.role in roles_in_snap and obs.success_count >= self.MIN_SUCCESS_COUNT:
                    if best is None or obs.success_count > best.success_count:
                        best = obs

        # Fall back to highest-count observation regardless of role
        if best is None:
            candidates = [o for o in entry.observations if o.success_count >= self.MIN_SUCCESS_COUNT]
            if candidates:
                best = max(candidates, key=lambda o: o.success_count)

        if best is None:
            return ""

        seq_str = " → ".join(best.interaction_sequence)
        role_hint = f" (seen: {best.role})" if best.role else ""
        return (
            f"Based on {best.success_count} similar past steps on this platform{role_hint}, "
            f"suggested interaction sequence: {seq_str}\n"
        )

    # ── persistence ────────────────────────────────────────────────────────

    def save(self) -> None:
        if not self._dirty:
            return
        data: dict[str, Any] = {
            "platform": self._platform,
            "updated": date.today().isoformat(),
            "entries": [e.model_dump() for e in self._entries],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
        )
        self._dirty = False
        print(f"           [pattern_inventory] saved to {self._path.resolve()}")

    # ── internal ───────────────────────────────────────────────────────────

    def _find_entry(self, step_type: str, verb: str) -> PatternEntry | None:
        for e in self._entries:
            if e.platform == self._platform and e.step_type == step_type and e.verb == verb:
                return e
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_verb(description: str) -> str:
    """Return a compound verb:noun key from the description.

    Extracts the first meaningful verb and the first meaningful noun that follows
    it, giving a more specific key than the verb alone.
    Examples:
      "go to the top navigation and select requests" → "go:requests"
      "search for campaign and select two campaigns"  → "search:campaign"
      "click excel extract button"                   → "click:excel"
      "Login to the application"                     → "login:application"
    """
    _NOUN_STOPWORDS = _STOPWORDS | {
        "page", "application", "button", "field", "form", "dropdown",
        "menu", "item", "list", "section", "tab", "link", "navigation",
        "top", "bottom", "left", "right", "open", "close", "select",
        "and", "or", "then", "also", "all", "any", "sub", "new",
    }
    tokens = [re.sub(r"[^a-z]", "", t) for t in re.split(r"[\s\-_,]+", description.lower())]
    tokens = [t for t in tokens if t]

    verb = ""
    for t in tokens:
        if t not in _STOPWORDS:
            verb = t
            break
    if not verb:
        return ""

    noun = ""
    past_verb = False
    for t in tokens:
        if t == verb:
            past_verb = True
            continue
        if past_verb and t not in _NOUN_STOPWORDS and len(t) > 2:
            noun = t
            break

    return f"{verb}:{noun}" if noun else verb


def _extract_roles(snapshot: str) -> list[str]:
    """Extract distinct aria roles from a snapshot string."""
    seen: list[str] = []
    for m in _ARIA_ROLE_RE.finditer(snapshot):
        role = m.group(1).lower()
        if role not in seen:
            seen.append(role)
    return seen


def _extract_interaction_sequence(action_records: list[Any]) -> list[str]:
    """Return the minimal causal interaction sequence from action_records.

    Rules:
    - Exclude read_page, take_screenshot, verify_claim (observational/terminal)
    - Include navigate — it is a valid winning action
    - Deduplicate consecutive identical tool names (keep one representative)
    - Start from the last navigate if present, otherwise from the first action
      (the last navigate defines where the agent actually got to before interacting)
    """
    _exclude = {"read_page", "take_screenshot", "verify_claim"}
    tools = [ar.tool for ar in action_records if ar.tool not in _exclude]

    if not tools:
        return []

    # Find index of last navigate — that's the effective starting point
    last_nav_idx = -1
    for i, t in enumerate(tools):
        if t == "navigate":
            last_nav_idx = i

    # Keep from last navigate onwards (inclusive), or all if no navigate
    causal = tools[last_nav_idx:] if last_nav_idx >= 0 else tools

    # Deduplicate consecutive identical tools
    deduped: list[str] = []
    for t in causal:
        if not deduped or deduped[-1] != t:
            deduped.append(t)

    return deduped


def _find_observation(entry: PatternEntry, role: str) -> AriaObservation | None:
    for obs in entry.observations:
        if obs.role == role:
            return obs
    return None
