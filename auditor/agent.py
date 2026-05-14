from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _elapsed(since: float) -> str:
    return f"{time.perf_counter() - since:.1f}s"

from rich.console import Console
from rich.rule import Rule

from auditor.evidence import EvidenceCollector
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
from auditor.loader import ConditionStatus, OutputCapture, Step, StepStatus, TestCondition
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
    "press_key":         ("yellow",  "⌨ "),
    "get_field_options": ("blue",    "📋"),
    "select_option":     ("green",   "▼ "),
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
    limit = 800 if name == "read_page" else 120
    display = result[:limit] + "…" if len(result) > limit else result
    console.print(f"           [{colour}]↳ {display}[/{colour}]")


def _log_selectors(selectors: list) -> None:
    """Log the actual DOM element that was matched — xpath, aria-label, tag."""
    if not selectors:
        return
    from rich.markup import escape
    parts = []
    for s in selectors:
        parts.append(f"{s.type}={escape(s.value)!r}")
    console.print(f"           [dim cyan]   element: {' | '.join(parts)}[/dim cyan]")


def _log_verdict(verdict: str, confidence: str, reasoning: str) -> None:
    colour = {"verified": "green", "failed": "red", "unverifiable": "yellow"}.get(verdict, "yellow")
    console.print(f"           [{colour}]verdict: {verdict} ({confidence}) [{_ts()}][/{colour}]")
    console.print(f"           [dim]{reasoning[:200]}[/dim]")


def _snap_from_messages(messages: list[dict], snap: dict[str, str]) -> None:
    """Extract url and title from the most recent read_page result in messages."""
    for msg in reversed(messages):
        content = msg.get("content", "")
        if msg.get("role") == "tool" and isinstance(content, str) and content.startswith("url:"):
            for line in content.splitlines():
                if line.startswith("url: "):
                    snap["url"] = line[5:].strip()
                elif line.startswith("title: "):
                    snap["title"] = line[7:].strip()
            return


def _prune_stale_read_pages(messages: list[dict], latest_id: str, read_page_ids: list[str]) -> None:
    """Replace old read_page results with a placeholder — only the latest snapshot is needed."""
    for msg in messages:
        if (
            msg.get("role") == "tool"
            and msg.get("tool_call_id") in read_page_ids
            and msg.get("tool_call_id") != latest_id
        ):
            msg["content"] = "[page snapshot removed — superseded by later read_page]"


def _execute_captures(
    captures: list[OutputCapture],
    session_data: dict[str, str],
) -> dict[str, str]:
    url = session_data.get("_last_url", "")
    title = session_data.get("_last_title", "")
    result: dict[str, str] = {}
    for cap in captures:
        if cap.strategy == "current_url":
            result[cap.key] = url
        elif cap.strategy == "page_title":
            result[cap.key] = title
        elif cap.strategy.startswith("url_segment:"):
            n = int(cap.strategy.split(":")[1])
            parts = [p for p in url.split("/") if p]
            result[cap.key] = parts[n] if n < len(parts) else ""
    return result


def _replay_selector(
    tool: str,
    selectors: list[SelectorRecord],
    args: dict[str, Any],
    session: BrowserSession,
) -> str:
    """Try stored selectors in order for click/hover/fill_field. Returns result string."""
    page = session._page
    for sel in selectors:
        try:
            if sel.type in ("xpath", "xpath_structural"):
                loc = page.locator(f"xpath={sel.value}").first
            elif sel.type == "aria_label":
                loc = page.locator(f'[aria-label="{sel.value}"]').first
            elif sel.type == "text":
                loc = page.get_by_text(sel.value, exact=True).first
            else:
                continue

            if tool == "click":
                loc.click(timeout=5000)
                # Brief wait for tab/panel transitions to settle before next action
                page.wait_for_timeout(400)
                sel.successes += 1
                return f"clicked (replay:{sel.type})"
            elif tool == "hover":
                loc.hover(timeout=5000)
                page.wait_for_timeout(600)
                sel.successes += 1
                return f"hovered (replay:{sel.type})"
            elif tool == "fill_field":
                value = args.get("value", "")
                if sel.type in ("xpath", "xpath_structural"):
                    # Delegate to fill_by_xpath which handles both regular fields
                    # AND combobox/autocomplete (types text + clicks the suggestion).
                    # The old inline combobox path only typed text and never clicked
                    # the dropdown item, causing autocomplete assertions to fail.
                    success = session.fill_by_xpath(sel.value, value)
                    if not success:
                        sel.failures += 1
                        continue
                else:
                    loc.fill(value, timeout=5000)
                    # Dismiss calendar popups that Ivalua opens after date fills
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                sel.successes += 1
                return f"filled (replay:{sel.type})"
        except Exception:
            sel.failures += 1
            continue

    # JS click fallback for click actions — some buttons (e.g. Ivalua's Save)
    # cannot be clicked via Playwright's actionability checks but respond to
    # a direct JS el.click(). Try each XPath selector via JS before giving up.
    if tool == "click":
        for sel in selectors:
            if sel.type in ("xpath", "xpath_structural"):
                try:
                    xpath_js = sel.value.replace("\\", "\\\\").replace('"', '\\"')
                    clicked = page.evaluate(f"""() => {{
                        const r = document.evaluate(
                            "{xpath_js}", document, null,
                            XPathResult.FIRST_ORDERED_NODE_TYPE, null
                        );
                        const el = r.singleNodeValue;
                        if (el) {{ el.click(); return true; }}
                        return false;
                    }}""")
                    if clicked:
                        sel.successes += 1
                        # Allow up to 5s for navigation triggered by the click
                        # (e.g. Save button that submits a form and redirects).
                        # Falls through silently if no navigation occurs.
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            page.wait_for_timeout(800)
                        return "clicked (replay:xpath-js)"
                except Exception:
                    pass

    return "error: all selectors failed"


