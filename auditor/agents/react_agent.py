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
from auditor.logger import get_logger
from auditor.pattern_inventory import PatternInventory
from auditor.storage.filesystem import EvidenceCollector

log = get_logger(__name__)
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
    pattern_inventory: PatternInventory | None = None,
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

    evidence_records: list[dict] = []
    first_step = True

    session._last_interacted_label = ""

    for step_id in order:
        step = step_map[step_id]

        if step.status == StepStatus.blocked:
            console.print(f"    [yellow]⊘[/yellow] {step_id} — blocked")
            continue

        messages: list[dict[str, Any]] = []
        read_page_ids: list[str] = []

        console.print(Rule(f"[bold]{step_id}[/bold] — {step.description} [dim][{ts()}][/dim]", style="dim"))
        console.print(f"    [dim]expected:[/dim] {step.expected}")
        log.info("step starting", extra={"event": "step_start", "step_id": step_id,
                                         "description": step.description, "expected": step.expected,
                                         "run_id": run_id, "tc_id": tc.id})

        ev = EvidenceCollector(run_id=run_id, claim_id=step_id, output_dir=output_dir)

        fp_for_step = fp_store.get(step.id, step.source_file) if fp_store else None
        if fp_for_step and fp_for_step.step_hash:
            current_hash = step_definition_hash(
                step.description,
                step.expected,
                getattr(step, "navigation", ""),
                list(step.data.keys()),
                step.hints,
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
            hints_context = (
                f"Suggested tool calls for this step:\n{hints_lines}\n"
                f"If these don't work, ignore them and find your own way "
                f"to complete the step.\n"
            )

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

        # Pattern inventory suggestion — only injected when no fingerprint exists
        # and the inventory has enough observations for this (platform, step_type, verb)
        inventory_context = ""
        if pattern_inventory and not fp_for_step:
            _snap_for_query = session.read_page()
            suggestion = pattern_inventory.query(step.type.value, step.description, _snap_for_query)
            if suggestion:
                inventory_context = suggestion
                console.print(f"    [dim cyan]📖 pattern inventory match — suggestion injected[/dim cyan]")

        messages.append({
            "role": "user",
            "content": (
                f"Claim: {step.description}\n"
                f"Expected outcome: {step.expected}\n"
                + hints_context
                + inventory_context
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
            pattern_inventory=pattern_inventory,
            preloaded_snapshot=_snap_for_query if (pattern_inventory and not fp_for_step) else None,
        )
        live_url = session.current_url()
        session_data["_last_url"] = live_url or snap.get("url", "")
        session_data["_last_title"] = snap.get("title", "") or session._page.title()

        record = ev.finalize()
        evidence_records.append(record)

        icon = {"verified": "[green]✓[/green]", "failed": "[red]✗[/red]"}.get(status.value, "[yellow]⊘[/yellow]")
        console.print(f"    {icon} {step_id} — {status.value} [dim]({elapsed(step_start)})[/dim]")
        log.info("step complete", extra={"event": "step_done", "step_id": step_id,
                                         "status": status.value, "tc_id": tc.id, "run_id": run_id,
                                         "elapsed_s": round(time.perf_counter() - step_start, 2)})

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
        pattern_inventory: PatternInventory | None = None,
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
            pattern_inventory=pattern_inventory,
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


def _trim_to_causal_chain(records: list) -> list:
    """Return only the actions that form the minimal causal path to success.

    Rules applied in order:
    1. Drop read_page and take_screenshot — observational, not causal.
    2. Deduplicate: if the same tool+args appear consecutively, keep only the last
       (the last attempt is the one that worked or informed the next action).
    3. For navigate: keep only the final navigate per unique URL — earlier navigates
       to the same destination are redundant.
    4. Drop navigate actions where the URL didn't change (no-op navigates).
    """
    from auditor.fingerprint import ActionRecord

    _observational = {"read_page", "take_screenshot"}
    cleaned = [r for r in records if r.tool not in _observational]

    # Deduplicate consecutive same-tool same-args (keep last of each run)
    deduped: list = []
    for r in cleaned:
        if (deduped and deduped[-1].tool == r.tool
                and deduped[-1].args == r.args):
            deduped[-1] = r  # replace with latest (may have better selectors)
        else:
            deduped.append(r)

    # For navigate: keep only the last navigate to each unique target
    seen_nav: dict[str, int] = {}
    for i, r in enumerate(deduped):
        if r.tool == "navigate":
            target = r.args.get("target", "")
            seen_nav[target] = i
    nav_keep = set(seen_nav.values())

    result = [
        r for i, r in enumerate(deduped)
        if r.tool != "navigate" or i in nav_keep
    ]
    return result


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
    pattern_inventory: PatternInventory | None = None,
    preloaded_snapshot: str | None = None,
) -> StepStatus:
    # --- Tier 1: fingerprint replay (zero LLM calls on stable UI) ---
    if fp_store and fp_for_step and fp_for_step.actions:
        console.print(f"    [dim cyan]⚡ trying fingerprint replay… [{ts()}][/dim cyan]")
        url_before_replay = session.current_url()
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
            log.info("fingerprint replay succeeded", extra={"event": "fp_replay_hit",
                                                            "step_id": step.id, "status": str(status)})
            return status

        # Replay failed — restore browser to pre-replay URL so ReAct starts from known state
        evidence.set_fingerprint_status("miss")
        url_after_replay = session.current_url()
        if url_after_replay != url_before_replay:
            console.print(f"    [dim yellow]replay left browser at {url_after_replay} — restoring to {url_before_replay}[/dim yellow]")
            restore_result = session.navigate(url_before_replay)
            log.info("replay rollback", extra={
                "event": "fp_replay_rollback",
                "step_id": step.id,
                "url_before_replay": url_before_replay,
                "url_after_replay": url_after_replay,
                "restore_result": restore_result[:200],
            })
        console.print(f"    [dim yellow]replay failed → falling back to ReAct[/dim yellow]")
        log.info("fingerprint replay failed — falling back to ReAct", extra={"event": "fp_replay_miss",
                                                                              "step_id": step.id})
        step.status = StepStatus.blocked

        # Rebuild the user message with the actual post-rollback URL (Option 4)
        actual_url = session.current_url()
        if messages and messages[-1]["role"] == "user":
            old_content = messages[-1]["content"]
            # Replace the nav_context portion — strip everything from "You are continuing"
            # or "Browser is currently at" to end, then append fresh URL
            import re as _re2
            cleaned = _re2.sub(
                r"(You are continuing from the previous step\..*|Browser is currently at:.*)",
                "",
                old_content,
                flags=_re2.DOTALL,
            ).rstrip()
            messages[-1]["content"] = (
                cleaned + "\n"
                f"Browser is currently at: {actual_url}. "
                "Proceed directly with verifying this claim from the current page.\n"
                "Verify this claim and call verify_claim when done."
            )

    # --- Tier 2: full ReAct loop ---
    action_records: list[ActionRecord] = []
    _failed_attempts: dict[str, int] = {}
    _working_memory: dict[str, Any] = {
        "step": 0,
        "current_url": session.current_url(),
        "last_action": None,
        "last_successful_action": None,
        "failed_attempts": {},
        "navigations": [],
    }

    # Pre-load page state — reuse snapshot already taken for pattern inventory query
    # if available, otherwise call read_page now.
    # Injected as a fake assistant tool_use + tool_result pair so the LLM sees it
    # as an already-completed read_page call and skips calling it again as its first action.
    initial_snapshot = preloaded_snapshot if preloaded_snapshot else session.read_page()
    last_snapshot = initial_snapshot
    evidence.log_action("read_page({})", initial_snapshot)
    # Inject as a fake tool_use + tool_result pair in the same OpenAI-compat format
    # used by the live loop so LiteLLM accepts it without format mismatch.
    # The LLM sees read_page as already-called and skips it as its first action.
    _preload_id = "toolu_preload_read_page"
    messages.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": _preload_id,
            "type": "function",
            "function": {"name": "read_page", "arguments": "{}"},
        }],
    })
    messages.append({
        "role": "tool",
        "tool_call_id": _preload_id,
        "content": initial_snapshot,
    })
    read_page_ids.append(_preload_id)
    snapshot_injected = True
    snap_lower = initial_snapshot.lower()
    # Extract short quoted strings from hints (e.g. "Campaign", "--") plus
    # words from step description as subject keywords to check in the snapshot.
    import re as _re
    _quoted = _re.findall(r'"([^"]{2,30})"', " ".join(step.hints or []))
    _desc_words = [w for w in step.description.split() if len(w) > 4][:5]
    _keywords = list(dict.fromkeys(_quoted + _desc_words))  # dedup, preserve order
    log.debug("pre-loaded read_page", extra={
        "event": "preload_read_page",
        "step_id": step.id,
        "url": session.current_url(),
        "reused_from_query": preloaded_snapshot is not None,
        "injected_as_tool_result": snapshot_injected,
        "snapshot_chars": len(initial_snapshot),
        "keywords_in_snapshot": {kw: (kw.lower() in snap_lower) for kw in _keywords},
    })

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
            url_before = session.current_url()
            result = dispatch(name, args, step, session, evidence)
            action_elapsed = time.perf_counter() - action_start
            url_after = session.current_url()

            _sensitive_re = re.compile(r"password|passwd|secret|token|credential", re.IGNORECASE)
            safe_args = {
                k: "[sensitive]" if _sensitive_re.search(k) else v
                for k, v in args.items()
            }
            result_summary = result.split("\n")[0][:200] if result else ""
            log.debug(
                "tool call",
                extra={
                    "event": "tool_call",
                    "step_id": step.id,
                    "step_num": step_num,
                    "tool": name,
                    "tool_args": safe_args,
                    "result_summary": result_summary,
                    "success": not result.startswith("error"),
                    "url_before": url_before,
                    "url_after": url_after,
                    "navigated": url_before != url_after,
                    "elapsed_ms": round(action_elapsed * 1000),
                },
            )

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

            # Update working memory from this action
            _working_memory["step"] = step_num
            _working_memory["current_url"] = url_after
            _working_memory["last_action"] = {"tool": name, "args": safe_args, "success": not result.startswith("error")}
            if not result.startswith("error") and name != "read_page":
                _working_memory["last_successful_action"] = {"tool": name, "args": safe_args}
            if url_after not in _working_memory["navigations"] and url_after:
                _working_memory["navigations"].append(url_after)
            if result.startswith("error"):
                akey_wm = f"{name}:{args.get('element_description', args.get('field_label', args.get('target', '')))}"
                _working_memory["failed_attempts"][akey_wm] = _working_memory["failed_attempts"].get(akey_wm, 0) + 1

            # Inject compact working memory into tool result (skip for verify_claim and read_page)
            content = result
            if name not in ("verify_claim", "read_page"):
                wm_lines = [
                    f"[context] step {_working_memory['step']}/20"
                    f" | url: {_working_memory['current_url']}"
                ]
                if _working_memory["failed_attempts"]:
                    fa = ", ".join(f"{k}×{v}" for k, v in _working_memory["failed_attempts"].items())
                    wm_lines.append(f"[context] failed so far: {fa}")
                if _working_memory["last_successful_action"]:
                    la = _working_memory["last_successful_action"]
                    wm_lines.append(f"[context] last success: {la['tool']}({la['args']})")
                content = result + "\n" + "\n".join(wm_lines)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": content,
            })

            if name == "read_page":
                read_page_ids.append(tool_call.id)
                prune_stale_read_pages(messages, tool_call.id, read_page_ids)
                if not result.startswith("error"):
                    last_snapshot = result

            # Capture selectors from successful interactive actions
            selectors: list[SelectorRecord] = []
            if name in ("click", "hover", "fill_field", "select_option", "download_file") and not result.startswith("error"):
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
                    causal_records = _trim_to_causal_chain(action_records[:-1])
                    for ar in causal_records:
                        if ar.tool == "fill_field":
                            val = ar.args.get("value", "")
                            if len(val) >= 3 and val.lower() in snap_lower and val not in assertions:
                                assertions.append(val)
                    causal_records.append(action_records[-1])  # re-attach verify_claim
                    causal_records[-1].assertions = assertions
                    templatize_actions(causal_records, session_data or {}, step.data)
                    current_hash = step_definition_hash(
                        step.description,
                        step.expected,
                        getattr(step, "navigation", ""),
                        list(step.data.keys()),
                        step.hints,
                    )
                    fp = StepFingerprint(
                        step_id=step.id,
                        source_file=step.source_file,
                        recorded_at=datetime.now(timezone.utc).isoformat(),
                        run_id=run_id,
                        verdict=args["verdict"],
                        confidence=args["confidence"],
                        actions=causal_records,
                        step_hash=current_hash,
                    )
                    fp_store.record(fp)
                    fp_store.save()
                    console.print(
                        f"    [dim cyan]📌 fingerprint recorded "
                        f"({len(action_records)} actions, {len(assertions)} assertions, "
                        f"hash={current_hash})[/dim cyan]"
                    )

                # Record into pattern inventory — always on verified, independent of fp_store
                if pattern_inventory and args.get("verdict") == "verified":
                    pattern_inventory.record(
                        step_type=step.type.value,
                        description=step.description,
                        action_records=action_records[:-1],  # exclude verify_claim itself
                        last_snapshot=last_snapshot,
                    )
                    pattern_inventory.save()

                return step.status

    console.print(f"    [yellow]max actions reached without verdict → blocked[/yellow]")
    step.status = StepStatus.blocked
    return step.status
