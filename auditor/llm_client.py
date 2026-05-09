from __future__ import annotations

import time
from typing import Any

import litellm
from dotenv import load_dotenv

load_dotenv()

_SYSTEM_PROMPT = """\
You are a QA auditor verifying claims about a web application.
You have browser tools available. Use them to verify the given claim.
Be methodical: navigate to the right page, interact minimally, observe the result.
When you have enough evidence, call verify_claim with your verdict.
Never guess — if you cannot reach a clear verdict, use verdict=unverifiable.

Interaction rules:
- For autocomplete/lookup fields (e.g. Requesting Agency, Division): call fill_field(field_label, value) DIRECTLY with the field label — do NOT click the field first. fill_field opens the dropdown internally and types into the search input. After calling fill_field, call read_page to see the suggestion list, then click the matching suggestion text.
- For combobox/select fields: use select_option first; fall back to fill_field if select_option fails.
- NEVER click a field label to open a dropdown — clicking a label in this app opens a modal popup. Always use fill_field instead.
- If a "See All" modal popup opened accidentally, close it with click("Cancel") or click("Close"), then use fill_field directly.
- When the context says "Browser is currently at:" with a form URL, do NOT navigate away — you are already on the right page. Only navigate if the URL is completely wrong.
- Strip asterisks from field labels when passing to tools — use "Requesting Agency" not "Requesting Agency *".
- If test data is provided, use those exact values without modification.
- Autocomplete workflow: fill_field("Requesting Agency", "Department of Homeless Services") → read_page → click("Department of Homeless Services") to pick from the suggestion list.
- For date fields: use fill_field("(M/d/yyyy)", "6/1/2026") for the START date and fill_field("to", "5/31/2027") for the END date (the end date lives under the "to:" heading). NEVER click the calendar icon. After filling a date, move on immediately — the calendar dismisses automatically.
- For browse-modal fields (e.g. "Procurement Method"): clicking the combobox opens a modal popup. After clicking the combobox, call read_page to confirm the modal is open (the page will show an iframe URL containing "ellipsis_browse" or "modal.aspx"). Then call click("Emergency") — the code will find the correct row in the modal. After clicking, call read_page and confirm the modal has closed and the combobox now shows the selected value. Do NOT press Escape — that cancels the modal without selecting.
- For Yes/No radio questions: call click("Yes") or click("No") to select the answer. If there are multiple Yes/No questions on the page, click the exact text near the question. Do not use fill_field for Yes/No radios.
"""

_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Go to a URL path (e.g. '/login') or full URL",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_page",
            "description": "Get a semantic summary of the current page (fields, buttons, errors, headings)",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an element identified by its visible text",
            "parameters": {
                "type": "object",
                "properties": {"element_description": {"type": "string"}},
                "required": ["element_description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fill_field",
            "description": "Type a value into a form field identified by its label",
            "parameters": {
                "type": "object",
                "properties": {
                    "field_label": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["field_label", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_field",
            "description": "Clear a form field identified by its label",
            "parameters": {
                "type": "object",
                "properties": {"field_label": {"type": "string"}},
                "required": ["field_label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_form",
            "description": "Submit the current form",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_field_options",
            "description": "Get all available options in a dropdown or select field",
            "parameters": {
                "type": "object",
                "properties": {"field_label": {"type": "string"}},
                "required": ["field_label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_option",
            "description": "Select a value in a dropdown, <select>, or combobox field. Use this instead of click/fill_field when the aria snapshot shows role=combobox or role=listbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field_label": {"type": "string", "description": "Label of the dropdown field"},
                    "option_value": {"type": "string", "description": "The option text or value to select"},
                },
                "required": ["field_label", "option_value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hover",
            "description": "Hover over an element to reveal CSS dropdowns or submenus, then call read_page to see the revealed options",
            "parameters": {
                "type": "object",
                "properties": {"element_description": {"type": "string"}},
                "required": ["element_description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a keyboard key (e.g. 'Escape' to close a calendar/dropdown, 'Tab' to move focus, 'Enter' to confirm). Use Escape after filling a date field to dismiss the calendar picker.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Key name: Escape, Tab, Enter, ArrowDown, etc."}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Capture a screenshot as evidence with a descriptive label",
            "parameters": {
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_claim",
            "description": "Record the final verdict for the current claim",
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["verified", "failed", "blocked", "unverifiable"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reasoning": {"type": "string"},
                },
                "required": ["verdict", "confidence", "reasoning"],
            },
        },
    },
]


class LLMClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self._reasoning_model: str = config["reasoning_model"]
        self._fast_model: str = config["fast_model"]
        self._max_tokens: int = config.get("max_tokens", 4096)
        self._cache: bool = config.get("cache_system_prompt", True)

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                **({"cache_control": {"type": "ephemeral"}} if self._cache else {}),
            }
        ]
        self._system_message: dict[str, Any] = {"role": "system", "content": content}

    def reason(self, messages: list[dict[str, Any]]) -> Any:
        for attempt in range(3):
            try:
                return litellm.completion(
                    model=self._reasoning_model,
                    messages=[self._system_message] + messages,
                    tools=_TOOL_DEFINITIONS,
                    max_tokens=self._max_tokens,
                )
            except litellm.RateLimitError:
                wait = 60 * (attempt + 1)
                print(f"    [rate limit] waiting {wait}s before retry {attempt + 1}/3…")
                time.sleep(wait)
        raise RuntimeError("rate limit: exhausted 3 retries")

    def extract(self, prompt: str) -> str:
        response = litellm.completion(
            model=self._fast_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        return response.choices[0].message.content or ""
