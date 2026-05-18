from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import litellm
import yaml as _yaml
from dotenv import load_dotenv

load_dotenv()


def _load_prompts() -> tuple[str, list]:
    base = Path(__file__).parent / "prompts"
    system = (base / "system_prompt.md").read_text()
    tools = _yaml.safe_load((base / "tool_definitions.yaml").read_text())
    return system, tools


_SYSTEM_PROMPT, _TOOL_DEFINITIONS = _load_prompts()


class LLMClient:
    def __init__(self, config: dict[str, Any], platform_guidance: str = "") -> None:
        self._reasoning_model: str = config["reasoning_model"]
        self._fast_model: str = config["fast_model"]
        self._max_tokens: int = config.get("max_tokens", 4096)
        self._cache: bool = config.get("cache_system_prompt", True)

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

    def reason(self, messages: list[dict[str, Any]]) -> Any:
        for attempt in range(3):
            try:
                return litellm.completion(
                    model=self._reasoning_model,
                    messages=[self._system_message] + messages,
                    tools=_TOOL_DEFINITIONS,
                    max_tokens=self._max_tokens,
                    timeout=120,   # seconds — prevents indefinite hang on stalled connections
                )
            except litellm.RateLimitError:
                wait = 60 * (attempt + 1)
                print(f"    [rate limit] waiting {wait}s before retry {attempt + 1}/3…")
                time.sleep(wait)
            except (litellm.APIConnectionError, litellm.Timeout,
                    litellm.APIError, Exception) as e:
                wait = 30 * (attempt + 1)
                print(f"    [llm error] {type(e).__name__}: {e} — waiting {wait}s before retry {attempt + 1}/3…")
                time.sleep(wait)
        raise RuntimeError("LLM call failed: exhausted 3 retries")

    def extract(self, prompt: str) -> str:
        response = litellm.completion(
            model=self._fast_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        return response.choices[0].message.content or ""
