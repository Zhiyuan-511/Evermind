"""Evermind provider plugin base classes.

One provider per vendor. The orchestrator hands off a normalized
`ChatRequest`, the provider returns an async iterator of `ChatChunk`
regardless of whether the vendor uses OpenAI chat completions, the
OpenAI responses API, Anthropic messages, Gemini generateContent,
or a home-grown endpoint.

Implementation contract:
    - `normalize_request(req)`  — translate Evermind's neutral request
      into the vendor's wire body (cache_control, thinking config,
      enable_thinking, partial_mode, GroupId URL query, etc.)
    - `send(body)`              — open the SSE / websocket stream,
      yield ChatChunk as chunks arrive.
    - `parse_stream_chunk(raw)` — single SSE line/event to ChatChunk
      (strips `<think>` tags, lifts reasoning_content, maps tool_use
      blocks to a uniform tool_call_delta).
    - `extract_content(chunks)` — terminal aggregation of a full stream.
    - `on_error_retry(err, n)`  — vendor-specific backoff ladder.
    - `build_next_turn(...)`    — handles multi-turn quirks (DeepSeek
      reasoning_content must/must-not persist, Anthropic must resend
      full content blocks, etc.).

The base class is deliberately minimal — all the vendor behaviour lives
in subclasses. Unit tests cover each subclass in isolation; the base
only verifies the invariant that a concrete subclass is fully wired.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("evermind.providers")


class ProviderError(Exception):
    """Raised when a provider call fails fatally and should not be retried."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, vendor: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.vendor = vendor


@dataclass
class ProviderRetryHint:
    """Small hint object returned by `on_error_retry`."""

    should_retry: bool
    backoff_seconds: float = 0.0
    reason: str = ""
    switch_model: Optional[str] = None  # vendor-suggested downgrade target


@dataclass
class ChatRequest:
    """Evermind's neutral chat request. Providers translate to wire format."""

    model: str
    messages: List[Dict[str, Any]]
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    stream: bool = True
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    stop: Optional[List[str]] = None

    # Evermind semantic fields — providers may use or ignore
    session_id: Optional[str] = None
    node_name: Optional[str] = None
    want_thinking: bool = False
    system_prompt: Optional[str] = None

    # Escape hatch for vendor-specific passthrough
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatChunk:
    """Uniform streaming chunk across all vendors.

    Providers map their SSE events here. Downstream consumers never have
    to care whether the source was OpenAI's `delta.content`, Anthropic's
    `content_block_delta`, Gemini's `candidates[0].content.parts`, etc.
    """

    content_delta: str = ""
    reasoning_delta: str = ""  # CoT / thinking content, kept out of dialogue history
    tool_call_delta: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None  # "stop" | "tool_calls" | "length" | "content_filter"
    usage: Optional[Dict[str, Any]] = None
    raw: Any = None  # original SSE event for debug

    @property
    def is_empty(self) -> bool:
        return (
            not self.content_delta
            and not self.reasoning_delta
            and self.tool_call_delta is None
            and self.finish_reason is None
            and self.usage is None
        )


