"""DeepSeek provider.

Key behaviours:
    - Official endpoint: https://api.deepseek.com
    - `deepseek-reasoner` emits `delta.reasoning_content` BEFORE `delta.content`.
    - Multi-turn history rule (V3.2+):
        - Tool-call branch: must include `reasoning_content` in the
          previous assistant message or the next turn 400s.
        - Non-tool-call branch: must NOT include `reasoning_content`
          or the next turn 400s.
      These rules flipped in the v3.2 release and broke every integration
      that wasn't aware (see https://github.com/n8n-io/n8n/issues/22579).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import BaseProvider, ChatRequest, OpenAICompatProvider, ProviderRetryHint
from .registry import register_provider

logger = logging.getLogger("evermind.providers.deepseek")


@register_provider
class DeepSeekProvider(OpenAICompatProvider):
    name = "deepseek"
    display_name = "DeepSeek"
    default_endpoint = "https://api.deepseek.com"
    supports_tool_use = True
    supports_thinking = True
    supports_prompt_cache = False

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return mn.startswith("deepseek") or mn in {"ds-chat", "ds-coder", "ds-reasoner"}

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        body = super().normalize_request(req)
        if "deepseek-reasoner" in req.model.lower() or "r1" in req.model.lower():
            # Reasoner ignores temperature/top_p anyway; drop noise to avoid
            # confusing the server-side guardrails.
            body.pop("temperature", None)
            body.pop("top_p", None)
        return body

    def build_next_turn(
        self,
        history: List[Dict[str, Any]],
        last_assistant: Dict[str, Any],
        *,
        tool_results: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Apply the tool vs non-tool reasoning_content rule."""
        messages = list(history)
        reasoning = last_assistant.get("reasoning_content") or ""
        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": last_assistant.get("content") or "",
        }
        if last_assistant.get("tool_calls"):
            assistant_msg["tool_calls"] = last_assistant["tool_calls"]
            if not assistant_msg["content"]:
                assistant_msg["content"] = None
            if reasoning:
                # REQUIRED in tool-call branch (v3.2+)
                assistant_msg["reasoning_content"] = reasoning
        # else: non-tool branch — NEVER include reasoning_content
        messages.append(assistant_msg)

        if tool_results:
            for r in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": r.get("tool_call_id") or r.get("id") or "",
                    "content": r.get("content") or r.get("output") or "",
                })
        return messages

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        # 400/401/402/422 → fatal
        if status in (400, 401, 402, 422):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 429:
            backoff = min(15.0, 1.0 * (2 ** (attempt - 1)))
            return ProviderRetryHint(attempt <= 3, backoff, "rate-limit")
        if status in (500, 502, 503, 504):
            backoff = min(10.0, 1.5 * attempt)
            return ProviderRetryHint(attempt <= 2, backoff, f"server {status}")
        # network / timeout
        if any(tok in msg for tok in ("timeout", "connection", "ssl", "reset")):
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
