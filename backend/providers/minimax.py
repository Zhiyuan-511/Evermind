"""MiniMax provider.

This is the provider responsible for narration-only failures Evermind
saw repeatedly in v5.8.6 — see `MEMORY.md project_v587_narration_guard.md`.

Root cause research (see docs/research):
    - Default MiniMax streaming embeds CoT as `<think>...</think>` inside
      `delta.content`. Relays that don't strip those tags forward them
      as the deliverable — hence "narration-only" output.
    - OpenAI-compat mode supports `reasoning_split=True` which lifts the
      thinking into a separate `reasoning_details` field.
    - Native endpoint expects `GroupId` as a URL query string (not a
      header), which many relays forget.

Evermind strategy:
    - Default to OpenAI-compat + `reasoning_split=True`.
    - If `<think>` leaks through anyway, strip it client-side in
      `parse_stream_chunk`.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from .base import BaseProvider, ChatChunk, ChatRequest, OpenAICompatProvider, ProviderRetryHint
from .registry import register_provider

logger = logging.getLogger("evermind.providers.minimax")

_DEFAULT_ENDPOINT = "https://api.minimax.io/v1"

_THINK_OPEN = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think>", re.IGNORECASE)


@register_provider
class MiniMaxProvider(OpenAICompatProvider):
    name = "minimax"
    display_name = "MiniMax"
    default_endpoint = _DEFAULT_ENDPOINT
    supports_tool_use = True
    supports_thinking = True

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return mn.startswith("minimax") or mn.startswith("abab") or mn.startswith("m2")

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        body = super().normalize_request(req)
        extra_body = body.setdefault("extra_body", {}) if "extra_body" in body else {}
        if "extra_body" not in body:
            extra_body = {}
        else:
            extra_body = body["extra_body"]
        extra_body.setdefault("reasoning_split", True)  # lift <think> out of content
        body["extra_body"] = extra_body
        if body.get("temperature") is None:
            body["temperature"] = 1.0
        if body.get("top_p") is None:
            body["top_p"] = 0.95
        return body

    def parse_stream_chunk(self, raw_event: Any) -> Optional[ChatChunk]:
        """Strip any stray <think> tags if the server didn't honour reasoning_split."""
        chunk = super().parse_stream_chunk(raw_event)
        if chunk is None:
            return None
        if chunk.content_delta:
            stripped, reasoning_extra = _strip_think_tags(chunk.content_delta)
            if stripped != chunk.content_delta:
                chunk.content_delta = stripped
                if reasoning_extra:
                    chunk.reasoning_delta = (chunk.reasoning_delta or "") + reasoning_extra
        return chunk

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        # MiniMax-specific codes
        if any(code in msg for code in ("1001", "timeout")):
            return ProviderRetryHint(attempt <= 2, 2.0 * attempt, "mm-1001")
        if "1002" in msg:
            return ProviderRetryHint(attempt <= 3, 3.0 * attempt, "mm-1002 rate-limit")
        if any(code in msg for code in ("1004", "1008", "1026", "1027", "2049", "2056")):
            return ProviderRetryHint(False, 0.0, "mm-fatal")
        if "1033" in msg:
            return ProviderRetryHint(attempt <= 2, 2.0 * attempt, "mm-1033 backend")
        if "1039" in msg:
            return ProviderRetryHint(False, 0.0, "mm-1039 token-limit")
        if "1041" in msg:
            return ProviderRetryHint(False, 0.0, "mm-1041 concurrency")
        # HTTP fallback
        if status in (400, 401, 403, 422):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 429:
            return ProviderRetryHint(attempt <= 3, 3.0 * attempt, "rate-limit")
        if status in (500, 502, 503):
            return ProviderRetryHint(attempt <= 2, 2.0 * attempt, f"server {status}")
        if any(tok in msg for tok in ("timeout", "connection", "reset")):
            return ProviderRetryHint(attempt <= 2, 2.0 * attempt, "transient")
        return ProviderRetryHint(False, 0.0, "unknown")


def _strip_think_tags(text: str) -> tuple[str, str]:
    """Return (cleaned_content, extracted_reasoning)."""
    if "<think" not in text.lower():
        return text, ""
    # Greedy block extraction: everything between first <think> and </think>
    out = []
    reasoning = []
    cursor = 0
    while True:
        m_open = _THINK_OPEN.search(text, cursor)
        if not m_open:
            out.append(text[cursor:])
            break
        out.append(text[cursor:m_open.start()])
        m_close = _THINK_CLOSE.search(text, m_open.end())
        if not m_close:
            reasoning.append(text[m_open.end():])  # dangling open — lift rest to reasoning
            break
        reasoning.append(text[m_open.end():m_close.start()])
        cursor = m_close.end()
    return "".join(out), "".join(reasoning)


def _extract_status(msg: str) -> Optional[int]:
    m = re.search(r"\b(4\d\d|5\d\d)\b", msg)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None