def _resolve_template(value: str, session_data: dict[str, str]) -> str:
    """Replace {{key}} placeholders with values from session_data."""
    import re
    def replacer(m: re.Match) -> str:
        return session_data.get(m.group(1), m.group(0))
    return re.sub(r"\{\{(\w+)\}\}", replacer, value)


def _templatize_actions(
    actions: list[ActionRecord],
    session_data: dict[str, str],
    step_data: dict[str, str] | None = None,
) -> None:
    """Replace session/step-data values in fingerprint actions with {{key}} placeholders.

    Two substitutions are performed:
    1. navigate target: session_data values that appear as substrings are replaced
       (e.g. a captured requisition_url inside a navigate target).
    2. fill_field value: test data (step_data) values that are an EXACT match are
       replaced so the fingerprint stays valid when test_data.yaml changes.
       Exact-match only — partial/abbreviation matches (e.g. "NYC" for
       "Department of Homeless Services") are left as-is.

    Only named keys (no leading underscore) are used — internal tracking keys
    like _last_url and _last_title are excluded so they don't shadow the correct
    named key (e.g. requisition_url) when both hold the same URL at record time.
    """
    named_session = {k: v for k, v in session_data.items() if not k.startswith("_")}
    named_step = {k: v for k, v in (step_data or {}).items() if not k.startswith("_")}
    for action in actions:
        if action.tool == "navigate":
            target = action.args.get("target", "")
            for key, val in named_session.items():
                if val and val in target:
                    action.args["target"] = target.replace(val, f"{{{{{key}}}}}")
                    break
        elif action.tool == "fill_field":
            val = action.args.get("value", "")
            for key, data_val in named_step.items():
                # Exact case-insensitive match only — abbreviations like "NYC" for
                # "Department of Homeless Services" must NOT be templatized.
                if data_val and val.strip().lower() == data_val.strip().lower():
                    action.args["value"] = f"{{{{{key}}}}}"
                    break
        elif action.tool == "select_option":
            val = action.args.get("option_value", "")
            for key, data_val in named_step.items():
                if data_val and val.strip().lower() == data_val.strip().lower():
                    action.args["option_value"] = f"{{{{{key}}}}}"
                    break


