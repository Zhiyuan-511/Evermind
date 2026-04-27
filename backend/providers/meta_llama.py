"""Meta Llama provider (via Together / Groq / Fireworks hosts).

Model names are used as the matching key since users pick a Llama model
first, then Evermind picks the best available host. Default host is
Together because it exposes the widest parameter surface (reasoning,
safety_model, repetition_penalty, seed, …).

The provider is OpenAI-compatible by design on every host. Quirks we
normalize:
    - Same model on different hosts can behave differently (FP8 vs BF16
      quantization, context truncation policy). Evermind doesn't try to
      hide that — the user chooses the model string explicitly.
    - Groq supports `service_tier` (auto | on_demand | flex | performance);
      we pass it through via `extra`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .base import ChatRequest, OpenAICompatProvider, ProviderRetryHint
from .registry import register_provider

logger = logging.getLogger("evermind.providers.meta_llama")

_DEFAULT_ENDPOINT = "https://api.together.xyz/v1"


@register_provider
class MetaLlamaProvider(OpenAICompatProvider):
    name = "llama"
    display_name = "Meta Llama"
    default_endpoint = _DEFAULT_ENDPOINT
    supports_tool_use = True

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return (
            mn.startswith("meta-llama/")
            or mn.startswith("llama")
            or mn.startswith("together/")
            or mn.startswith("groq/")
            or mn.startswith("fireworks/")
        )

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        body = super().normalize_request(req)
        # Together's "reasoning" extension (passed verbatim via extra)
        if req.want_thinking and "reasoning" not in body:
            body["reasoning"] = {"enabled": True, "effort": "medium"}
        return body

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        if status in (400, 401, 403):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 422:
            # Often means "model doesn't support tool_call" — caller should
            # drop tools and retry, which we signal via unique reason.
            return ProviderRetryHint(False, 0.0, "unsupported-feature")
        if status == 429:
            return ProviderRetryHint(attempt <= 3, min(15.0, 3.0 * attempt), "rate-limit")
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
