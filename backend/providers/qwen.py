"""Aliyun Qwen (DashScope) provider.

Endpoint: https://dashscope.aliyuncs.com/compatible-mode/v1 (CN) /
          https://dashscope-intl.aliyuncs.com/compatible-mode/v1 (overseas)

Critical rules discovered during research:
    - Non-streaming + enable_thinking=True → 400.
    - Commercial Qwen (max/plus/flash/turbo) defaults thinking=off.
    - Open-source Qwen defaults thinking=on.
    - `qwen3-next-80b-a3b-thinking` is thinking-ONLY; `enable_thinking=False` is ignored.
Evermind defaults `enable_thinking=False` and forces `stream=True` to
avoid the commonest failure modes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .base import ChatRequest, OpenAICompatProvider, ProviderRetryHint
from .registry import register_provider

logger = logging.getLogger("evermind.providers.qwen")

_DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1"

_THINKING_ONLY_MODELS = {
    "qwen3-next-80b-a3b-thinking",
    "qwq-32b",
}


@register_provider
class QwenProvider(OpenAICompatProvider):
    name = "qwen"
    display_name = "Alibaba Qwen"
    default_endpoint = _DEFAULT_ENDPOINT
    supports_tool_use = True
    supports_thinking = True
    supports_prompt_cache = False

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return mn.startswith("qwen") or mn.startswith("qwq") or mn.startswith("tongyi")

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        body = super().normalize_request(req)
        body["stream"] = True  # non-streaming fails with thinking param
        extra_body = body.setdefault("extra_body", {}) if "extra_body" in body else {}
        # Merge any caller-provided extra_body
        if "extra_body" not in body:
            extra_body = {}
        else:
            extra_body = body["extra_body"]
        is_thinking_only = str(req.model).lower() in _THINKING_ONLY_MODELS
        if is_thinking_only:
            logger.debug("Qwen thinking-only model %s — not setting enable_thinking", req.model)
        else:
            extra_body.setdefault("enable_thinking", bool(req.want_thinking))
        body["extra_body"] = extra_body
        # Qwen commercial models perform badly at temperature=0 (degenerate loops)
        if body.get("temperature") in (0, 0.0):
            body["temperature"] = 0.7
        return body

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        if "enable_thinking must be set to false for non-streaming" in msg:
            return ProviderRetryHint(False, 0.0, "thinking-param-mismatch")
        if status in (400, 401, 403, 422):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 429:
            return ProviderRetryHint(attempt <= 3, min(10.0, 2.0 * (2 ** (attempt - 1))), "rate-limit")
        if status in (500, 502, 503):
            return ProviderRetryHint(attempt <= 2, 2.5, f"server {status}")
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