def _try_fingerprint_replay(
    step: Step,
    session: BrowserSession,
    evidence: EvidenceCollector,
    fp: StepFingerprint,
    snap: dict[str, str],
    session_data: dict[str, str] | None = None,
    skip_navigate: bool = False,
) -> StepStatus | None:
    """
    Replay a fingerprint without calling the LLM.
    Returns ClaimStatus on success, None if replay fails (triggers ReAct fallback).
    """
    sd = session_data or {}
    last_snapshot = ""
    prev_tool = ""

    for action in fp.actions:
        tool = action.tool
        args = action.args

        if tool == "navigate":
            target = _resolve_template(args["target"], sd)
            if skip_navigate:
                # "continue" mode TC — this step should never navigate away from
                # the current browser state. A stored navigate was recorded when
                # the TC incorrectly had a navigation hint; skip it silently.
                print(f"           [replay] navigate({target!r}) skipped — navigation_mode=continue")
                continue
            result = session.navigate(target)
            evidence.log_action(f"navigate({target}) [replay]", result)
            if result.startswith("error"):
                # ERR_ABORTED = server-side redirect that Playwright reports as aborted.
                # The browser follows the redirect and is still within the app — continue
                # the replay rather than bailing. All other errors are fatal.
                if "ERR_ABORTED" not in result:
                    return None
                print(f"           [replay] navigate ERR_ABORTED (redirect) — continuing")

        elif tool == "read_page":
            # Only replay intermediate reads needed for state (skip purely-observational ones)
            pass

        elif tool == "select_option":
            field_label = args.get("field_label", "")
            raw_value = args.get("option_value", "")
            option_value = re.sub(
                r"\{\{(\w+)\}\}",
                lambda m: step.data.get(m.group(1), sd.get(m.group(1), m.group(0))),
                raw_value,
            )
            # Prefer XPath-based fill when selectors are stored — fill_by_xpath
            # uses press_sequentially + _click_autocomplete_item (8 s polling)
            # which is more robust than select_option's fixed 300 ms waits.
            xpath_sels = [s for s in action.selectors if s.type in ("xpath", "xpath_structural")]
            if xpath_sels:
                sel = xpath_sels[0]
                success = session.fill_by_xpath(sel.value, option_value)
                if success:
                    sel.successes += 1
                    result = f"filled via xpath (replay:select_option)"
                else:
                    sel.failures += 1
                    result = session.select_option(field_label, option_value)  # label fallback
            else:
                result = session.select_option(field_label, option_value)
            evidence.log_action(f"select_option({field_label!r}, {option_value!r}) [replay]", result)
            if result.startswith("error"):
                return None
            session._last_interacted_label = field_label.rstrip(" *").strip()

        elif tool in ("click", "hover", "fill_field"):
            if action.selectors:
                resolved_args = dict(args)
                if tool == "fill_field" and "value" in resolved_args:
                    resolved_args["value"] = re.sub(
                        r"\{\{(\w+)\}\}",
                        lambda m: step.data.get(m.group(1), m.group(0)),
                        resolved_args["value"],
                    )
                result = _replay_selector(tool, action.selectors, resolved_args, session)
            else:
                # No selectors recorded — fall back to ReAct
                return None
            evidence.log_action(f"{tool}(...) [replay]", result)
            if result.startswith("error"):
                return None
            # Mirror _dispatch: keep _last_interacted_label in sync so that
            # any step falling back to ReAct after this replay gets correct
            # focused-snapshot context.
            if tool == "fill_field":
                session._last_interacted_label = args.get("field_label", "").rstrip(" *").strip()
            elif tool == "click":
                # If the click immediately follows a fill_field it is almost
                # certainly a listbox/autocomplete selection — preserve the
                # label set by that fill_field.  All other clicks reset it.
                if prev_tool != "fill_field":
                    session._last_interacted_label = ""

        elif tool == "take_screenshot":
            label = args.get("label", "replay")
            evidence.save_screenshot(session.page, f"{step.id}_{label}")

        elif tool == "verify_claim":
            # Check assertions against current page before accepting the stored verdict
            if action.assertions:
                # Brief settle wait — the last interaction (fill, click, select)
                # may trigger a DOM update (chip render, XHR response, redirect)
                # that takes a moment to propagate.  Without this wait the
                # aria_snapshot is read before the widget has updated and the
                # assertion fails even though the interaction succeeded.
                session.page.wait_for_timeout(1500)
                last_snapshot = session.read_page()
                _snap_from_messages(
                    [{"role": "tool", "content": last_snapshot}], snap
                )
                # Dynamic-ID assertions (containing 5+ digit sequences) should have
                # been filtered out at recording time by extract_assertions(), so
                # the stored list should only contain stable structural strings.
                # A simple case-insensitive substring check is sufficient.
                snapshot_lower = last_snapshot.lower()
                if not all(a.lower() in snapshot_lower for a in action.assertions):
                    return None  # page content changed — fall back to ReAct

            verdict = args.get("verdict", "verified")
            confidence = args.get("confidence", "high")
            reasoning = args.get("reasoning", "replayed from fingerprint")
            # Always capture page state at verdict time — same guarantee as ReAct path.
            xpath = session._highlight_last_element()
            evidence.save_screenshot(session.page, f"{step.id}_verdict_{verdict}")
            if xpath:
                session._unhighlight_element(xpath)
            evidence.set_verdict(verdict, confidence, f"[replay] {reasoning[:200]}")
            step.status = StepStatus(verdict)
            return step.status

        prev_tool = tool  # track for next iteration (used to detect listbox clicks)

    return None  # no verify_claim found in fingerprint


