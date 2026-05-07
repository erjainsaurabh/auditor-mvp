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
