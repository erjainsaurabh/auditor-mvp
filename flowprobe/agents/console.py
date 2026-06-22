"""Console logging helpers for the ReAct loop.

All display-only functions — no domain logic, no side effects beyond printing.
"""
from __future__ import annotations

import time
from datetime import datetime

from rich.console import Console
from rich.markup import escape

console = Console()

_TOOL_STYLE = {
    "navigate":          ("cyan",    "🌐"),
    "read_page":         ("blue",    "📄"),
    "click":             ("yellow",  "🖱 "),
    "hover":             ("yellow",  "🖱 "),
    "fill_field":        ("green",   "✏ "),
    "clear_field":       ("green",   "✏ "),
    "submit_form":       ("green",   "↩ "),
    "press_key":         ("yellow",  "⌨ "),
    "get_field_options": ("blue",    "📋"),
    "select_option":     ("green",   "▼ "),
    "take_screenshot":   ("magenta", "📸"),
    "verify_claim":      ("bold",    "⚖ "),
}


def ts() -> str:
    """Current time as HH:MM:SS string."""
    return datetime.now().strftime("%H:%M:%S")


def elapsed(since: float) -> str:
    """Seconds elapsed since *since* (from time.perf_counter)."""
    return f"{time.perf_counter() - since:.1f}s"


def log_llm_decision(step: int, name: str, args: dict) -> None:
    colour, icon = _TOOL_STYLE.get(name, ("white", "•"))
    arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    console.print(
        f"    [dim]step {step:02d}[/dim]  "
        f"[{colour}]{icon} LLM → {name}({arg_str})[/{colour}]"
    )


def log_result(name: str, result: str) -> None:
    is_error = result.startswith("error")
    colour = "red" if is_error else "dim"

    if name == "read_page":
        lines = result.splitlines()
        header_lines: list[str] = []
        body_lines: list[str] = []
        in_body = False
        for line in lines:
            if not in_body and (line.startswith("url: ") or line.startswith("title: ")):
                header_lines.append(line)
            else:
                in_body = True
                body_lines.append(line)
        body = "\n".join(body_lines)
        limit = 600
        body_display = body[:limit] + "…" if len(body) > limit else body
        for hl in header_lines:
            console.print(f"           [{colour}]{hl}[/{colour}]")
        if body_display.strip():
            console.print(f"           [{colour}]{body_display}[/{colour}]")
    else:
        page_after = ""
        main_result = result
        if "\npage_after_click:" in result:
            parts = result.split("\npage_after_click:", 1)
            main_result = parts[0]
            page_after = parts[1].strip()

        limit = 160
        display = main_result[:limit] + "…" if len(main_result) > limit else main_result
        console.print(f"           [{colour}]↳ {display}[/{colour}]")
        if page_after:
            nav_colour = "red dim" if is_error else "dim cyan"
            console.print(f"           [{nav_colour}]   ↳ page_after_click: {page_after}[/{nav_colour}]")


def log_selectors(selectors: list) -> None:
    """Log the actual DOM element that was matched — xpath, aria-label, tag."""
    if not selectors:
        return
    parts = []
    for s in selectors:
        parts.append(f"{s.type}={escape(s.value)!r}")
    console.print(f"           [dim cyan]   element: {' | '.join(parts)}[/dim cyan]")


def log_verdict(verdict: str, confidence: str, reasoning: str) -> None:
    colour = {"verified": "green", "failed": "red", "unverifiable": "yellow"}.get(verdict, "yellow")
    console.print(f"           [{colour}]verdict: {verdict} ({confidence}) [{ts()}][/{colour}]")
    console.print(f"           [dim]{reasoning[:200]}[/dim]")