def run_test_condition(
    tc: TestCondition,
    session: BrowserSession,
    llm: LLMClient,
    output_dir: Path,
    run_id: str,
    max_actions: int,
    session_data: dict[str, str] | None = None,
    fp_store: FingerprintStore | FingerprintRouter | None = None,
) -> tuple[ConditionStatus, list[dict], dict[str, str]]:
    session_data = session_data or {}
    console.print(Rule(f"[bold blue]{tc.id}[/bold blue] — {tc.goal}", style="blue"))

    # Log any session data this test condition is receiving
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

    # Reset focused-snapshot anchor once per TEST CONDITION, not per step.
    # Steps within the same test condition share browser state — letting the
    # fill_field focus from one step carry into the next one is intentional:
    # it gives the LLM the correct focused view without re-filling fields
    # (e.g. step_026 fills Procurement Method → step_027 reads PMQ questions).
    session._last_interacted_label = ""

    for step_id in order:
        step = step_map[step_id]

        if step.status == StepStatus.blocked:
            console.print(f"    [yellow]⊘[/yellow] {step_id} — blocked")
            continue

        console.print(Rule(f"[bold]{step_id}[/bold] — {step.description} [dim][{_ts()}][/dim]", style="dim"))
        console.print(f"    [dim]expected:[/dim] {step.expected}")

        ev = EvidenceCollector(run_id=run_id, claim_id=step_id, output_dir=output_dir)

        fp_for_step = fp_store.get(step.id, step.source_file) if fp_store else None
        # Invalidate fingerprint if the step definition has changed since recording.
        # step_hash="" means the fingerprint pre-dates this feature — let it replay
        # (backwards-compatible: old fingerprints without a hash are not broken).
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

        # Build the user message for this step, continuing the shared conversation
        is_continue = tc.execution.navigation_mode == "continue"
        if first_step:
            if is_continue:
                # "continue" mode: stay on current browser state — never mention a
                # navigation target so the LLM cannot be tempted to navigate away.
                last_url = session_data.get("_last_url", "")
                last_title = session_data.get("_last_title", "")
                title_hint = f" ({last_title})" if last_title else ""
                nav_context = (
                    f"Browser is currently at: {last_url}{title_hint}. "
                    f"This test condition continues from the previous one — "
                    f"do NOT navigate away. The page state is already correct.\n"
                )
            elif available:
                data_str = "\n".join(f"  {k}: {v}" for k, v in available.items())
                nav_hint = f"\n  start at: {tc.execution.navigation}" if tc.execution.navigation else ""
                current_browser = session_data.get("_last_url", "")
                if current_browser:
                    nav_context = (
                        f"Browser is currently at: {current_browser}. "
                        f"Session data from previous steps:\n{data_str}\n"
                        f"IMPORTANT: If the browser is already on the target page, "
                        f"do NOT navigate — proceed directly with the claim. "
                        f"Only navigate if the current page is clearly wrong."
                        f"{nav_hint}\n"
                    )
                else:
                    nav_context = (
                        f"Session data from previous steps:\n{data_str}\n"
                        f"Use this data to navigate directly if relevant"
                        f"{nav_hint}\n"
                    )
            elif tc.depends_on and session_data.get("_last_url"):
                last_url = session_data["_last_url"]
                last_title = session_data.get("_last_title", "")
                title_hint = f" ({last_title})" if last_title else ""
                nav_context = (
                    f"Browser is currently at: {last_url}{title_hint}. "
                    f"Page state from the previous test condition is preserved — do NOT navigate away "
                    f"unless this is the wrong page. "
                    + (f"Navigation hint if needed: {tc.execution.navigation}\n" if tc.execution.navigation else "\n")
                )
            else:
                nav_context = (
                    f"Start by navigating to: {tc.execution.navigation}\n"
                    if tc.execution.navigation else
                    "Navigate to the appropriate page to begin verification.\n"
                )
        else:
            current_url = session.current_url()
            nav_context = (
                f"You are continuing from the previous step. "
                f"Browser is currently at: {current_url}. "
                f"The page state from the previous step is preserved — do NOT navigate away. "
                f"Proceed directly with verifying this claim from the current page.\n"
            )

        data_context = ""
        if step.data:
            _sensitive = re.compile(r"password|passwd|secret|token|credential", re.IGNORECASE)
            data_lines = "\n".join(
                f"  {k}: [sensitive — pass placeholder {{{{{k}}}}} as the value]"
                if _sensitive.search(k) else f"  {k}: {v}"
                for k, v in step.data.items()
            )
            data_context = f"Test data (use these exact values):\n{data_lines}\n"

        messages.append({
            "role": "user",
            "content": (
                f"Claim: {step.description}\n"
                f"Expected outcome: {step.expected}\n"
                + data_context
                + nav_context
                + "Verify this claim and call verify_claim when done."
            ),
        })

        first_step = False
        console.print(f"    [dim]starting ReAct loop (max {max_actions} steps)… [{_ts()}][/dim]")

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
        # Prefer live browser URL (works for fingerprint replay + ReAct alike)
        live_url = session.current_url()
        session_data["_last_url"] = live_url or snap.get("url", "")
        session_data["_last_title"] = snap.get("title", "") or session._page.title()

        record = ev.finalize()
        evidence_records.append(record)

        icon = {"verified": "[green]✓[/green]", "failed": "[red]✗[/red]"}.get(status.value, "[yellow]⊘[/yellow]")
        console.print(f"    {icon} {step_id} — {status.value} [dim]({_elapsed(step_start)})[/dim]")

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
        captured = _execute_captures(tc.execution.output_capture, session_data)
        session_data.update(captured)
        for k, v in captured.items():
            console.print(f"    [dim]captured:[/dim] {k} = {v}")

    return tc.status, evidence_records, session_data


