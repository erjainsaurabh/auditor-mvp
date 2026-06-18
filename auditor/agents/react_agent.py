"""ReactAgent — ReAct (Reason → Act → Observe) loop implementation.

One agent instance drives one TestCondition end to end:
  1. For each Step in topological order:
     a. Try fingerprint replay (Tier 1 — zero LLM calls)
     b. Fall back to full ReAct loop (Tier 2 — LLM-driven)
  2. Record fingerprint on first successful ReAct run
  3. Cascade failures to dependent steps
  4. Return (ConditionStatus, evidence_records, updated_session_data)
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.rule import Rule

from auditor.agents.console import (
    console,
    elapsed,
    log_llm_decision,
    log_result,
    log_selectors,
    log_verdict,
    ts,
)
from auditor.agents.dispatch import dispatch
from auditor.agents.replay import FingerprintReplayer
from auditor.agents.session_utils import (
    execute_captures,
    prune_stale_read_pages,
    snap_from_messages,
    templatize_actions,
)
from auditor.fingerprint import (
    ActionRecord,
    FingerprintRouter,
    FingerprintStore,
    SelectorRecord,
    StepFingerprint,
    extract_assertions,
    step_definition_hash,
)
from auditor.graph import build_step_graph, cascade_step_failure, mark_steps_blocked, step_execution_order
from auditor.llm_client import LLMClient
from auditor.loader import ConditionStatus, Step, StepStatus, TestCondition
from auditor.storage.filesystem import EvidenceCollector

_replayer = FingerprintReplayer()


# ---------------------------------------------------------------------------
# Public entry point (functional API — used by run.py and ReactAgent.run)
# ---------------------------------------------------------------------------

def run_test_condition(
    tc: TestCondition,
    session: Any,
    llm: LLMClient,
    output_dir: Path,
    run_id: str,
    max_actions: int,
    session_data: dict[str, str] | None = None,
    fp_store: FingerprintStore | FingerprintRouter | None = None,
) -> tuple[ConditionStatus, list[dict], dict[str, str]]:
    session_data = session_data or {}
    console.print(Rule(f"[bold blue]{tc.id}[/bold blue] — {tc.goal}", style="blue"))

    available = {k: v for k, v in session_data.items() if k in tc.execution.input}
    if available:
        for k, v in available.items():
            console.print(f"    [dim]session_data:[/dim] {k} = {v}")

    step_graph = build_step_graph(tc.steps)
    order = step_execution_order(step_graph)
    step_map = tc.step_map

    messages: list[dict[str, Any]] = []
    read_page_ids: list[str] = []
    evidence_records: list[dict] = []
    first_step = True

    session._last_interacted_label = ""

    for step_id in order:
        step = step_map[step_id]

        if step.status == StepStatus.blocked:
            console.print(f"    [yellow]⊘[/yellow] {step_id} — blocked")
            continue

        console.print(Rule(f"[bold]{step_id}[/bold] — {step.description} [dim][{ts()}][/dim]", style="dim"))
        console.print(f"    [dim]expected:[/dim] {step.expected}")

        ev = EvidenceCollector(run_id=run_id, claim_id=step_id, output_dir=output_dir)

        fp_for_step = fp_store.get(step.id, step.source_file) if fp_store else None
        if fp_for_step and fp_for_step.step_hash:
            current_hash = step_definition_hash(
                step.description,
                step.expected,
                getattr(step, "navigation", ""),
                list(step.data.keys()),
            )
            if fp_for_step.step_hash != current_hash:
                console.print(
                    f"    [yellow]⚠ fingerprint stale (YAML changed) — hash "
                    f"{fp_for_step.step_hash} → {current_hash} — running ReAct[/yellow]"
                )
                fp_for_step = None

        is_continue = tc.execution.navigation_mode == "continue"
        nav_context = _build_nav_context(
            step=step,
            tc=tc,
            session=session,
            session_data=session_data,
            available=available,
            first_step=first_step,
            is_continue=is_continue,
        )

        hints_context = ""
        if step.hints:
            hints_lines = "\n".join(f"  {i+1}. {h}" for i, h in enumerate(step.hints))
            hints_context = f"IMPORTANT — follow these steps exactly:\n{hints_lines}\n"

        data_context = ""
        if step.data:
            _sensitive = re.compile(r"password|passwd|secret|token|credential", re.IGNORECASE)
            data_lines = "\n".join(
                f"  {k}: [sensitive — pass placeholder {{{{{k}}}}} as the value]"
                if _sensitive.search(k) else f"  {k}: {v}"
                for k, v in step.data.items()
            )
            override_note = (
                " Any example values shown in the steps above are illustrations only — "
                "use the exact values from this data block instead."
                if step.hints else ""
            )
            data_context = f"Test data (use these exact values).{override_note}\n{data_lines}\n"

        messages.append({
            "role": "user",
            "content": (
                f"Claim: {step.description}\n"
                f"Expected outcome: {step.expected}\n"
                + hints_context
                + data_context
                + nav_context
                + "Verify this claim and call verify_claim when done."
            ),
        })

        first_step = False
        console.print(f"    [dim]starting ReAct loop (max {max_actions} steps)… [{ts()}][/dim]")

        step_start = time.perf_counter()
        snap: dict[str, str] = {}
        status = _react_loop(
            step=step,
            session=session,
            llm=llm,
            evidence=ev,
            messages=messages,
            read_page_ids=read_page_ids,
            max_actions=max_actions,
            snap=snap,
            fp_store=fp_store,
            run_id=run_id,
            fp_for_step=fp_for_step,
            session_data=session_data,
            skip_navigate=is_continue,
        )
        live_url = session.current_url()
        session_data["_last_url"] = live_url or snap.get("url", "")
        session_data["_last_title"] = snap.get("title", "") or session._page.title()

        record = ev.finalize()
        evidence_records.append(record)

        icon = {"verified": "[green]✓[/green]", "failed": "[red]✗[/red]"}.get(status.value, "[yellow]⊘[/yellow]")
        console.print(f"    {icon} {step_id} — {status.value} [dim]({elapsed(step_start)})[/dim]")

        if status in (StepStatus.failed, StepStatus.blocked):
            blocked = cascade_step_failure(step_graph, step_id)
            mark_steps_blocked(tc.steps, set(blocked))
            if blocked:
                console.print(f"    [dim]cascading block to: {', '.join(blocked)}[/dim]")

    # Determine test condition status
    statuses = [step_map[sid].status for sid in order]
    if any(s == StepStatus.failed for s in statuses):
        tc.status = ConditionStatus.failed
    elif all(s in (StepStatus.verified, StepStatus.unverifiable) for s in statuses):
        tc.status = ConditionStatus.verified
    else:
        tc.status = ConditionStatus.failed

    # Run output captures — only if test condition passed
    captured: dict[str, str] = {}
    if tc.status == ConditionStatus.verified and tc.execution.output_capture:
        captured = execute_captures(tc.execution.output_capture, session_data)
        session_data.update(captured)
        for k, v in captured.items():
            console.print(f"    [dim]captured:[/dim] {k} = {v}")

    return tc.status, evidence_records, session_data


# ---------------------------------------------------------------------------
# ReactAgent class — implements BaseAgent protocol
# ---------------------------------------------------------------------------

class ReactAgent:
    """Thin class wrapper around run_test_condition for protocol conformance."""

    def run(
        self,
        tc: TestCondition,
        session: Any,
        llm: LLMClient,
        output_dir: Path,
        run_id: str,
        max_actions: int,
        session_data: dict[str, str] | None = None,
        fp_store: Any | None = None,
    ) -> tuple[ConditionStatus, list[dict], dict[str, str]]:
        return run_test_condition(
            tc=tc,
            session=session,
            llm=llm,
            output_dir=output_dir,
            run_id=run_id,
            max_actions=max_actions,
            session_data=session_data,
            fp_store=fp_store,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_nav_context(
    step: Step,
    tc: TestCondition,
    session: Any,
    session_data: dict[str, str],
    available: dict[str, str],
    first_step: bool,
    is_continue: bool,
) -> str:
    if not first_step:
        current_url = session.current_url()
        return (
            f"You are continuing from the previous step. "
            f"Browser is currently at: {current_url}. "
            f"The page state from the previous step is preserved — do NOT navigate away. "
            f"Proceed directly with verifying this claim from the current page.\n"
        )

    if is_continue:
        last_url = session_data.get("_last_url", "")
        last_title = session_data.get("_last_title", "")
        title_hint = f" ({last_title})" if last_title else ""
        return (
            f"Browser is currently at: {last_url}{title_hint}. "
            f"This test condition continues from the previous one — "
            f"do NOT navigate away. The page state is already correct.\n"
        )

    if available:
        data_str = "\n".join(f"  {k}: {v}" for k, v in available.items())
        nav_hint = f"\n  start at: {tc.execution.navigation}" if tc.execution.navigation else ""
        current_browser = session_data.get("_last_url", "")
        if current_browser:
            return (
                f"Browser is currently at: {current_browser}. "
                f"Session data from previous steps:\n{data_str}\n"
                f"IMPORTANT: If the browser is already on the target page, "
                f"do NOT navigate — proceed directly with the claim. "
                f"Only navigate if the current page is clearly wrong."
                f"{nav_hint}\n"
            )
        return (
            f"Session data from previous steps:\n{data_str}\n"
            f"Use this data to navigate directly if relevant"
            f"{nav_hint}\n"
        )

    if tc.depends_on and session_data.get("_last_url"):
        last_url = session_data["_last_url"]
        last_title = session_data.get("_last_title", "")
        title_hint = f" ({last_title})" if last_title else ""
        return (
            f"Browser is currently at: {last_url}{title_hint}. "
            f"Page state from the previous test condition is preserved — do NOT navigate away "
            f"unless this is the wrong page. "
            + (f"Navigation hint if needed: {tc.execution.navigation}\n" if tc.execution.navigation else "\n")
        )

    return (
        f"Start by navigating to: {tc.execution.navigation}\n"
        if tc.execution.navigation else
        "Navigate to the appropriate page to begin verification.\n"
    )


def _react_loop(
    step: Step,
    session: Any,
    llm: LLMClient,
    evidence: EvidenceCollector,
    messages: list[dict[str, Any]],
    read_page_ids: list[str],
    max_actions: int,
    snap: dict[str, str] | None = None,
    fp_store: FingerprintStore | FingerprintRouter | None = None,
    run_id: str = "",
    fp_for_step: StepFingerprint | None = None,
    session_data: dict[str, str] | None = None,
    skip_navigate: bool = False,
) -> StepStatus:
    # --- Tier 1: fingerprint replay (zero LLM calls on stable UI) ---
    if fp_store and fp_for_step and fp_for_step.actions:
        console.print(f"    [dim cyan]⚡ trying fingerprint replay… [{ts()}][/dim cyan]")
        replay_snap: dict[str, str] = snap if snap is not None else {}
        status = _replayer.try_replay(
            step, session, evidence, fp_for_step, replay_snap, session_data, skip_navigate=skip_navigate
        )
        if snap is not None:
            snap.update(replay_snap)
        if status is not None:
            evidence.set_fingerprint_status("hit")
            current_url = session.current_url()
            messages.append({
                "role": "assistant",
                "content": (
                    f"Step {step.id} {status} (fingerprint replay). "
                    f"Browser is currently at: {current_url}"
                ),
            })
            console.print(f"    [dim cyan]⚡ replay succeeded — no LLM call [{ts()}][/dim cyan]")
            return status
        evidence.set_fingerprint_status("miss")
        console.print(f"    [dim yellow]replay failed → falling back to ReAct[/dim yellow]")
        step.status = StepStatus.blocked

    # --- Tier 2: full ReAct loop ---
    action_records: list[ActionRecord] = []
    last_snapshot = ""
    _failed_attempts: dict[str, int] = {}

    def _attempt_key(tool_name: str, tool_args: dict) -> str | None:
        if tool_name == "click":
            return f"click:{tool_args.get('element_description', '')}"
        if tool_name == "hover":
            return f"hover:{tool_args.get('element_description', '')}"
        if tool_name == "fill_field":
            return f"fill:{tool_args.get('field_label', '')}"
        if tool_name == "navigate":
            return f"navigate:{tool_args.get('target', '')}"
        return None

    def _retry_hint(tool_name: str, tool_args: dict, count: int) -> str:
        desc = (
            tool_args.get("element_description")
            or tool_args.get("field_label")
            or tool_args.get("target", "")
        )
        if tool_name == "click":
            alts = (
                "• Try 'Save and Close' instead of 'Save'\n"
                "• Use a more specific description (e.g. the button's aria label)\n"
                "• Try submit_form if this is a form submission\n"
                "• Call verify_claim(unverifiable) if the action cannot be completed"
            )
        elif tool_name == "fill_field":
            alts = (
                "• Try clear_field first, then fill again\n"
                "• Check the exact field label (include asterisk if mandatory)\n"
                "• Call verify_claim(unverifiable) if the field cannot be filled"
            )
        elif tool_name == "navigate":
            alts = (
                "• Call read_page to check the current page state\n"
                "• Try a relative path instead of an absolute URL"
            )
        else:
            alts = "• Try a different approach or call verify_claim(unverifiable)"

        if count >= 3:
            return (
                f"\n\n⛔ STOP: '{desc}' has failed {count} times with the same approach. "
                f"Do NOT retry it again — it will not work. "
                f"Call verify_claim with verdict='unverifiable' and explain why the action could not be completed."
            )
        return (
            f"\n\n⚠ REPEATED FAILURE ({count}×): '{desc}' has already failed with this exact approach. "
            f"You MUST try a different strategy:\n{alts}"
        )

    for step_num in range(1, max_actions + 1):
        console.print(f"    [dim]── calling LLM (step {step_num}/{max_actions}) … [{ts()}][/dim]")
        llm_start = time.perf_counter()
        response = llm.reason(messages)
        usage = response.usage
        if usage:
            console.print(f"    [dim]   tokens: {usage.prompt_tokens} in / {usage.completion_tokens} out ({elapsed(llm_start)})[/dim]")
        msg = response.choices[0].message

        if not msg.tool_calls:
            console.print("    [yellow]LLM returned no tool call — stopping loop[/yellow]")
            break

        messages.append({"role": "assistant", "content": msg.content, "tool_calls": msg.tool_calls})

        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            log_llm_decision(step_num, name, args)

            action_start = time.perf_counter()
            result = dispatch(name, args, step, session, evidence)
            action_elapsed = time.perf_counter() - action_start

            # Failure deduplication — escalating hints after repeated identical failures
            akey = _attempt_key(name, args)
            if akey and result.startswith("error"):
                _failed_attempts[akey] = _failed_attempts.get(akey, 0) + 1
                count = _failed_attempts[akey]
                if count >= 2:
                    hint = _retry_hint(name, args, count)
                    result += hint
                    console.print(
                        f"           [{'red' if count >= 3 else 'yellow'}]"
                        f"{'⛔' if count >= 3 else '⚠'} repeated failure #{count} — "
                        f"hint injected into LLM context[/{'red' if count >= 3 else 'yellow'}]"
                    )

            evidence.log_action(f"{name}({args})", result)
            log_result(name, result)
            if action_elapsed > 0.5 and name != "verify_claim":
                console.print(f"           [dim]action took {action_elapsed:.1f}s[/dim]")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

            if name == "read_page":
                read_page_ids.append(tool_call.id)
                prune_stale_read_pages(messages, tool_call.id, read_page_ids)
                if not result.startswith("error"):
                    last_snapshot = result

            # Capture selectors from successful interactive actions
            selectors: list[SelectorRecord] = []
            if name in ("click", "hover", "fill_field", "select_option") and not result.startswith("error"):
                selectors = [SelectorRecord(**s) for s in session._last_selectors]
                log_selectors(selectors)

            # Only record actions that succeeded — failed actions pollute the fingerprint
            if name == "verify_claim" or not result.startswith("error"):
                action_records.append(ActionRecord(tool=name, args=args, selectors=selectors))

            if name == "verify_claim":
                evidence.set_verdict(args["verdict"], args["confidence"], args["reasoning"])
                step.status = StepStatus(args["verdict"])
                log_verdict(args["verdict"], args["confidence"], args["reasoning"])
                if snap is not None:
                    snap_from_messages(messages, snap)

                # Record fingerprint on successful verification
                if fp_store and args.get("verdict") == "verified":
                    assertions = extract_assertions(args.get("reasoning", ""), last_snapshot)
                    snap_lower = last_snapshot.lower()
                    for ar in action_records[:-1]:
                        if ar.tool == "fill_field":
                            val = ar.args.get("value", "")
                            if len(val) >= 3 and val.lower() in snap_lower and val not in assertions:
                                assertions.append(val)
                    action_records[-1].assertions = assertions
                    templatize_actions(action_records, session_data or {}, step.data)
                    current_hash = step_definition_hash(
                        step.description,
                        step.expected,
                        getattr(step, "navigation", ""),
                        list(step.data.keys()),
                    )
                    fp = StepFingerprint(
                        step_id=step.id,
                        source_file=step.source_file,
                        recorded_at=datetime.now(timezone.utc).isoformat(),
                        run_id=run_id,
                        verdict=args["verdict"],
                        confidence=args["confidence"],
                        actions=action_records,
                        step_hash=current_hash,
                    )
                    fp_store.record(fp)
                    fp_store.save()
                    console.print(
                        f"    [dim cyan]📌 fingerprint recorded "
                        f"({len(action_records)} actions, {len(assertions)} assertions, "
                        f"hash={current_hash})[/dim cyan]"
                    )

                return step.status

    console.print(f"    [yellow]max actions reached without verdict → blocked[/yellow]")
    step.status = StepStatus.blocked
    return step.status
