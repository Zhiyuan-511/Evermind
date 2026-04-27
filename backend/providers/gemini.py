"""Google Gemini provider.

Endpoint: https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
         :streamGenerateContent for streaming

Major differences from OpenAI:
    - Roles: `user` and `model` (no `assistant`).
    - `system` goes into `systemInstruction` at top level.
    - Message shape: `contents=[{role, parts:[{text}]}]`.
    - Tools: `tools=[{functionDeclarations:[{name,description,parameters}]}]`.
    - Tool call: `parts:[{functionCall:{id,name,args}}]`.
    - Tool result: `parts:[{functionResponse:{id,name,response}}]`.
    - Stream emits full `GenerateContentResponse` frames (not deltas);
      the consumer must diff against the previous frame.

v6.0 uses google-genai SDK when available, otherwise falls back to
plain httpx against the REST endpoint. We don't implement the full
Gemini surface — just enough to run Evermind agents.
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

logger = logging.getLogger("evermind.providers.gemini")

_DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"


@register_provider
class GeminiProvider(BaseProvider):
    name = "gemini"
    display_name = "Google Gemini"
    default_endpoint = _DEFAULT_ENDPOINT
    supports_tool_use = True
    supports_thinking = True

    @classmethod
    def matches(cls, model_name: str) -> bool:
        mn = str(model_name or "").lower()
        return (
            mn.startswith("gemini")
            or mn.startswith("google/")
            or mn in {"text-bison", "chat-bison"}
        )

    def normalize_request(self, req: ChatRequest) -> Dict[str, Any]:
        contents: List[Dict[str, Any]] = []
        system_parts: List[str] = []
        for m in req.messages:
            role = m.get("role") or "user"
            text = m.get("content") or ""
            if role == "system":
                if isinstance(text, str) and text:
                    system_parts.append(text)
                continue
            gemini_role = "model" if role == "assistant" else "user"
            if isinstance(text, list):
                parts = text  # already Gemini-shaped
            else:
                parts = [{"text": str(text)}]
            contents.append({"role": gemini_role, "parts": parts})

        body: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {},
        }
        if system_parts or req.system_prompt:
            sys_text = req.system_prompt or "\n\n".join(system_parts)
            body["systemInstruction"] = {"parts": [{"text": sys_text}]}

        gc = body["generationConfig"]
        if req.temperature is not None:
            gc["temperature"] = req.temperature
        if req.top_p is not None:
            gc["topP"] = req.top_p
        if req.max_tokens is not None:
            gc["maxOutputTokens"] = req.max_tokens

        if req.tools:
            body["tools"] = [{"functionDeclarations": self._translate_tools(req.tools)}]
            if req.tool_choice is not None:
                body["toolConfig"] = {
                    "functionCallingConfig": self._translate_tool_choice(req.tool_choice),
                }
        for key, value in (req.extra or {}).items():
            body[key] = value
        return body

    def parse_stream_chunk(self, raw_event: Any) -> Optional[ChatChunk]:
        """Gemini sends full frames — we diff parts versus our running buffer.

        For v6.0 we emit one ChatChunk per frame that contains fresh parts.
        The 'diff' is naive: we treat each part's text as a delta. This is
        acceptable because Gemini streams incrementally within a single
        candidate; duplicated content is extremely rare.
        """
        try:
            candidates = _getattr_or_key(raw_event, "candidates") or []
            if not candidates:
                return None
            first = candidates[0]
            content = _getattr_or_key(first, "content") or {}
            parts = _getattr_or_key(content, "parts") or []

            content_parts: List[str] = []
            tool_call_delta: Optional[Dict[str, Any]] = None
            reasoning_parts: List[str] = []

            for part in parts:
                text = _getattr_or_key(part, "text")
                fn_call = _getattr_or_key(part, "functionCall") or _getattr_or_key(part, "function_call")
                if text:
                    content_parts.append(str(text))
                if fn_call:
                    import json as _json
                    args = _getattr_or_key(fn_call, "args") or {}
                    tool_call_delta = {
                        "index": 0,
                        "id": _getattr_or_key(fn_call, "id") or "",
                        "function": {
                            "name": _getattr_or_key(fn_call, "name") or "",
                            "arguments": _json.dumps(args) if isinstance(args, dict) else str(args),
                        },
                    }

            finish = _getattr_or_key(first, "finishReason") or _getattr_or_key(first, "finish_reason")
            if not (content_parts or tool_call_delta or reasoning_parts or finish):
                return None
            return ChatChunk(
                content_delta="".join(content_parts),
                reasoning_delta="".join(reasoning_parts),
                tool_call_delta=tool_call_delta,
                finish_reason=_map_finish_reason(finish) if finish else None,
                raw=raw_event,
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("Gemini parse_stream_chunk swallow: %s", exc)
            return None

    async def send(self, body: Dict[str, Any]):
        """Use google-genai SDK if installed, else httpx REST."""
        try:
            from google import genai  # type: ignore
        except ImportError:
            genai = None  # fall through to httpx

        if genai is not None:
            client = genai.Client(api_key=self.api_key)
            async for chunk in self._stream_via_sdk(client, body):
                yield chunk
            return

        async for chunk in self._stream_via_httpx(body):
            yield chunk

    async def _stream_via_sdk(self, client, body):  # pragma: no cover
        model = body.pop("model", None) or body.pop("modelId", None) or ""
        async for event in await client.aio.models.generate_content_stream(
            model=model, **body,
        ):
            chunk = self.parse_stream_chunk(event)
            if chunk is not None:
                yield chunk

    async def _stream_via_httpx(self, body):  # pragma: no cover
        import json as _json
        try:
            import httpx
        except ImportError as exc:
            raise ProviderError("httpx not installed", vendor=self.name) from exc
        model = body.pop("model", None) or body.pop("modelId", None) or ""
        url = f"{self.api_base}/models/{model}:streamGenerateContent?alt=sse"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
            **self.extra_headers,
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=body, headers=headers) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload in ("[DONE]", ""):
                        continue
                    try:
                        event = _json.loads(payload)
                    except _json.JSONDecodeError:
                        continue
                    chunk = self.parse_stream_chunk(event)
                    if chunk is not None:
                        yield chunk

    def on_error_retry(self, err: Exception, attempt: int) -> ProviderRetryHint:
        msg = str(err).lower()
        status = getattr(err, "status_code", None) or _extract_status(msg)
        if status in (400, 401, 403, 422):
            return ProviderRetryHint(False, 0.0, f"fatal {status}")
        if status == 404:
            return ProviderRetryHint(False, 0.0, "model-not-found")
        if status == 429 or "quota" in msg or "rate" in msg:
            return ProviderRetryHint(attempt <= 3, min(30.0, 5.0 * (2 ** (attempt - 1))), "rate-limit")
        if status in (500, 502, 503, 504):
            return ProviderRetryHint(attempt <= 2, 3.0 * attempt, f"server {status}")
        if any(tok in msg for tok in ("timeout", "connection", "reset")):
            return ProviderRetryHint(attempt <= 2, 2.0 * attempt, "transient")
        return ProviderRetryHint(False, 0.0, "unknown")

    # ── helpers ──────────────────────────────────────────────────────────
    def _translate_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for t in tools or []:
            fn = t.get("function") or t
            out.append({
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {"type": "object"},
            })
        return out

    def _translate_tool_choice(self, choice: Any) -> Dict[str, Any]:
        if isinstance(choice, str):
            mode = {"auto": "AUTO", "required": "ANY", "any": "ANY", "none": "NONE"}.get(choice.lower(), "AUTO")
            return {"mode": mode}
        if isinstance(choice, dict) and choice.get("type") == "function":
            return {"mode": "ANY", "allowedFunctionNames": [(choice.get("function") or {}).get("name")]}
        return {"mode": "AUTO"}


def _map_finish_reason(reason: Any) -> str:
    s = str(reason or "").upper()
    return {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
    }.get(s, s.lower())


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
