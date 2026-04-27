"""ByteDance Doubao (Volcengine Ark) provider.

Endpoint: https://ark.cn-beijing.volces.com/api/v3

Key behaviours:
    - `model` accepts either an endpoint ID (`ep-20250118155555-xxxxx`)
      or a direct model name (`doubao-seed-1-6`, `doubao-1-5-pro-32k`).
    - `thinking.type` is a strict whitelist: `enabled | disabled | auto`.
      Any other value is silently dropped by the server — this is a
      common integration trap.
    - Function calling requires the endpoint to have it enabled in the
      Volcengine console. We pass tools through unconditionally; the
      caller is responsible for confirming endpoint capability.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .base import ChatRequest, OpenAICompatProvider, ProviderRetryHint
from .registry import register_provider

logger = logging.getLogger("evermind.providers.doubao")

_DEFAULT_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3"
_VALID_THINKING_TYPES = {"enabled", "disabled", "auto"}


@register_provider
class DoubaoProvider(OpenAICompatProvider):
    name = "doubao"
    display_name = "ByteDance Doubao"
    default_endpoint = _DEFAULT_ENDPOINT
    supports_tool_use = True
    supports_thinking = True

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return (
            mn.startswith("doubao")
            or mn.startswith("ep-")  # Volcengine endpoint IDs
            or mn.startswith("volcengine")
        )

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        body = super().normalize_request(req)
        thinking = "enabled" if req.want_thinking else "disabled"
        # allow caller to override via req.extra
        caller_thinking = (req.extra or {}).get("thinking_type")
        if caller_thinking in _VALID_THINKING_TYPES:
            thinking = caller_thinking
        extra_body = body.setdefault("extra_body", {}) if "extra_body" in body else {}
        if "extra_body" not in body:
            extra_body = {}
        else:
            extra_body = body["extra_body"]
        extra_body["thinking"] = {"type": thinking}
        body["extra_body"] = extra_body
        return body

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        if status == 404 and ("endpoint" in msg or "ep-" in msg):
            return ProviderRetryHint(False, 0.0, "endpoint-not-found")
        if status in (400, 401, 403, 422):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 429:
            # Volcengine limiter is coarse — back off aggressively
            return ProviderRetryHint(attempt <= 3, min(30.0, 3.0 * (2 ** (attempt - 1))), "rate-limit")
        if status in (500, 502, 503, 504):
            return ProviderRetryHint(attempt <= 2, 3.0 * attempt, f"server {status}")
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
