from __future__ import annotations

from pathlib import Path

import yaml

# Default trial order when no stats are available yet.
# Mirrors the original hardcoded waterfall (duplicate tab already removed here).
CLICK_DEFAULT_ORDER: list[str] = [
    "option",
    "tab",
    "button",
    "link",
    "text_exact",
    "text_desc",
    "text_fuzzy",
    "aria_label",
    "title",
]

FILL_DEFAULT_ORDER: list[str] = [
    "label_exact",
    "label_fuzzy",
    "placeholder",
    "aria_label",
    "name_id",
]

_DEFAULT_ORDERS: dict[str, list[str]] = {
    "click":      CLICK_DEFAULT_ORDER,
    "fill_field": FILL_DEFAULT_ORDER,
}


class StrategyStats:
    """Tracks per-strategy win rates across all elements for a given platform.

    For each tool (click / fill_field) and each strategy key (button, tab, …)
    we record:
      - ``tried``:  how many times the strategy was attempted
      - ``wins``:   how many times it was the FIRST strategy to succeed

    win_rate = wins / tried

    At the start of each click call the strategy list is sorted by win_rate
    descending so the historically-best strategy is tried first.  Ties
    (including cold-start where tried=0) preserve the original default order.

    File format (strategy_stats.yaml):
        platform: ivalua
        stats:
          click:
            button:   {tried: 60, wins: 47}
            tab:      {tried: 55, wins:  8}
            option:   {tried: 60, wins:  1}
            ...
          fill_field:
            label_exact: {tried: 40, wins: 31}
            ...
    """

    def __init__(self, path: Path, platform: str = "generic") -> None:
        self._path = path
        self._platform = platform
        # tool → strategy_key → {"tried": int, "wins": int}
        self._data: dict[str, dict[str, dict[str, int]]] = {}
        self._dirty = False
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
            for tool, keys in (raw.get("stats") or {}).items():
                self._data[tool] = {}
                for key, counts in (keys or {}).items():
                    self._data[tool][key] = {
                        "tried": int((counts or {}).get("tried", 0)),
                        "wins":  int((counts or {}).get("wins",  0)),
                    }

    # ── write ──────────────────────────────────────────────────────────────

    def _ensure(self, tool: str, key: str) -> None:
        self._data.setdefault(tool, {}).setdefault(key, {"tried": 0, "wins": 0})

    def record_tried(self, tool: str, strategy_key: str) -> None:
        """Call every time a strategy is attempted (before success/failure known)."""
        self._ensure(tool, strategy_key)
        self._data[tool][strategy_key]["tried"] += 1
        self._dirty = True

    def record_win(self, tool: str, strategy_key: str) -> None:
        """Call when this strategy was the one that succeeded."""
        self._ensure(tool, strategy_key)
        self._data[tool][strategy_key]["wins"] += 1
        self._dirty = True
        self.save()   # persist immediately — crash-safe

    # ── read ───────────────────────────────────────────────────────────────

    def win_rate(self, tool: str, strategy_key: str) -> float:
        counts = self._data.get(tool, {}).get(strategy_key, {})
        tried = counts.get("tried", 0)
        wins  = counts.get("wins",  0)
        return wins / tried if tried > 0 else 0.0

    def sorted_keys(self, tool: str) -> list[str]:
        """Return strategy keys for *tool* sorted by win_rate descending.

        Keys with no data keep their default relative order (cold-start safe).
        """
        default = _DEFAULT_ORDERS.get(tool, [])

        def _sort_key(k: str) -> tuple[float, int]:
            rate = self.win_rate(tool, k)
            try:
                pos = default.index(k)
            except ValueError:
                pos = len(default)
            return (-rate, pos)   # desc win_rate, then default order as tiebreaker

        return sorted(default, key=_sort_key)

    # ── persistence ────────────────────────────────────────────────────────

    def save(self, force: bool = False) -> None:
        """Write stats to disk.

        *force=True* creates the file even when no new data was recorded this
        run — used at end-of-run so the file always exists after the first
        execution (e.g. when all steps hit fingerprint replay and no live
        strategy calls were made).
        """
        if not self._dirty and not force:
            return
        data = {
            "platform": self._platform,
            "stats": {
                tool: {
                    key: counts
                    for key, counts in sorted(tool_data.items())
                }
                for tool, tool_data in sorted(self._data.items())
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
        )
        self._dirty = False
        print(f"           [strategy_stats] saved to {self._path.resolve()}")

    # ── diagnostics ────────────────────────────────────────────────────────

    def summary(self, tool: str = "click") -> str:
        """One-line per strategy showing win rate, sorted by priority."""
        lines: list[str] = []
        for key in self.sorted_keys(tool):
            counts = self._data.get(tool, {}).get(key, {"tried": 0, "wins": 0})
            rate   = self.win_rate(tool, key)
            lines.append(
                f"  {key:15s}  wins={counts['wins']:3d}  "
                f"tried={counts['tried']:3d}  rate={rate:.0%}"
            )
        return f"strategy order ({tool}):\n" + "\n".join(lines)
