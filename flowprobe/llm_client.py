from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import litellm
import yaml as _yaml
from flowprobe.logger import get_logger

log = get_logger(__name__)

# Matches AWS AccessDeniedException bodies, e.g.
#   "User: arn:... is not authorized to perform: bedrock:ApplyGuardrail
#    on resource: arn:aws:bedrock:us-east-2:...:guardrail/xyz because ..."
_ACCESS_DENIED_RE = re.compile(
    r"not authorized to perform:\s*(?P<action>[\w:]+)"
    r"(?:\s+on resource:\s*(?P<arn>\S+))?"
)


def _parse_access_denied(err: str) -> tuple[str, str] | None:
    """Return (action, resource_arn) if the error is an AWS AccessDenied, else None."""
    if "not authorized to perform" not in err:
        return None
    m = _ACCESS_DENIED_RE.search(err)
    if not m:
        return None
    return m.group("action"), (m.group("arn") or "(no resource in message)")


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
    def __init__(self, platform_guidance: str = "") -> None:
        from flowprobe.config import settings
        self._reasoning_model: str = settings.reasoning_model
        self._fast_model: str = settings.fast_model
        self._max_tokens: int = settings.max_tokens
        self._cache: bool = settings.cache_system_prompt
        self._log_prompts: bool = settings.log_llm_prompts
        self._log_responses: bool = settings.log_llm_responses
        self._log_msg_max: int = settings.log_message_max_chars
        self._log_resp_max: int = settings.log_response_max_chars
        # Level axis (orthogonal to what/truncation): where prompt & response logs
        # emit. "info" surfaces them on the console; "debug" keeps them for the
        # structured pipeline only.
        self._log_emit = log.info if settings.log_llm_level.lower() == "info" else log.debug
        self._aws_region: str = settings.aws_region

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

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        """Apply the truncation axis: -1 = full, otherwise cap at max_chars."""
        if max_chars != -1 and len(text) > max_chars:
            return text[:max_chars] + f" … [TRUNCATED — {len(text)} chars total, showing {max_chars}]"
        return text

    def _log_request(
        self,
        model: str,
        messages: list[dict],
        call_type: str,
        context: dict | None = None,
        include_system_and_tools: bool = False,
    ) -> None:
        """Log the outgoing request. What: gated by log_llm_prompts. Level: emitted
        via self._log_emit (log_llm_level). Truncation: log_message_max_chars (-1 =
        full). When include_system_and_tools is set (reasoning calls), the system
        prompt and tool count are logged too, so the record reflects the full payload
        that actually goes to the provider. Emitted before the API call, so it is
        visible even when the request is content-filtered or errors."""
        if not self._log_prompts:
            return
        conversation: list[dict[str, str]] = []
        if include_system_and_tools:
            sys_text = "".join(
                p.get("text", "") for p in self._system_message.get("content", [])
                if isinstance(p, dict)
            )
            conversation.append({"role": "system", "content": self._truncate(sys_text, self._log_msg_max)})
            conversation.append({"role": "tools", "content": f"{len(_TOOL_DEFINITIONS)} tool definitions attached"})
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                # tool result or multi-part — extract text portions
                parts = [p.get("text", str(p)) for p in content if isinstance(p, dict)]
                content = " | ".join(parts)
            text = self._truncate(str(content), self._log_msg_max)
            if m.get("tool_calls"):
                tcs = [f"{t['function']['name']}({t['function']['arguments']})"
                       for t in m["tool_calls"]]
                text = (text + f"  tool_calls={tcs}").strip()
            conversation.append({"role": role, "content": text})

        self._log_emit(
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
        finish_reason = getattr(choice, "finish_reason", None)

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
                "content": self._truncate(msg.content or "", self._log_resp_max),
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
            self._log_emit(
                "llm_response",
                extra={
                    "event": "llm_response",
                    "call_type": call_type,
                    "model": model,
                    "duration_ms": duration_ms,
                    "finish_reason": finish_reason,
                    "response": response_summary,
                    **token_info,
                    **(context or {}),
                },
            )

        # Always log token usage at INFO so it's visible without debug mode.
        # finish_reason included — 'content_filter' with 0 tokens is the Bedrock
        # content-filter signature, so surfacing it here makes filter blocks obvious.
        log.info(
            "llm tokens — in=%d out=%d cache_read=%d duration=%dms finish=%s tool=%s",
            token_info.get("input_tokens", 0),
            token_info.get("output_tokens", 0),
            token_info.get("cache_read_tokens", 0),
            duration_ms,
            finish_reason,
            response_summary.get("tool", "text"),
            extra={
                "event": "llm_tokens",
                "model": model,
                "finish_reason": finish_reason,
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
        self._log_request(self._reasoning_model, messages, "reason", context,
                          include_system_and_tools=True)

        # Provider-specific kwargs. The anthropic-beta prompt-caching header is a
        # native-Anthropic-API construct — Bedrock rejects/ignores it and instead
        # honours the cache_control markers already set on the system message and
        # tool definitions, which LiteLLM translates to Bedrock cache points. On
        # Bedrock we also pin the region (None → boto3 default credential chain).
        is_bedrock = self._reasoning_model.startswith("bedrock/")
        extra: dict[str, Any] = {}
        if self._cache and not is_bedrock:
            extra["extra_headers"] = {"anthropic-beta": "prompt-caching-2024-07-31"}
        if is_bedrock and self._aws_region:
            extra["aws_region_name"] = self._aws_region

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
                    **extra,
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
                # IAM / permission errors never succeed on retry — fail fast with the
                # exact missing action + ARN instead of burning 90s on three retries.
                denied = _parse_access_denied(str(e))
                if denied:
                    action, arn = denied
                    log.error(
                        "llm IAM permission denied — action=%s resource=%s. "
                        "Add this action to the execution role; retrying will not help.",
                        action, arn,
                        extra={"event": "llm_access_denied", "missing_action": action,
                               "resource": arn, "model": self._reasoning_model,
                               "error": str(e), **(context or {})},
                    )
                    raise RuntimeError(
                        f"Bedrock/LLM access denied: role is missing '{action}' on '{arn}'. "
                        f"Grant this IAM action (region-wildcard for cross-region inference profiles)."
                    ) from e
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

        extra: dict[str, Any] = {}
        if self._fast_model.startswith("bedrock/") and self._aws_region:
            extra["aws_region_name"] = self._aws_region

        t0 = time.monotonic()
        response = litellm.completion(
            model=self._fast_model,
            messages=messages,
            max_tokens=1024,
            **extra,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        self._log_response(self._fast_model, response, "extract", duration_ms, context)
        return response.choices[0].message.content or ""