def _react_loop(
    step: Step,
    session: BrowserSession,
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
    # Steps with setup blocks: setup already ran via _run_setup(); only replay the ReAct actions.
    # Steps with setup blocks still qualify for replay — _try_fingerprint_replay handles ReAct part.
    if fp_store and fp_for_step and fp_for_step.actions:
        console.print(f"    [dim cyan]⚡ trying fingerprint replay… [{_ts()}][/dim cyan]")
        replay_snap: dict[str, str] = snap if snap is not None else {}
        status = _try_fingerprint_replay(step, session, evidence, fp_for_step, replay_snap, session_data, skip_navigate=skip_navigate)
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
            console.print(f"    [dim cyan]⚡ replay succeeded — no LLM call [{_ts()}][/dim cyan]")
            return status
        evidence.set_fingerprint_status("miss")
        console.print(f"    [dim yellow]replay failed → falling back to ReAct[/dim yellow]")
        step.status = StepStatus.blocked

    # --- Tier 2: full ReAct loop ---
    action_records: list[ActionRecord] = []
    last_snapshot = ""

    for step_num in range(1, max_actions + 1):
        console.print(f"    [dim]── calling LLM (step {step_num}/{max_actions}) … [{_ts()}][/dim]")
        llm_start = time.perf_counter()
        response = llm.reason(messages)
        usage = response.usage
        if usage:
            console.print(f"    [dim]   tokens: {usage.prompt_tokens} in / {usage.completion_tokens} out ({_elapsed(llm_start)})[/dim]")
        msg = response.choices[0].message

        if not msg.tool_calls:
            console.print("    [yellow]LLM returned no tool call — stopping loop[/yellow]")
            break

        messages.append({"role": "assistant", "content": msg.content, "tool_calls": msg.tool_calls})

        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            _log_llm_decision(step_num, name, args)

            action_start = time.perf_counter()
            result = _dispatch(name, args, step, session, evidence)
            action_elapsed = time.perf_counter() - action_start
            evidence.log_action(f"{name}({args})", result)

            _log_result(name, result)
            if action_elapsed > 0.5 and name != "verify_claim":
                console.print(f"           [dim]action took {action_elapsed:.1f}s[/dim]")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

            if name == "read_page":
                read_page_ids.append(tool_call.id)
                _prune_stale_read_pages(messages, tool_call.id, read_page_ids)
                if not result.startswith("error"):
                    last_snapshot = result

            # Capture selectors from interactive actions
            selectors: list[SelectorRecord] = []
            if name in ("click", "hover", "fill_field", "select_option") and not result.startswith("error"):
                selectors = [SelectorRecord(**s) for s in session._last_selectors]
                _log_selectors(selectors)

            # Only record actions that succeeded — failed actions pollute the
            # fingerprint and cause replay to bail before reaching the working
            # action sequence.  verify_claim is always kept (it's the verdict).
            if name == "verify_claim" or not result.startswith("error"):
                action_records.append(ActionRecord(tool=name, args=args, selectors=selectors))

            if name == "verify_claim":
                evidence.set_verdict(args["verdict"], args["confidence"], args["reasoning"])
                step.status = StepStatus(args["verdict"])
                _log_verdict(args["verdict"], args["confidence"], args["reasoning"])
                if snap is not None:
                    _snap_from_messages(messages, snap)

                # Record fingerprint for all verified steps
                if fp_store and args.get("verdict") == "verified":
                    assertions = extract_assertions(args.get("reasoning", ""), last_snapshot)
                    # Also assert every fill_field value — dates, labels, and typed
                    # values MUST appear in the post-fill snapshot. This prevents the
                    # case where a fingerprint was recorded against a pre-filled form
                    # (no fill actions) and the assertions are just field labels that
                    # are always present regardless of whether filling happened.
                    snap_lower = last_snapshot.lower()
                    for ar in action_records[:-1]:   # exclude the verify_claim record
                        if ar.tool == "fill_field":
                            val = ar.args.get("value", "")
                            if len(val) >= 3 and val.lower() in snap_lower and val not in assertions:
                                assertions.append(val)
                    action_records[-1].assertions = assertions
                    # Replace dynamic session values (e.g. requisition_url) with {{key}} placeholders.
                    # Also replace fill_field values that exactly match step test data so
                    # fingerprints stay valid when test_data.yaml changes.
                    _templatize_actions(action_records, session_data or {}, step.data)
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


def _dispatch(
    name: str,
    args: dict[str, Any],
    step: Step,
    session: BrowserSession,
    evidence: EvidenceCollector,
) -> str:
    match name:
        case "navigate":
            session._last_interacted_label = ""   # new page — reset focus
            return session.navigate(args["target"])
        case "read_page":
            return session.read_page()
        case "click":
            result = session.click(args["element_description"])
            if not result.startswith("error"):
                if "(ivalua-listbox)" in result:
                    # Listbox selection completes a fill_field operation.
                    # Preserve _last_interacted_label from the preceding fill_field
                    # so the next read_page shows a focused view around that field,
                    # including any conditional questions that just appeared.
                    pass
                else:
                    # All other clicks (nav buttons, tabs, save/cancel, Yes/No radios,
                    # section toggles): show the full page so the LLM sees the result
                    # without a focused window anchored on the wrong element.
                    session._last_interacted_label = ""
            return result
        case "hover":
            return session.hover(args["element_description"])
        case "fill_field":
            value = re.sub(r"\{\{(\w+)\}\}", lambda m: step.data.get(m.group(1), m.group(0)), args["value"])
            _sensitive = re.compile(r"password|passwd|secret|token|credential", re.IGNORECASE)
            display_value = "[sensitive]" if _sensitive.search(args["field_label"]) else repr(value)
            console.print(f"           [dim]data: field={args['field_label']!r} value={display_value}[/dim]")
            result = session.fill_field(args["field_label"], value)
            if not result.startswith("error"):
                session._last_interacted_label = args["field_label"].rstrip(" *").strip()
            return result
        case "clear_field":
            return session.clear_field(args["field_label"])
        case "submit_form":
            session._last_interacted_label = ""   # form submitted — reset focus
            return session.submit_form()
        case "press_key":
            return session.press_key(args["key"])
        case "get_field_options":
            return session.get_field_options(args["field_label"])
        case "select_option":
            result = session.select_option(args["field_label"], args["option_value"])
            if not result.startswith("error"):
                session._last_interacted_label = args["field_label"].rstrip(" *").strip()
            return result
        case "take_screenshot":
            # Highlight last interacted element before saving, then restore
            xpath = session._highlight_last_element()
            path = evidence.save_screenshot(session.page, f"{step.id}_{args['label']}")
            if xpath:
                session._unhighlight_element(xpath)
            return path
        case "verify_claim":
            # Always capture page state at verdict time as baseline evidence,
            # regardless of whether the LLM called take_screenshot earlier.
            verdict = args.get("verdict", "verified")
            xpath = session._highlight_last_element()
            evidence.save_screenshot(session.page, f"{step.id}_verdict_{verdict}")
            if xpath:
                session._unhighlight_element(xpath)
            return "verdict recorded"
        case _:
            return f"unknown tool: {name}"
