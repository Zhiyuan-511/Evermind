"""OpenAI provider (GPT-4 / GPT-5 / o-series).

Two API surfaces:
    - /v1/chat/completions — legacy, still fully supported
    - /v1/responses        — new, required for latest reasoning controls

For Evermind v6.0 we keep the Chat Completions path as the default so
existing relay stations (which only speak chat/completions) keep working.
`reasoning_effort` and `response_format` are passed through for Responses
compatibility, though enabling the Responses API fully is a V6.1 target.

Key behaviours:
    - o1 / o3 / o4 reasoning models: `max_tokens` renamed to
      `max_completion_tokens`; temperature is ignored; need larger budgets.
    - `reasoning_effort`: low / medium / high / xhigh (new in GPT-5.x).
    - Streaming deltas are standard OpenAI; `reasoning_tokens` appears
      in the final usage block.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .base import ChatRequest, OpenAICompatProvider, ProviderRetryHint
from .registry import register_provider

logger = logging.getLogger("evermind.providers.openai")

_DEFAULT_ENDPOINT = "https://api.openai.com/v1"

_REASONING_MODEL_PREFIXES = (
    "o1", "o3", "o4",
    "gpt-5",  # GPT-5.x has built-in reasoning controls
)


@register_provider
class OpenAIProvider(OpenAICompatProvider):
    name = "openai"
    display_name = "OpenAI"
    default_endpoint = _DEFAULT_ENDPOINT
    supports_tool_use = True
    supports_thinking = True
    supports_prompt_cache = True  # server-side automatic for prefixes ≥1024 tokens

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return (
            mn.startswith("gpt-")
            or mn.startswith("gpt3")
            or mn.startswith("gpt4")
            or mn.startswith("gpt5")
            or mn.startswith("o1")
            or mn.startswith("o3")
            or mn.startswith("o4")
            or mn.startswith("openai/")
            or mn in {"davinci", "babbage", "chatgpt-4o-latest"}
        )

    def _is_reasoning_model(self, model: str) -> bool:
        mn = str(model or "").lower()
        return any(mn.startswith(prefix) for prefix in _REASONING_MODEL_PREFIXES)

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        body = super().normalize_request(req)
        if self._is_reasoning_model(req.model):
            # o-series + gpt-5 use max_completion_tokens, not max_tokens
            if "max_tokens" in body:
                body["max_completion_tokens"] = body.pop("max_tokens")
            # reasoning models ignore temperature/top_p
            body.pop("temperature", None)
            body.pop("top_p", None)
            # Expose reasoning_effort when caller asked for thinking
            if req.want_thinking and "reasoning_effort" not in body:
                body["reasoning_effort"] = "medium"
        return body

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        if status in (400, 401, 403, 422):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 404:
            return ProviderRetryHint(False, 0.0, "model-not-found")
        if status == 429:
            # OpenAI honours retry-after; mirror that via backoff
            return ProviderRetryHint(attempt <= 3, min(30.0, 4.0 * (2 ** (attempt - 1))), "rate-limit")
        if status in (500, 502, 503, 504):
            return ProviderRetryHint(attempt <= 2, 2.5 * attempt, f"server {status}")
        if any(tok in msg for tok in ("timeout", "connection", "reset", "apitimeout")):
            return ProviderRetryHint(attempt <= 2, 2.0 * attempt, "transient")
        return ProviderRetryHint(False, 0.0, "unknown")


def _extract_status(msg: str) -> Optional[int]:
    import re
    m = re.search(r"\b(4\d\d|5\d\d)\b", msg)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None
