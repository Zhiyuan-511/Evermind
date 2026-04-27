"""Moonshot Kimi provider.

Three endpoints in practice:
    - api.moonshot.ai/v1           — general chat
    - api.kimi.com/coding/v1       — Kimi For Coding (UA=claude-code required)
    - api.moonshot.ai/anthropic    — Claude Code compatibility layer
The first two are OpenAI-compatible; the third is Anthropic-compatible
and is handled by the AnthropicProvider when present.

Key behaviours (for OpenAI-compat path):
    - Supports `prompt_cache_key` to reuse prefix cache across calls.
    - Tool-name regex is the strictest in the industry; validate upstream.
    - `partial: true` on the last assistant message activates continuation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .base import ChatRequest, OpenAICompatProvider, ProviderRetryHint
from .registry import register_provider

logger = logging.getLogger("evermind.providers.kimi")

_CODING_ENDPOINT = "https://api.kimi.com/coding/v1"
_GENERAL_ENDPOINT = "https://api.moonshot.ai/v1"
_CODING_UA = "claude-code"


@register_provider
class KimiProvider(OpenAICompatProvider):
    name = "kimi"
    display_name = "Moonshot Kimi"
    default_endpoint = _GENERAL_ENDPOINT
    supports_tool_use = True
    supports_thinking = True
    supports_prompt_cache = True

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return (
            mn.startswith("kimi")
            or mn.startswith("moonshot")
            or mn in {"kimi-coding", "kimi-for-coding"}
        )

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        body = super().normalize_request(req)
        if req.session_id or req.node_name:
            key_parts = [p for p in (req.session_id, req.node_name) if p]
            cache_key = ":".join(key_parts)
            body.setdefault("prompt_cache_key", cache_key)
        # Kimi recommends temperature=0.6 for reasoning stability
        if body.get("temperature") is None:
            body["temperature"] = 0.6
        return body

    def _is_coding_model(self, model: str) -> bool:
        mn = str(model or "").lower()
        return "coding" in mn or mn.startswith("kimi-for-coding")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Auto-select endpoint if caller didn't override
        if not kwargs.get("api_base"):
            # api_base stays at general; coding is set per-request because
            # we don't know the model until normalize_request runs.
            pass

    def endpoint_for_model(self, model: str) -> str:
        return _CODING_ENDPOINT if self._is_coding_model(model) else _GENERAL_ENDPOINT

    def headers_for_model(self, model: str) -> Dict[str, str]:
        headers = dict(self.extra_headers)
        if self._is_coding_model(model):
            headers.setdefault("User-Agent", _CODING_UA)
        return headers

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        if status in (400, 401, 403, 404, 422):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 429:
            return ProviderRetryHint(attempt <= 3, min(8.0, attempt * 2.0), "rate-limit")
        if status in (500, 502, 503):
            return ProviderRetryHint(attempt <= 2, 2.0, f"server {status}")
        if any(tok in msg for tok in ("timeout", "connection", "reset")):
            return ProviderRetryHint(attempt <= 2, 1.5 * attempt, "transient")
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
