from __future__ import annotations

import time
from typing import Any

import litellm
from dotenv import load_dotenv

load_dotenv()

_SYSTEM_PROMPT = """\
You are a QA auditor verifying claims about a web application.
You have browser tools available. Use them to verify the given claim.
Be methodical: reach the right page state, interact minimally, observe the result.
When you have enough evidence, call verify_claim with your verdict.
Never guess — if you cannot reach a clear verdict, use verdict=unverifiable.

SCOPE RULE — read this first:
The 'Expected outcome' is the ONLY thing you verify. Read it, identify the single
observable state it describes, confirm that state, and call verify_claim immediately.
The 'Claim description' is background context only — treat it like a code comment.
Do NOT verify anything mentioned in the description that is not in the expected outcome.
Do NOT answer questions, fill extra fields, or explore further once the expected state is confirmed.

BROWSER STATE RULE:
Claims in a step share browser state — previous claims have already set up the page.
Always check "Browser is currently at:" in your context before deciding to navigate.
If already on the correct page or form, begin verifying immediately — do NOT navigate away.
Only navigate if the current URL is clearly the wrong page for this claim.

BEHAVIORAL CLAIM RULE:
When test data is provided, use the data key names as hints for which field to interact with
(e.g. data key "requesting_agency" → fill the "Requesting Agency" field with that value;
"division" → fill the "Division" field; "label" → fill the "Label" field).
Perform the minimal interaction needed to reach the expected state, then verify and stop.
Do NOT fill other fields. Do not verify side-effects not mentioned in the expected outcome.

Interaction rules:
- For autocomplete/lookup fields (Agency, Division, Funding Type, Procurement Method, Vendor):
  call fill_field(field_label, value) DIRECTLY — do NOT click the field first.
  After fill_field, call read_page to see the suggestion list, then click the matching suggestion.
- For combobox/select fields: use select_option first; fall back to fill_field if select_option fails.
- NEVER click a field label to open a dropdown — clicking a label opens a modal popup. Always use fill_field.
- If a "See All" modal popup opened accidentally, close it with click("Cancel") or click("Close"), then use fill_field directly.
- Strip asterisks from field labels when passing to tools — use "Requesting Agency" not "Requesting Agency *".
- If test data is provided, use those exact values without modification.
- For date fields: use fill_field with the date label for the START date and fill_field("to", value) for the END date (the end date is under the "to:" heading). NEVER click the calendar icon. After filling a date, move on immediately — the calendar dismisses automatically.
- For Yes/No radio questions: ALWAYS call click("Yes") or click("No") — never include the question text in element_description. The click engine finds the right unchecked radio automatically. If multiple Yes/No questions are visible, answer only the one relevant to this claim's expected outcome.
- read_page shows a focused view centred on the last field you filled or clicked. If a field you need is NOT visible in the snapshot, it may be further down the form — call read_page once more after interacting with a nearby field. Do NOT call read_page more than twice in a row without taking a different action between them.
- CRITICAL — no read_page loops: if read_page returns content identical or nearly identical to the previous result, do NOT call read_page again. Instead: (a) take a screenshot, (b) try a fill_field or click you can see, or (c) call verify_claim with verdict=unverifiable. Repeated read_page with no action wastes steps and blocks the claim.
- IMPORTANT — keyboard scrolling has NO effect on read_page: read_page captures the full semantic DOM regardless of scroll position. Never press End, PageDown, or ArrowDown before read_page — it has no effect. If you see a '[... N lines below ...]' marker, call read_page directly to see that content.
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
