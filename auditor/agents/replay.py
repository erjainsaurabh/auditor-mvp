"""Fingerprint replay — deterministic step execution without LLM calls.

FingerprintReplayer.try_replay() attempts to execute a recorded fingerprint
against the live browser. Returns StepStatus on success, None on failure
(caller should fall back to the full ReAct loop).

Execution tiers (called from react_agent.py):
  Tier 1 — try_replay()  — stored selectors, zero LLM calls
  Tier 2 — full ReAct loop (in react_agent.py)
"""
from __future__ import annotations

import re
from typing import Any

from auditor.agents.console import console, ts
from auditor.agents.session_utils import resolve_template, snap_from_messages
from auditor.fingerprint import SelectorRecord, StepFingerprint
from auditor.loader import Step, StepStatus


def replay_selector(
    tool: str,
    selectors: list[SelectorRecord],
    args: dict[str, Any],
    session: Any,       # BrowserAdapter
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
                    success = session.fill_by_xpath(sel.value, value)
                    if not success:
                        sel.failures += 1
                        continue
                else:
                    loc.fill(value, timeout=5000)
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                sel.successes += 1
                return f"filled (replay:{sel.type})"
        except Exception:
            sel.failures += 1
            continue

    # JS click fallback — some buttons respond to el.click() but not Playwright's
    # actionability-checked click (e.g. Ivalua's Save button).
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
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=5000)
                        except Exception:
                            page.wait_for_timeout(800)
                        return "clicked (replay:xpath-js)"
                except Exception:
                    pass

    return "error: all selectors failed"


class FingerprintReplayer:
    """Replays a stored fingerprint deterministically — no LLM involved."""

    def try_replay(
        self,
        step: Step,
        session: Any,               # BrowserAdapter
        evidence: Any,              # EvidenceStore
        fp: StepFingerprint,
        snap: dict[str, str],
        session_data: dict[str, str] | None = None,
        skip_navigate: bool = False,
    ) -> StepStatus | None:
        """
        Attempt to replay a fingerprint.
        Returns StepStatus on success, None if any action fails (triggers ReAct fallback).
        """
        sd = session_data or {}
        last_snapshot = ""
        prev_tool = ""

        for action in fp.actions:
            tool = action.tool
            args = action.args

            if tool == "navigate":
                target = resolve_template(args["target"], sd)
                if skip_navigate:
                    print(f"           [replay] navigate({target!r}) skipped — navigation_mode=continue")
                    continue
                result = session.navigate(target)
                evidence.log_action(f"navigate({target}) [replay]", result)
                if result.startswith("error"):
                    if "ERR_ABORTED" not in result:
                        return None
                    print(f"           [replay] navigate ERR_ABORTED (redirect) — continuing")

            elif tool == "read_page":
                pass  # skip observational reads during replay

            elif tool == "select_option":
                field_label = args.get("field_label", "")
                raw_value = args.get("option_value", "")
                option_value = re.sub(
                    r"\{\{(\w+)\}\}",
                    lambda m: step.data.get(m.group(1), sd.get(m.group(1), m.group(0))),
                    raw_value,
                )
                xpath_sels = [s for s in action.selectors if s.type in ("xpath", "xpath_structural")]
                if xpath_sels:
                    sel = xpath_sels[0]
                    success = session.fill_by_xpath(sel.value, option_value)
                    if success:
                        sel.successes += 1
                        result = "filled via xpath (replay:select_option)"
                    else:
                        sel.failures += 1
                        result = session.select_option(field_label, option_value)
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
                    result = replay_selector(tool, action.selectors, resolved_args, session)
                else:
                    return None
                evidence.log_action(f"{tool}(...) [replay]", result)
                if result.startswith("error"):
                    return None
                if tool == "fill_field":
                    session._last_interacted_label = args.get("field_label", "").rstrip(" *").strip()
                elif tool == "click":
                    if prev_tool != "fill_field":
                        session._last_interacted_label = ""

            elif tool == "take_screenshot":
                label = args.get("label", "replay")
                path = evidence.save_screenshot(session.page, f"{step.id}_{label}")
                evidence.log_action(f"take_screenshot(label={label!r}) [replay]", path)

            elif tool == "verify_claim":
                if action.assertions:
                    session.page.wait_for_timeout(1500)
                    last_snapshot = session.read_page()
                    snap_from_messages(
                        [{"role": "tool", "content": last_snapshot}], snap
                    )
                    snapshot_lower = last_snapshot.lower()
                    if not all(a.lower() in snapshot_lower for a in action.assertions):
                        return None  # page content changed — fall back to ReAct

                verdict = args.get("verdict", "verified")
                confidence = args.get("confidence", "high")
                reasoning = args.get("reasoning", "replayed from fingerprint")
                xpath = session._highlight_last_element()
                evidence.save_screenshot(session.page, f"{step.id}_verdict_{verdict}")
                if xpath:
                    session._unhighlight_element(xpath)
                evidence.set_verdict(verdict, confidence, f"[replay] {reasoning[:200]}")
                step.status = StepStatus(verdict)
                return step.status

            prev_tool = tool

        return None  # no verify_claim found in fingerprint
