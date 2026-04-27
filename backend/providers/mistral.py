"""Mistral provider.

Endpoints:
    - Chat: https://api.mistral.ai/v1/chat/completions
    - FIM (Codestral): https://codestral.mistral.ai/v1/fim/completions (separate host!)

OpenAI-compatible schema overall. Key quirks:
    - `tool_choice`: auto | any | required | none (`any` ≡ OpenAI `required`).
    - `response_format`: json_object | json_schema; schema mode requires
      prompt-level instruction to emit JSON.
    - Pixtral multimodal uses OpenAI-style image_url content blocks.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .base import ChatRequest, OpenAICompatProvider, ProviderRetryHint
from .registry import register_provider

logger = logging.getLogger("evermind.providers.mistral")

_DEFAULT_ENDPOINT = "https://api.mistral.ai/v1"
_CODESTRAL_ENDPOINT = "https://codestral.mistral.ai/v1"


@register_provider
class MistralProvider(OpenAICompatProvider):
    name = "mistral"
    display_name = "Mistral"
    default_endpoint = _DEFAULT_ENDPOINT
    supports_tool_use = True

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return (
            mn.startswith("mistral")
            or mn.startswith("codestral")
            or mn.startswith("open-mistral")
            or mn.startswith("pixtral")
            or mn.startswith("open-mixtral")
            or mn.startswith("ministral")
        )

    def endpoint_for_model(self, model: str) -> str:
        mn = str(model or "").lower()
        if mn.startswith("codestral") and "fim" in mn:
            return _CODESTRAL_ENDPOINT
        return _DEFAULT_ENDPOINT

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        body = super().normalize_request(req)
        # Mistral accepts "any" but we want to keep OpenAI compat
        if body.get("tool_choice") == "required":
            body["tool_choice"] = "any"
        return body

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        if status in (400, 401, 403, 422):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 429:
            return ProviderRetryHint(attempt <= 3, min(15.0, 2.5 * attempt), "rate-limit")
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
