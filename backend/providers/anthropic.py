"""Anthropic provider.

Anthropic uses the Messages API (not OpenAI Chat Completions):
    POST /v1/messages
    header x-api-key: $ANTHROPIC_API_KEY
    header anthropic-version: 2023-06-01

Structural differences versus OpenAI:
    - `system` is a top-level field, not a message with role=system.
    - `messages` must strictly alternate user/assistant (no role=system).
    - `max_tokens` is REQUIRED.
    - Tools are Anthropic-native: `[{name, description, input_schema}]`.
    - Tool calls come back as content blocks `{type:"tool_use", id, name, input}`.
    - Tool results sent back as content blocks
      `{type:"tool_result", tool_use_id, content}`.
    - Thinking blocks carry a `signature`; multi-turn MUST echo it back.
    - Stream event types are completely different (message_start /
      content_block_delta / message_stop).

For v6.0, Evermind drives Anthropic via the official `anthropic` SDK.
`normalize_request` produces a Messages-API body. Callers using legacy
OpenAI-compat relays should use the OpenAIProvider instead.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import (
    BaseProvider,
    ChatChunk,
    ChatRequest,
    ProviderError,
    ProviderRetryHint,
)
from .registry import register_provider

logger = logging.getLogger("evermind.providers.anthropic")

_DEFAULT_ENDPOINT = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"


@register_provider
class AnthropicProvider(BaseProvider):
    name = "anthropic"
    display_name = "Anthropic Claude"
    default_endpoint = _DEFAULT_ENDPOINT
    supports_tool_use = True
    supports_thinking = True
    supports_prompt_cache = True

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return (
            mn.startswith("claude-")
            or mn.startswith("anthropic/")
            or mn in {"claude"}
        )

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        system_prompt = req.system_prompt or self._extract_system(req.messages)
        user_asst_messages = [m for m in req.messages if m.get("role") != "system"]

        body: Dict[str, Any] = {
            "model": req.model,
            "messages": user_asst_messages,
            "max_tokens": req.max_tokens or 4096,  # REQUIRED
        }
        if system_prompt:
            body["system"] = system_prompt
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.top_p is not None:
            body["top_p"] = req.top_p
        if req.stop:
            body["stop_sequences"] = req.stop
        if req.stream:
            body["stream"] = True
        if req.tools:
            body["tools"] = self._translate_tools(req.tools)
            if req.tool_choice is not None:
                body["tool_choice"] = self._translate_tool_choice(req.tool_choice)
        if req.want_thinking:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": max(1024, min(10000, (req.max_tokens or 4096) // 2)),
            }
        for key, value in (req.extra or {}).items():
            body[key] = value
        return body

    def parse_stream_chunk(self, raw_event: Any) -> Optional[ChatChunk]:
        """Convert Anthropic stream events to ChatChunk.

        Anthropic event types observed:
            - message_start
            - content_block_start / content_block_delta / content_block_stop
            - message_delta / message_stop
            - ping
            - error

        delta subtypes within content_block_delta:
            - text_delta               → content_delta
            - input_json_delta         → tool_call_delta arguments
            - thinking_delta           → reasoning_delta
            - signature_delta          → ignored (server-side only)
        """
        try:
            event_type = _getattr_or_key(raw_event, "type") or ""
            if event_type == "content_block_delta":
                delta = _getattr_or_key(raw_event, "delta") or {}
                delta_type = _getattr_or_key(delta, "type") or ""
                idx = int(_getattr_or_key(raw_event, "index") or 0)
                if delta_type == "text_delta":
                    return ChatChunk(
                        content_delta=_getattr_or_key(delta, "text") or "",
                        raw=raw_event,
                    )
                if delta_type == "thinking_delta":
                    return ChatChunk(
                        reasoning_delta=_getattr_or_key(delta, "thinking") or "",
                        raw=raw_event,
                    )
                if delta_type == "input_json_delta":
                    return ChatChunk(
                        tool_call_delta={
                            "index": idx,
                            "function": {
                                "name": "",  # name comes in content_block_start
                                "arguments": _getattr_or_key(delta, "partial_json") or "",
                            },
                        },
                        raw=raw_event,
                    )
            elif event_type == "content_block_start":
                block = _getattr_or_key(raw_event, "content_block") or {}
                if _getattr_or_key(block, "type") == "tool_use":
                    idx = int(_getattr_or_key(raw_event, "index") or 0)
                    return ChatChunk(
                        tool_call_delta={
                            "index": idx,
                            "id": _getattr_or_key(block, "id") or "",
                            "function": {
                                "name": _getattr_or_key(block, "name") or "",
                                "arguments": "",
                            },
                        },
                        raw=raw_event,
                    )
            elif event_type == "message_delta":
                delta = _getattr_or_key(raw_event, "delta") or {}
                usage = _getattr_or_key(raw_event, "usage")
                finish = _getattr_or_key(delta, "stop_reason")
                if finish or usage:
                    return ChatChunk(
                        finish_reason=_map_stop_reason(finish) if finish else None,
                        usage=dict(usage) if isinstance(usage, dict) else None,
                        raw=raw_event,
                    )
            elif event_type == "message_stop":
                return ChatChunk(finish_reason="stop", raw=raw_event)
            elif event_type == "error":
                err = _getattr_or_key(raw_event, "error") or {}
                logger.warning("Anthropic stream error: %s", err)
            return None
        except Exception as exc:  # pragma: no cover
            logger.debug("Anthropic parse_stream_chunk swallow: %s", exc)
            return None

    async def send(self, body: Dict[str, Any]):
        """Use the official anthropic async SDK."""
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise ProviderError(
                "anthropic SDK not installed (pip install anthropic)",
                vendor=self.name,
            ) from exc

        kwargs: Dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base and self.api_base != _DEFAULT_ENDPOINT:
            kwargs["base_url"] = self.api_base
        client = AsyncAnthropic(**kwargs)

        # The SDK's .messages.stream() manages SSE for us.
        async with client.messages.stream(**body) as stream:
            async for event in stream:
                chunk = self.parse_stream_chunk(event)
                if chunk is not None:
                    yield chunk

    def build_next_turn(
        self,
        history: List[Dict[str, Any]],
        last_assistant: Dict[str, Any],
        *,
        tool_results: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Anthropic expects full content-block arrays in multi-turn."""
        messages = list(history)
        assistant_content = last_assistant.get("content_blocks") or last_assistant.get("content") or ""
        messages.append({
            "role": "assistant",
            "content": assistant_content,
        })
        if tool_results:
            user_blocks = []
            for r in tool_results:
                user_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": r.get("tool_call_id") or r.get("id") or "",
                    "content": r.get("content") or r.get("output") or "",
                })
            messages.append({"role": "user", "content": user_blocks})
        return messages

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        if status in (400, 401, 403, 422):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 429 or "rate_limit" in msg:
            return ProviderRetryHint(attempt <= 3, min(20.0, 3.0 * (2 ** (attempt - 1))), "rate-limit")
        if status == 529 or "overloaded" in msg:
            return ProviderRetryHint(attempt <= 3, 5.0 * attempt, "anthropic-overload")
        if status in (500, 502, 503, 504):
            return ProviderRetryHint(attempt <= 2, 2.5 * attempt, f"server {status}")
        if any(tok in msg for tok in ("timeout", "connection", "reset")):
            return ProviderRetryHint(attempt <= 2, 2.0 * attempt, "transient")
        return ProviderRetryHint(False, 0.0, "unknown")

    # ── helpers ──────────────────────────────────────────────────────────
    def _extract_system(self, messages: List[Dict[str, Any]]) -> str:
        parts: List[str] = []
        for m in messages:
            if m.get("role") == "system":
                content = m.get("content") or ""
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            parts.append(b.get("text") or "")
                else:
                    parts.append(str(content))
        return "\n\n".join(p for p in parts if p)

    def _translate_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """OpenAI tools → Anthropic native tools."""
        out: List[Dict[str, Any]] = []
        for t in tools or []:
            fn = t.get("function") or t
            out.append({
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object"},
            })
        return out

    def _translate_tool_choice(self, choice: Any) -> Dict[str, Any]:
        if isinstance(choice, str):
            if choice == "required" or choice == "any":
                return {"type": "any"}
            if choice == "none":
                return {"type": "none"}
            return {"type": "auto"}
        if isinstance(choice, dict):
            if choice.get("type") == "function":
                return {"type": "tool", "name": (choice.get("function") or {}).get("name") or ""}
        return {"type": "auto"}


def _map_stop_reason(reason: Any) -> str:
    if reason is None:
        return ""
    s = str(reason).lower()
    if s == "end_turn":
        return "stop"
    if s == "tool_use":
        return "tool_calls"
    if s == "max_tokens":
        return "length"
    if s == "stop_sequence":
        return "stop"
    return s


def _getattr_or_key(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _extract_status(msg: str) -> Optional[int]:
    import re
    m = re.search(r"\b(4\d\d|5\d\d)\b", msg)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None
