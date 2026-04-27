"""Zhipu GLM provider (bigmodel.cn / z.ai).

Endpoint: https://open.bigmodel.cn/api/paas/v4 (CN)
          https://api.z.ai/api/paas/v4         (overseas)

Key behaviours:
    - `thinking` is a structured field: {"type": "enabled" | "disabled"}.
    - GLM-4.6+ defaults `thinking=enabled`; Evermind turns it off unless
      the caller opts in via `want_thinking=True` to save tokens.
    - Supports `request_id` for idempotent retries — we generate one per
      request so retries don't double-bill.
    - 4.6+ can emit `tool_calls` DURING reasoning; the base parser already
      handles interleaved `delta.reasoning_content` + `delta.tool_calls`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from .base import ChatRequest, OpenAICompatProvider, ProviderRetryHint
from .registry import register_provider

logger = logging.getLogger("evermind.providers.zhipu")

_DEFAULT_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4"


@register_provider
class ZhipuProvider(OpenAICompatProvider):
    name = "zhipu"
    display_name = "Zhipu GLM"
    default_endpoint = _DEFAULT_ENDPOINT
    supports_tool_use = True
    supports_thinking = True

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return mn.startswith("glm") or mn.startswith("chatglm") or mn.startswith("zhipu")

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        body = super().normalize_request(req)
        thinking_type = "enabled" if req.want_thinking else "disabled"
        body["thinking"] = {"type": thinking_type}
        # request_id for retry idempotency
        extra_body = body.setdefault("extra_body", {}) if "extra_body" in body else {}
        if "extra_body" not in body:
            extra_body = {}
        else:
            extra_body = body["extra_body"]
        extra_body.setdefault("request_id", uuid.uuid4().hex)
        body["extra_body"] = extra_body
        if body.get("temperature") is None:
            body["temperature"] = 1.0
        return body

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        if status in (400, 401, 403, 422):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 429 or "apireachlimiterror" in msg:
            return ProviderRetryHint(attempt <= 3, min(12.0, 2.5 * attempt), "rate-limit")
        if status == 503 or "apiserverflowexceederror" in msg:
            return ProviderRetryHint(attempt <= 3, 5.0 * attempt, "overload")
        if status in (500, 502, 504) or "apiinternalerror" in msg:
            return ProviderRetryHint(attempt <= 2, 2.0 * attempt, f"server {status}")
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
