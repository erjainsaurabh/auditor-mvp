from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.rule import Rule

from auditor.evidence import EvidenceCollector
from auditor.graph import build_claim_graph, cascade_claim_failure, claim_execution_order, mark_claims_blocked
from auditor.llm_client import LLMClient
from auditor.loader import Claim, ClaimStatus, OutputCapture, Step, StepStatus
from auditor.tools import BrowserSession

console = Console()

_TOOL_STYLE = {
    "navigate":          ("cyan",    "🌐"),
    "read_page":         ("blue",    "📄"),
    "click":             ("yellow",  "🖱 "),
    "hover":             ("yellow",  "🖱 "),
    "fill_field":        ("green",   "✏ "),
    "clear_field":       ("green",   "✏ "),
    "submit_form":       ("green",   "↩ "),
    "get_field_options": ("blue",    "📋"),
    "take_screenshot":   ("magenta", "📸"),
    "verify_claim":      ("bold",    "⚖ "),
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


def _prune_stale_read_pages(messages: list[dict], latest_id: str, read_page_ids: list[str]) -> None:
    """Replace old read_page results with a placeholder — only the latest snapshot is needed."""
    for msg in messages:
        if (
            msg.get("role") == "tool"
            and msg.get("tool_call_id") in read_page_ids
            and msg.get("tool_call_id") != latest_id
        ):
            msg["content"] = "[page snapshot removed — superseded by later read_page]"


def _execute_captures(captures: list[OutputCapture], session: BrowserSession) -> dict[str, str]:
    result: dict[str, str] = {}
    for cap in captures:
        if cap.strategy == "current_url":
            result[cap.key] = session.current_url()
        elif cap.strategy == "page_title":
            try:
                result[cap.key] = session._page.title()
            except Exception:
                result[cap.key] = ""
        elif cap.strategy.startswith("url_segment:"):
            n = int(cap.strategy.split(":")[1])
            parts = [p for p in session.current_url().split("/") if p]
            result[cap.key] = parts[n] if n < len(parts) else ""
    return result


def run_step(
    step: Step,
    session: BrowserSession,
    llm: LLMClient,
    output_dir: Path,
    run_id: str,
    max_actions: int,
    session_data: dict[str, str] | None = None,
) -> tuple[StepStatus, list[dict], dict[str, str]]:
    session_data = session_data or {}
    console.print(Rule(f"[bold blue]{step.id}[/bold blue] — {step.goal}", style="blue"))

    # Log any session data this step is receiving
    available = {k: v for k, v in session_data.items() if k in step.input}
    if available:
        for k, v in available.items():
            console.print(f"    [dim]session_data:[/dim] {k} = {v}")

    claim_graph = build_claim_graph(step.claims)
    order = claim_execution_order(claim_graph)
    claim_map = step.claim_map

    messages: list[dict[str, Any]] = []
    read_page_ids: list[str] = []
    evidence_records: list[dict] = []
    first_claim = True

    for claim_id in order:
        claim = claim_map[claim_id]

        if claim.status == ClaimStatus.blocked:
            console.print(f"    [yellow]⊘[/yellow] {claim_id} — blocked")
            continue

        console.print(Rule(f"[bold]{claim_id}[/bold] — {claim.description}", style="dim"))
        console.print(f"    [dim]expected:[/dim] {claim.expected}")

        ev = EvidenceCollector(run_id=run_id, claim_id=claim_id, output_dir=output_dir)

        setup_log = _run_setup(claim, session, ev)
        if setup_log:
            console.print(f"    [dim]setup:[/dim] {'; '.join(setup_log)}")

        # Build the user message for this claim, continuing the shared conversation
        if setup_log:
            nav_context = (
                f"Setup already completed: {'; '.join(setup_log)}. "
                f"You are already on the correct page — do NOT navigate away. "
                f"Proceed directly to verifying the claim.\n"
            )
        elif first_claim:
            if available:
                data_str = "\n".join(f"  {k}: {v}" for k, v in available.items())
                nav_context = (
                    f"Session data from previous steps:\n{data_str}\n"
                    f"Use this data to navigate directly if relevant, "
                    f"otherwise start at: {claim.navigation}\n"
                )
            else:
                nav_context = f"Start by navigating to: {claim.navigation}\n"
        else:
            nav_context = (
                f"You are continuing from the previous verification. "
                f"Navigate to {claim.navigation} if needed, then verify this claim.\n"
            )

        messages.append({
            "role": "user",
            "content": (
                f"Claim: {claim.description}\n"
                f"Expected outcome: {claim.expected}\n"
                + nav_context
                + "Verify this claim and call verify_claim when done."
            ),
        })

        first_claim = False
        console.print(f"    [dim]starting ReAct loop (max {max_actions} steps)…[/dim]")

        status = _react_loop(
            claim=claim,
            session=session,
            llm=llm,
            evidence=ev,
            messages=messages,
            read_page_ids=read_page_ids,
            max_actions=max_actions,
        )

        record = ev.finalize()
        evidence_records.append(record)

        icon = {"verified": "[green]✓[/green]", "failed": "[red]✗[/red]"}.get(str(status), "[yellow]⊘[/yellow]")
        console.print(f"    {icon} {claim_id} — {status}")

        if status in (ClaimStatus.failed, ClaimStatus.blocked):
            blocked = cascade_claim_failure(claim_graph, claim_id)
            mark_claims_blocked(step.claims, set(blocked))
            if blocked:
                console.print(f"    [dim]cascading block to: {', '.join(blocked)}[/dim]")

    # Determine step status
    statuses = [claim_map[cid].status for cid in order]
    if any(s == ClaimStatus.failed for s in statuses):
        step.status = StepStatus.failed
    elif all(s in (ClaimStatus.verified, ClaimStatus.unverifiable) for s in statuses):
        step.status = StepStatus.verified
    else:
        step.status = StepStatus.failed

    # Run output captures — only if step passed
    captured: dict[str, str] = {}
    if step.status == StepStatus.verified and step.output_capture:
        captured = _execute_captures(step.output_capture, session)
        session_data.update(captured)
        for k, v in captured.items():
            console.print(f"    [dim]captured:[/dim] {k} = {v}")

    return step.status, evidence_records, session_data


def _react_loop(
    claim: Claim,
    session: BrowserSession,
    llm: LLMClient,
    evidence: EvidenceCollector,
    messages: list[dict[str, Any]],
    read_page_ids: list[str],
    max_actions: int,
) -> ClaimStatus:
    for step_num in range(1, max_actions + 1):
        console.print(f"    [dim]── calling LLM (step {step_num}/{max_actions}) …[/dim]")
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

            _log_llm_decision(step_num, name, args)

            result = _dispatch(name, args, claim, session, evidence)
            evidence.log_action(f"{name}({args})", result)

            _log_result(name, result)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

            if name == "read_page":
                read_page_ids.append(tool_call.id)
                _prune_stale_read_pages(messages, tool_call.id, read_page_ids)

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
