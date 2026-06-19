"""Session data utilities for the ReAct loop.

Handles:
- Template substitution in action args ({{key}} → session_data[key])
- Extracting URL/title from message history
- Pruning stale read_page snapshots from the LLM context window
- Executing OutputCapture strategies after a test condition completes
- Templatizing recorded actions so fingerprints stay valid across data changes
"""
from __future__ import annotations

import re

from auditor.loader import OutputCapture


def resolve_template(value: str, session_data: dict[str, str]) -> str:
    """Replace {{key}} placeholders with values from session_data."""
    def replacer(m: re.Match) -> str:
        return session_data.get(m.group(1), m.group(0))
    return re.sub(r"\{\{(\w+)\}\}", replacer, value)


def snap_from_messages(messages: list[dict], snap: dict[str, str]) -> None:
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


def prune_stale_read_pages(messages: list[dict], latest_id: str, read_page_ids: list[str]) -> None:
    """Replace old read_page results with a placeholder — only the latest snapshot is needed.
    Also collapses repeated identical tool errors into a single counted message so the
    conversation history doesn't bloat with 8× identical 'Timeout 1500ms exceeded' strings.
    """
    for msg in messages:
        if (
            msg.get("role") == "tool"
            and msg.get("tool_call_id") in read_page_ids
            and msg.get("tool_call_id") != latest_id
        ):
            msg["content"] = "[page snapshot removed — superseded by later read_page]"

    # Collapse consecutive identical tool error messages.
    # Pattern: tool msg with same error text appearing N times → keep first, replace rest.
    seen_errors: dict[str, int] = {}
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.startswith("error:"):
            continue
        # Normalise: strip variable parts (timeouts, element IDs) for dedup key
        key = re.sub(r"\d+ms|\bid=['\"]?[\w-]+['\"]?", "", content)[:120]
        count = seen_errors.get(key, 0)
        seen_errors[key] = count + 1
        if count > 0:
            msg["content"] = f"[same error repeated — already shown above]"


def execute_captures(
    captures: list[OutputCapture],
    session_data: dict[str, str],
) -> dict[str, str]:
    """Run OutputCapture strategies and return the captured key→value pairs."""
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


def templatize_actions(
    actions: list,
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
                if data_val and val.strip().lower() == data_val.strip().lower():
                    action.args["value"] = f"{{{{{key}}}}}"
                    break
        elif action.tool == "select_option":
            val = action.args.get("option_value", "")
            for key, data_val in named_step.items():
                if data_val and val.strip().lower() == data_val.strip().lower():
                    action.args["option_value"] = f"{{{{{key}}}}}"
                    break
