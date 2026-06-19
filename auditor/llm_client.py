from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import litellm
import yaml as _yaml
from dotenv import load_dotenv

from auditor.logger import get_logger

load_dotenv()

log = get_logger(__name__)


def _load_prompts() -> tuple[str, list]:
    base = Path(__file__).parent / "prompts"
    system = (base / "system_prompt.md").read_text()
    tools = _yaml.safe_load((base / "tool_definitions.yaml").read_text())
    # Mark the last tool with cache_control so Anthropic caches system + all tools
    # as a single prefix. Without this, tool definitions (~2K tokens) are re-sent
    # and re-billed on every call even when the system prompt cache hits.
    if tools:
        last = tools[-1]
        fn = last.get("function", last)
        fn.setdefault("cache_control", {"type": "ephemeral"})
    return system, tools


_SYSTEM_PROMPT, _TOOL_DEFINITIONS = _load_prompts()


class LLMClient:
    def __init__(self, config: dict[str, Any], platform_guidance: str = "") -> None:
        self._reasoning_model: str = config["reasoning_model"]
        self._fast_model: str = config["fast_model"]
        self._max_tokens: int = config.get("max_tokens", 4096)
        self._cache: bool = config.get("cache_system_prompt", True)
        self._log_prompts: bool = config.get("log_llm_prompts", True)
        self._log_responses: bool = config.get("log_llm_responses", True)

        system_text = _SYSTEM_PROMPT
        if platform_guidance:
            system_text = system_text + "\n" + platform_guidance.strip()

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": system_text,
                **({"cache_control": {"type": "ephemeral"}} if self._cache else {}),
            }
        ]
        self._system_message: dict[str, Any] = {"role": "system", "content": content}

    # ── internal helpers ──────────────────────────────────────────────────────

    def _log_request(
        self,
        model: str,
        messages: list[dict],
        call_type: str,
        context: dict | None = None,
    ) -> None:
        if not self._log_prompts:
            return
        # Build a clean, readable view of the conversation — last 5 messages
        # are most relevant; include all but cap at 10 to avoid huge log lines
        trimmed = messages[-10:] if len(messages) > 10 else messages
        conversation = []
        for m in trimmed:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                # tool result or multi-part — extract text portions
                parts = [p.get("text", str(p)) for p in content if isinstance(p, dict)]
                content = " | ".join(parts)
            conversation.append({"role": role, "content": str(content)[:2000]})

        log.debug(
            "llm_request",
            extra={
                "event": "llm_request",
                "call_type": call_type,
                "model": model,
                "messages_count": len(messages),
                "conversation": conversation,
                **(context or {}),
            },
        )

    def _log_response(
        self,
        model: str,
        response: Any,
        call_type: str,
        duration_ms: int,
        context: dict | None = None,
    ) -> None:
        choice = response.choices[0]
        msg = choice.message

        # Extract what the LLM decided to do
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            response_summary = {
                "type": "tool_call",
                "tool": tc.function.name,
                "args": tc.function.arguments,  # raw JSON string
            }
        else:
            response_summary = {
                "type": "text",
                "content": (msg.content or "")[:3000],
            }

        usage = getattr(response, "usage", None)
        token_info = {}
        if usage:
            token_info = {
                "input_tokens": getattr(usage, "prompt_tokens", 0),
                "output_tokens": getattr(usage, "completion_tokens", 0),
                "cache_read_tokens": getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
            }

        if self._log_responses:
            log.debug(
                "llm_response",
                extra={
                    "event": "llm_response",
                    "call_type": call_type,
                    "model": model,
                    "duration_ms": duration_ms,
                    "response": response_summary,
                    **token_info,
                    **(context or {}),
                },
            )

        # Always log token usage at INFO so it's visible without debug mode
        log.info(
            "llm tokens — in=%d out=%d cache_read=%d duration=%dms tool=%s",
            token_info.get("input_tokens", 0),
            token_info.get("output_tokens", 0),
            token_info.get("cache_read_tokens", 0),
            duration_ms,
            response_summary.get("tool", "text"),
            extra={
                "event": "llm_tokens",
                "model": model,
                **token_info,
                "duration_ms": duration_ms,
                **(context or {}),
            },
        )

    # ── public API ────────────────────────────────────────────────────────────

    def reason(
        self,
        messages: list[dict[str, Any]],
        context: dict | None = None,
    ) -> Any:
        """Call the reasoning model. context dict is logged as structured fields."""
        self._log_request(self._reasoning_model, messages, "reason", context)

        for attempt in range(3):
            t0 = time.monotonic()
            try:
                response = litellm.completion(
                    model=self._reasoning_model,
                    messages=[self._system_message] + messages,
                    tools=_TOOL_DEFINITIONS,
                    tool_choice="auto",
                    max_tokens=self._max_tokens,
                    timeout=120,
                    **({"extra_headers": {"anthropic-beta": "prompt-caching-2024-07-31"}} if self._cache else {}),
                )
                duration_ms = int((time.monotonic() - t0) * 1000)
                self._log_response(self._reasoning_model, response, "reason", duration_ms, context)
                return response
            except litellm.RateLimitError:
                wait = 60 * (attempt + 1)
                log.warning("llm rate limit — waiting %ds (attempt %d/3)", wait, attempt + 1,
                            extra={"event": "llm_rate_limit", "wait_s": wait, "attempt": attempt + 1})
                time.sleep(wait)
            except (litellm.APIConnectionError, litellm.Timeout,
                    litellm.APIError, Exception) as e:
                wait = 30 * (attempt + 1)
                log.warning("llm error %s: %s — waiting %ds (attempt %d/3)",
                            type(e).__name__, e, wait, attempt + 1,
                            extra={"event": "llm_error", "error_type": type(e).__name__,
                                   "error": str(e), "wait_s": wait, "attempt": attempt + 1})
                time.sleep(wait)

        raise RuntimeError("LLM call failed: exhausted 3 retries")

    def extract(self, prompt: str, context: dict | None = None) -> str:
        """Call the fast model for simple extraction tasks."""
        messages = [{"role": "user", "content": prompt}]
        self._log_request(self._fast_model, messages, "extract", context)

        t0 = time.monotonic()
        response = litellm.completion(
            model=self._fast_model,
            messages=messages,
            max_tokens=1024,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        self._log_response(self._fast_model, response, "extract", duration_ms, context)
        return response.choices[0].message.content or ""
