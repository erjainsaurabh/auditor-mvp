from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from auditor.evidence import EvidenceCollector
from auditor.llm_client import LLMClient
from auditor.loader import Claim, ClaimStatus
from auditor.tools import BrowserSession

console = Console()

_TOOL_STYLE = {
    "navigate":       ("cyan",   "🌐"),
    "read_page":      ("blue",   "📄"),
    "click":          ("yellow", "🖱 "),
    "hover":          ("yellow", "🖱 "),
    "fill_field":     ("green",  "✏ "),
    "clear_field":    ("green",  "✏ "),
    "submit_form":    ("green",  "↩ "),
    "get_field_options": ("blue","📋"),
    "take_screenshot":("magenta","📸"),
    "verify_claim":   ("bold",   "⚖ "),
}


def _log_llm_decision(step: int, name: str, args: dict) -> None:
    colour, icon = _TOOL_STYLE.get(name, ("white", "•"))
    arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    console.print(
        f"    [dim]step {step:02d}[/dim]  "
        f"[{colour}]{icon} LLM → {name}({arg_str})[/{colour}]"
    )


def _log_result(name: str, result: str) -> None:
    is_error = result.startswith("error")
    colour = "red" if is_error else "dim"
    limit = 300 if name == "read_page" else 120
    display = result[:limit] + "…" if len(result) > limit else result
    console.print(f"           [{colour}]↳ {display}[/{colour}]")


def _log_verdict(verdict: str, confidence: str, reasoning: str) -> None:
    colour = {"verified": "green", "failed": "red", "unverifiable": "yellow"}.get(verdict, "yellow")
    console.print(f"           [{colour}]verdict: {verdict} ({confidence})[/{colour}]")
    console.print(f"           [dim]{reasoning[:200]}[/dim]")


def run_claim(
    claim: Claim,
    session: BrowserSession,
    llm: LLMClient,
    evidence: EvidenceCollector,
    max_actions: int = 20,
    prior_url: str | None = None,
) -> ClaimStatus:
    console.print(Rule(f"[bold]{claim.id}[/bold] — {claim.description}", style="dim"))
    console.print(f"    [dim]expected:[/dim] {claim.expected}")

    setup_log = _run_setup(claim, session, evidence)

    if setup_log:
        console.print(f"    [dim]setup:[/dim] {'; '.join(setup_log)}")

    # Build context note for the LLM
    if setup_log:
        nav_context = (
            f"Setup already completed: {'; '.join(setup_log)}. "
            f"You are already on the correct page — do NOT navigate away. "
            f"Proceed directly to verifying the claim.\n"
        )
    elif prior_url:
        nav_context = (
            f"The browser is currently at: {prior_url} (carried over from the previous claim). "
            f"Navigate to {claim.navigation} if needed, then verify the claim.\n"
        )
    else:
        nav_context = f"Start by navigating to: {claim.navigation}\n"

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"Claim: {claim.description}\n"
                f"Expected outcome: {claim.expected}\n"
                + nav_context
                + "Verify this claim and call verify_claim when done."
            ),
        }
    ]

    console.print(f"    [dim]starting ReAct loop (max {max_actions} steps)…[/dim]")

    for step in range(1, max_actions + 1):
        console.print(f"    [dim]── calling LLM (step {step}/{max_actions}) …[/dim]")
        response = llm.reason(messages)
        usage = response.usage
        if usage:
            console.print(f"    [dim]   tokens: {usage.prompt_tokens} in / {usage.completion_tokens} out[/dim]")
        msg = response.choices[0].message

        if not msg.tool_calls:
            console.print("    [yellow]LLM returned no tool call — stopping loop[/yellow]")
            break

        messages.append({"role": "assistant", "content": msg.content, "tool_calls": msg.tool_calls})

        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            _log_llm_decision(step, name, args)

            result = _dispatch(name, args, claim, session, evidence)
            evidence.log_action(f"{name}({args})", result)

            _log_result(name, result)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

            if name == "verify_claim":
                evidence.set_verdict(args["verdict"], args["confidence"], args["reasoning"])
                claim.status = ClaimStatus(args["verdict"])
                _log_verdict(args["verdict"], args["confidence"], args["reasoning"])
                return claim.status

    console.print(f"    [yellow]max actions reached without verdict → blocked[/yellow]")
    claim.status = ClaimStatus.blocked
    return claim.status


def _run_setup(claim: Claim, session: BrowserSession, evidence: EvidenceCollector) -> list[str]:
    if not claim.setup and not claim.action:
        return []

    log: list[str] = []

    result = session.navigate(claim.navigation)
    evidence.log_action(f"navigate({claim.navigation})", result)
    log.append(result)

    for step in claim.setup:
        if step.fill_field:
            label = step.fill_field["label"]
            value = step.fill_field["value"]
            r = session.fill_field(label, value)
            evidence.log_action(f"fill_field({label!r}, {value!r})", r)
            log.append(r)
        elif step.click:
            r = session.click(step.click)
            evidence.log_action(f"click({step.click!r})", r)
            log.append(r)
        elif step.hover:
            r = session.hover(step.hover)
            evidence.log_action(f"hover({step.hover!r})", r)
            log.append(r)

    if claim.action == "submit_form":
        r = session.submit_form()
        evidence.log_action("submit_form()", r)
        log.append(r)

    return log


def _dispatch(
    name: str,
    args: dict[str, Any],
    claim: Claim,
    session: BrowserSession,
    evidence: EvidenceCollector,
) -> str:
    match name:
        case "navigate":
            return session.navigate(args["target"])
        case "read_page":
            return session.read_page()
        case "click":
            return session.click(args["element_description"])
        case "hover":
            return session.hover(args["element_description"])
        case "fill_field":
            return session.fill_field(args["field_label"], args["value"])
        case "clear_field":
            return session.clear_field(args["field_label"])
        case "submit_form":
            return session.submit_form()
        case "get_field_options":
            return session.get_field_options(args["field_label"])
        case "take_screenshot":
            return evidence.save_screenshot(session.page, f"{claim.id}_{args['label']}")
        case "verify_claim":
            return "verdict recorded"
        case _:
            return f"unknown tool: {name}"
