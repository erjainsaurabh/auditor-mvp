"""Tool dispatcher — maps LLM tool call names to BrowserSession actions.

`dispatch()` is the single seam between the ReAct loop and the browser layer.
Adding a new tool means: define it in tool_definitions.yaml, handle it here,
and implement it on the BrowserAdapter. The ReAct loop itself never changes.
"""
from __future__ import annotations

import re
from typing import Any

from auditor.agents.console import console
from auditor.storage.base import EvidenceStore


def dispatch(
    name: str,
    args: dict[str, Any],
    step: Any,          # auditor.loader.Step — typed as Any to avoid circular import
    session: Any,       # auditor.browser.base.BrowserAdapter
    evidence: EvidenceStore,
) -> str:
    match name:
        case "navigate":
            session._last_interacted_label = ""   # new page — reset focus
            return session.navigate(args["target"])

        case "read_page":
            return session.read_page()

        case "click":
            result = session.click(args["element_description"], element_type=args.get("element_type"))
            if not result.startswith("error"):
                if "(ivalua-listbox)" in result:
                    # Listbox selection completes a fill_field operation.
                    # Preserve _last_interacted_label so the next read_page
                    # shows a focused view around that field.
                    pass
                else:
                    session._last_interacted_label = ""
            # Always append post-click page state so the LLM can detect
            # navigation even when the click itself reports an error.
            try:
                page = session._page
                page.wait_for_load_state("domcontentloaded", timeout=2000)
                result += f"\npage_after_click: url={page.url} | title={page.title()}"
            except Exception:
                pass
            return result

        case "hover":
            return session.hover(args["element_description"])

        case "fill_field":
            value = re.sub(
                r"\{\{(\w+)\}\}",
                lambda m: step.data.get(m.group(1), m.group(0)),
                args["value"],
            )
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
            session._last_interacted_label = ""
            return session.submit_form()

        case "press_key":
            return session.press_key(args["key"])

        case "click_by_id":
            return session.click_by_id(args["element_id"])

        case "get_field_options":
            return session.get_field_options(args["field_label"])

        case "select_option":
            result = session.select_option(args["field_label"], args["option_value"])
            if not result.startswith("error"):
                session._last_interacted_label = args["field_label"].rstrip(" *").strip()
            return result

        case "select_filter":
            # Accept both "container_attribute" (current) and "scoped_selector" (legacy alias)
            container_attr = args.get("container_attribute") or args.get("scoped_selector", "")
            result = session.select_filter(args["filter_label"], args["option_value"], container_attr)
            if not result.startswith("error"):
                session._last_interacted_label = args["filter_label"].rstrip(" *").strip()
            return result

        case "download_file":
            result = session.download_file(args["element_description"])
            console.print(f"           [dim]download: {result}[/dim]")
            return result

        case "upload_file":
            result = session.upload_file(args["field_label"], args["file_ref"])
            if not result.startswith("error"):
                session._last_interacted_label = args["field_label"].rstrip(" *").strip()
            return result

        case "take_screenshot":
            xpath = session._highlight_last_element()
            path = evidence.save_screenshot(session.page, f"{step.id}_{args['label']}")
            if xpath:
                session._unhighlight_element(xpath)
            return path

        case "verify_claim":
            verdict = args.get("verdict", "verified")
            xpath = session._highlight_last_element()
            evidence.save_screenshot(session.page, f"{step.id}_verdict_{verdict}")
            if xpath:
                session._unhighlight_element(xpath)
            return "verdict recorded"

        case _:
            return f"unknown tool: {name}"