class BaseProvider(ABC):
    """Abstract base for every provider plugin.

    Subclasses MUST define:
        - `name`: lowercase vendor slug (e.g. "kimi", "anthropic")
        - `supported_models`: list or predicate for model matching
        - `default_endpoint`: primary API base URL

    Subclasses SHOULD override:
        - `normalize_request`
        - `parse_stream_chunk`
        - `on_error_retry`

    Subclasses MAY override:
        - `build_next_turn`  (multi-turn history mutation)
        - `validate_tools`   (tool-schema sanity check)
    """

    # Metadata — required to be overridden
    name: str = ""
    display_name: str = ""
    default_endpoint: str = ""
    supports_tool_use: bool = True
    supports_thinking: bool = False
    supports_prompt_cache: bool = False

    def __init__(
        self,
        *,
        api_key: str = "",
        api_base: str = "",
        extra_headers: Optional[Dict[str, str]] = None,
        http_client: Optional[Any] = None,
    ) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__} must set `name`")
        self.api_key = api_key
        self.api_base = (api_base or self.default_endpoint).rstrip("/")
        self.extra_headers = dict(extra_headers or {})
        self.http_client = http_client  # injected for tests

    # ── Matcher ──────────────────────────────────────────────────────────
    @classmethod
    @abstractmethod
    def matches(cls, model_name: str) -> bool:
        """Return True if this provider handles the given model name.

        The registry iterates providers in registration order and asks
        each one; first match wins. Concrete providers should be
        conservative (exact vendor prefixes only) to avoid claiming
        models another vendor also supports.
        """

    # ── Request shaping ──────────────────────────────────────────────────
    @abstractmethod
    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        """Translate the neutral ChatRequest into a vendor-specific wire body."""

    # ── Streaming ────────────────────────────────────────────────────────
    @abstractmethod
    async def send(self, body: Dict[str, Any]) -> AsyncIterator[ChatChunk]:
        """Send the request and yield ChatChunk objects as the stream arrives.

        Subclasses that use the `openai` Python SDK can delegate to the
        SDK's async client; subclasses that talk to a bespoke endpoint
        (MiniMax native, Gemini) can use httpx directly — base.py does
        NOT force a transport.
        """
        raise NotImplementedError  # pragma: no cover — abstract

    @abstractmethod
    def parse_stream_chunk(self, raw_event: Any) -> Optional[ChatChunk]:
        """Convert one raw SSE event / SDK delta to a ChatChunk.

        Return None for keepalive pings or empty deltas that should be
        suppressed. Never raise — invalid lines should log and return
        None so the stream keeps flowing.
        """

    # ── Aggregation ──────────────────────────────────────────────────────
    def extract_content(
        self,
        chunks: List[ChatChunk],
    ) -> Tuple[str, str, List[Dict[str, Any]]]:
        """Aggregate a list of chunks into (content, reasoning, tool_calls).

        Default implementation concatenates deltas and merges tool-call
        deltas by `index`. Subclasses that need different aggregation
        (e.g. Anthropic content_block arrays) override.
        """
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls_by_idx: Dict[int, Dict[str, Any]] = {}

        for chunk in chunks:
            if chunk.content_delta:
                content_parts.append(chunk.content_delta)
            if chunk.reasoning_delta:
                reasoning_parts.append(chunk.reasoning_delta)
            tcd = chunk.tool_call_delta
            if isinstance(tcd, dict):
                idx = int(tcd.get("index", 0) or 0)
                slot = tool_calls_by_idx.setdefault(idx, {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if tcd.get("id"):
                    slot["id"] = tcd["id"]
                fn_delta = tcd.get("function") or {}
                if fn_delta.get("name"):
                    slot["function"]["name"] = fn_delta["name"]
                if fn_delta.get("arguments"):
                    slot["function"]["arguments"] += fn_delta["arguments"]

        tool_calls = [tool_calls_by_idx[k] for k in sorted(tool_calls_by_idx.keys())]
        return ("".join(content_parts), "".join(reasoning_parts), tool_calls)

    # ── Multi-turn history ───────────────────────────────────────────────
    def build_next_turn(
        self,
        history: List[Dict[str, Any]],
        last_assistant: Dict[str, Any],
        *,
        tool_results: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Append the assistant's response (and any tool results) to history.

        Default implementation emits a standard OpenAI-style assistant
        message. DeepSeek, MiniMax, Anthropic override because they
        have strict rules about what must/must-not persist across turns.
        """
        messages = list(history)
        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": last_assistant.get("content") or "",
        }
        if last_assistant.get("tool_calls"):
            assistant_msg["tool_calls"] = last_assistant["tool_calls"]
            if not assistant_msg["content"]:
                assistant_msg["content"] = None  # OpenAI tolerates null here
        messages.append(assistant_msg)

        if tool_results:
            for result in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": result.get("tool_call_id") or result.get("id") or "",
                    "content": result.get("content") or result.get("output") or "",
                })
        return messages

    # ── Retry policy ─────────────────────────────────────────────────────
    @abstractmethod
    def on_error_retry(
        self,
        err: Exception,
        attempt: int,
    ) -> ProviderRetryHint:
        """Decide whether to retry and how long to back off.

        Called after any raised exception during `send` or HTTP calls.
        `attempt` is 1-indexed (attempt=1 is the first retry).
        """

    # ── Tool validation ──────────────────────────────────────────────────
    def validate_tools(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[str]:
        """Check that tools conform to this provider's schema.

        Returns a human-readable error message if the schema is wrong,
        or None if everything is fine. Default implementation accepts
        the OpenAI function schema and warns on obvious anti-patterns.
        """
        if not tools:
            return None
        for tool in tools:
            if not isinstance(tool, dict):
                return f"Tool entry must be a dict, got {type(tool).__name__}"
            if tool.get("type") not in ("function", None):
                return f"Unsupported tool type: {tool.get('type')}"
            fn = tool.get("function") or {}
            name = fn.get("name") or ""
            if not name:
                return "Tool function must have a non-empty name"
            # Kimi's regex is the strictest in the industry — use it as the
            # canonical check so any tool that passes here works everywhere.
            import re as _re
            if not _re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]{1,63}", name):
                return f"Tool name {name!r} violates ^[a-zA-Z_][a-zA-Z0-9_-]{{1,63}}$"
        return None

    # ── Introspection ────────────────────────────────────────────────────
    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name.title(),
            "endpoint": self.api_base,
            "supports_tool_use": self.supports_tool_use,
            "supports_thinking": self.supports_thinking,
            "supports_prompt_cache": self.supports_prompt_cache,
        }


class OpenAICompatProvider(BaseProvider):
    """Shared machinery for providers that speak raw OpenAI chat completions.

    Subclasses can override `normalize_request` to inject vendor-specific
    params (e.g. Kimi's `prompt_cache_key`, Qwen's `enable_thinking`) and
    `parse_stream_chunk` to lift reasoning_content or strip `<think>`,
    but they inherit the SSE plumbing here.
    """

    supports_tool_use = True

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": req.model,
            "messages": list(req.messages),
            "stream": bool(req.stream),
        }
        if req.tools:
            body["tools"] = req.tools
        if req.tool_choice is not None:
            body["tool_choice"] = req.tool_choice
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.top_p is not None:
            body["top_p"] = req.top_p
        if req.stop:
            body["stop"] = req.stop
        if req.stream:
            body["stream_options"] = {"include_usage": True}
        for key, value in (req.extra or {}).items():
            body[key] = value
        return body

    def parse_stream_chunk(self, raw_event: Any) -> Optional[ChatChunk]:
        """Default OpenAI delta extraction. Subclasses lift reasoning etc."""
        try:
            choices = raw_event.choices if hasattr(raw_event, "choices") else raw_event.get("choices") or []
            if not choices:
                usage = getattr(raw_event, "usage", None) or (
                    raw_event.get("usage") if isinstance(raw_event, dict) else None
                )
                if usage:
                    return ChatChunk(usage=dict(usage) if isinstance(usage, dict) else _to_dict(usage), raw=raw_event)
                return None
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            if delta is None and isinstance(choice, dict):
                delta = choice.get("delta") or {}

            content = ""
            reasoning = ""
            tool_call_delta: Optional[Dict[str, Any]] = None

            content = _getattr_or_key(delta, "content") or ""
            reasoning = _getattr_or_key(delta, "reasoning_content") or _getattr_or_key(delta, "reasoning") or ""

            raw_tool_calls = _getattr_or_key(delta, "tool_calls") or []
            if raw_tool_calls:
                first = raw_tool_calls[0]
                tool_call_delta = {
                    "index": int(_getattr_or_key(first, "index") or 0),
                    "id": _getattr_or_key(first, "id") or "",
                    "function": {
                        "name": _getattr_or_key(_getattr_or_key(first, "function"), "name") or "",
                        "arguments": _getattr_or_key(_getattr_or_key(first, "function"), "arguments") or "",
                    },
                }

            finish = _getattr_or_key(choice, "finish_reason")
            if not (content or reasoning or tool_call_delta or finish):
                return None
            return ChatChunk(
                content_delta=content,
                reasoning_delta=reasoning,
                tool_call_delta=tool_call_delta,
                finish_reason=finish,
                raw=raw_event,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("parse_stream_chunk swallow: %s", exc)
            return None

    async def send(self, body: Dict[str, Any]) -> AsyncIterator[ChatChunk]:  # pragma: no cover
        """Default send uses the openai async client when available."""
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise ProviderError(f"openai SDK not installed: {exc}", vendor=self.name) from exc

        client_kwargs: Dict[str, Any] = {}
        if self.api_key:
            client_kwargs["api_key"] = self.api_key
        if self.api_base:
            client_kwargs["base_url"] = self.api_base
        if self.extra_headers:
            client_kwargs["default_headers"] = dict(self.extra_headers)
        client = AsyncOpenAI(**client_kwargs)

        stream = await client.chat.completions.create(**body)
        async for event in stream:
            chunk = self.parse_stream_chunk(event)
            if chunk is not None:
                yield chunk


# ── internal helpers ────────────────────────────────────────────────────

def _getattr_or_key(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _to_dict(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {"value": str(obj)}
