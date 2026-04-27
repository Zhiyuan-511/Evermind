"""xAI Grok provider.

Endpoint: https://api.x.ai/v1 (OpenAI-compatible)
Also: https://api.x.ai/v1/responses for server-side tools (web_search/x_search)

Key differences from OpenAI:
    - `reasoning_content` only on reasoning models.
    - `reasoning_effort: low | medium | high` (no xhigh).
    - Legacy Live Search `search_parameters` was sunset 2026-01-12.
      Must use Responses API + tools for live search.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .base import ChatRequest, OpenAICompatProvider, ProviderRetryHint
from .registry import register_provider

logger = logging.getLogger("evermind.providers.xai")

_DEFAULT_ENDPOINT = "https://api.x.ai/v1"


@register_provider
class XAIProvider(OpenAICompatProvider):
    name = "xai"
    display_name = "xAI Grok"
    default_endpoint = _DEFAULT_ENDPOINT
    supports_tool_use = True
    supports_thinking = True

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return (
            mn.startswith("grok")
            or mn.startswith("xai/")
            or mn.startswith("x-ai/")
        )

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        body = super().normalize_request(req)
        if "reasoning" in req.model.lower() and req.want_thinking:
            body.setdefault("reasoning_effort", "medium")
        return body

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        if status == 410 or "search_parameters" in msg:
            return ProviderRetryHint(False, 0.0, "xai-live-search-sunset")
        if status in (400, 401, 403, 422):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 429:
            return ProviderRetryHint(attempt <= 3, min(20.0, 3.0 * attempt), "rate-limit")
        if status in (500, 502, 503, 504):
            return ProviderRetryHint(attempt <= 2, 2.5 * attempt, f"server {status}")
        if any(tok in msg for tok in ("timeout", "connection", "reset")):
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
