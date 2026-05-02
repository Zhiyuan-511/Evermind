"""
Evermind Backend — Proxy/Relay API Plugin (中转 API)
Allows connecting to any OpenAI-compatible endpoint.
References: LiteLLM Proxy, OneAPI, New API patterns.
"""

import asyncio
import logging
import os
import re
import time
from copy import deepcopy
from typing import Any, Dict, List, Optional
from types import SimpleNamespace

logger = logging.getLogger("evermind.proxy_relay")

# ─────────────────────────────────────────────
# Security — sanitize sensitive data from log messages
# ─────────────────────────────────────────────
_SENSITIVE_RE = re.compile(
    r"(?:sk|key|token|api[_-]?key|Bearer)[-_\s]?[a-zA-Z0-9._\-]{8,}",
    re.IGNORECASE,
)
MAX_TIMEOUT_SECS = 300  # Hard cap on endpoint timeout
RELAY_POOL_PREFIX = "relay_pool/"
_PROVIDER_API_KEY_ENV_MAP = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "KIMI_API_KEY",
    "qwen": "QWEN_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "minimax": "MINIMAX_API_KEY",
}

RELAY_PROVIDER_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "openai_compat": {
        "id": "openai_compat",
        "label": "OpenAI Compat",
        "provider": "openai",
        "api_style": "openai_compatible",
        "default_base_url": "https://api.openai.com/v1",
        "default_models": ["gpt-5.4", "gpt-4.1", "gpt-4o", "o3"],
        "default_model_map": {
            "gpt-5.4": "gpt-5.4",
            "gpt-4.1": "gpt-4.1",
            "gpt-4o": "gpt-4o",
            "o3": "o3",
        },
        "docs_url": "https://developers.openai.com/api/docs/models/gpt-5.4",
        "description": "Official OpenAI-compatible route for GPT-5.4 and related OpenAI models. You can override the base URL with a third-party relay that forwards OpenAI models.",
    },
    "litellm_proxy": {
        "id": "litellm_proxy",
        "label": "LiteLLM Proxy",
        "provider": "relay",
        "api_style": "litellm_proxy",
        "default_base_url": "",
        "default_models": [],
        "default_model_map": {},
        "docs_url": "https://docs.litellm.ai/",
        "description": "Unified gateway for multi-provider routing, retries, and failover.",
    },
    "gemini_openai": {
        "id": "gemini_openai",
        "label": "Gemini OpenAI Compat",
        "provider": "google",
        "api_style": "openai_compatible",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_models": ["gemini-3-flash-preview", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"],
        "default_model_map": {
            "gemini-3-flash-preview": "gemini-3-flash-preview",
            "gemini-2.5-pro": "gemini-2.5-pro",
            "gemini-2.5-flash": "gemini-2.5-flash",
            "gemini-2.0-flash": "gemini-2.0-flash",
        },
        "docs_url": "https://ai.google.dev/gemini-api/docs/openai",
        "description": "Official Gemini OpenAI-compatible endpoint.",
    },
    "glm_openai": {
        "id": "glm_openai",
        "label": "GLM OpenAI Compat",
        "provider": "zhipu",
        "api_style": "openai_compatible",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "default_models": ["glm-5", "glm-4.7", "glm-4.5"],
        "default_model_map": {
            "glm-5": "glm-5",
            "glm-4.7": "glm-4.7",
            "glm-4.5": "glm-4.5",
        },
        "docs_url": "https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
        "description": "Official GLM OpenAI-compatible endpoint from Zhipu.",
    },
    "minimax_openai": {
        "id": "minimax_openai",
        "label": "MiniMax OpenAI Compat",
        "provider": "minimax",
        "api_style": "openai_compatible",
        "default_base_url": "https://api.minimax.io/v1",
        "default_models": [
            "minimax-m2.7",
            "minimax-m2.7-highspeed",
            "minimax-m2.5",
            "minimax-m2.5-highspeed",
            "minimax-m2.1",
            "minimax-m2.1-highspeed",
            "minimax-m2",
        ],
        "default_model_map": {
            "minimax-m2.7": "MiniMax-M2.7",
            "minimax-m2.7-highspeed": "MiniMax-M2.7-highspeed",
            "minimax-m2.5": "MiniMax-M2.5",
            "minimax-m2.5-highspeed": "MiniMax-M2.5-highspeed",
            "minimax-m2.1": "MiniMax-M2.1",
            "minimax-m2.1-highspeed": "MiniMax-M2.1-highspeed",
            "minimax-m2": "MiniMax-M2",
        },
        "docs_url": "https://platform.minimax.io/docs/api-reference/text-openai-api",
        "description": "Official MiniMax OpenAI-compatible endpoint. Anthropic-compatible API is recommended by MiniMax for some flows.",
    },
    "claude_openai_compat": {
        "id": "claude_openai_compat",
        "label": "Claude OpenAI Compat",
        "provider": "anthropic",
        "api_style": "openai_compatible",
        "default_base_url": "https://api.anthropic.com/v1/",
        "default_models": ["claude-4-opus", "claude-4-sonnet"],
        "default_model_map": {
            "claude-4-opus": "claude-opus-4-6",
            "claude-4-sonnet": "claude-sonnet-4-6",
        },
        "docs_url": "https://docs.anthropic.com/en/api/openai-sdk",
        "description": "Anthropic OpenAI SDK compatibility layer. Useful for migration/testing; native Claude API remains the primary production path.",
    },
}


def _sanitize_log(msg: str) -> str:
    """Strip potential API keys / secrets from log messages."""
    return _SENSITIVE_RE.sub("[REDACTED]", msg) if msg else msg


def _provider_env_api_key(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    env_key = _PROVIDER_API_KEY_ENV_MAP.get(normalized, "")
    return str(os.getenv(env_key, "") or "").strip() if env_key else ""


def relay_pool_model_id(model_name: str) -> str:
    """Return the virtual model id used for same-model relay pools."""
    return f"{RELAY_POOL_PREFIX}{str(model_name or '').strip()}"


def _normalize_base_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def relay_template_catalog() -> List[Dict[str, Any]]:
    catalog: List[Dict[str, Any]] = []
    for template_id, template in RELAY_PROVIDER_TEMPLATES.items():
        item = deepcopy(template)
        item["id"] = template_id
        item["default_base_url"] = _normalize_base_url(item.get("default_base_url", ""))
        item["default_models"] = list(item.get("default_models") or [])
        item["default_model_map"] = dict(item.get("default_model_map") or {})
        catalog.append(item)
    catalog.sort(key=lambda item: (str(item.get("provider") or ""), str(item.get("label") or "")))
    return catalog


def resolve_relay_template(
    *,
    provider: str = "",
    api_style: str = "",
    base_url: str = "",
    template_id: str = "",
) -> Dict[str, Any]:
    requested_id = str(template_id or "").strip()
    if requested_id and requested_id in RELAY_PROVIDER_TEMPLATES:
        return deepcopy(RELAY_PROVIDER_TEMPLATES[requested_id])

    normalized_provider = str(provider or "").strip().lower()
    normalized_style = str(api_style or "").strip().lower()
    normalized_base = _normalize_base_url(base_url)
    best_score = -1
    best_match: Dict[str, Any] = {}

    for candidate_id, raw_template in RELAY_PROVIDER_TEMPLATES.items():
        template = deepcopy(raw_template)
        template["id"] = candidate_id
        score = 0
        template_provider = str(template.get("provider") or "").strip().lower()
        template_style = str(template.get("api_style") or "").strip().lower()
        template_base = _normalize_base_url(str(template.get("default_base_url") or ""))
        if normalized_provider and normalized_provider == template_provider:
            score += 3
        if normalized_style and normalized_style == template_style:
            score += 3
        if normalized_base and template_base and normalized_base == template_base:
            score += 5
        elif normalized_base and template_base and normalized_base.startswith(template_base):
            score += 4
        if score > best_score:
            best_score = score
            best_match = template

    return best_match if best_score > 0 else {}


def _apply_relay_template_defaults(
    *,
    provider: str,
    api_style: str,
    base_url: str,
    models: Optional[List[str]],
    model_map: Optional[Dict[str, Any]],
    template_id: str = "",
) -> Dict[str, Any]:
    template = resolve_relay_template(
        provider=provider,
        api_style=api_style,
        base_url=base_url,
        template_id=template_id,
    )
    normalized_provider = str(provider or template.get("provider") or "openai").strip().lower() or "openai"
    normalized_style = str(api_style or template.get("api_style") or "openai_compatible").strip().lower() or "openai_compatible"
    normalized_base = _normalize_base_url(base_url or template.get("default_base_url") or "")

    merged_model_map: Dict[str, str] = {}
    for raw_alias, raw_target in dict(template.get("default_model_map") or {}).items():
        alias = str(raw_alias or "").strip()
        target = str(raw_target or "").strip()
        if alias and target:
            merged_model_map[alias] = target
    for raw_alias, raw_target in dict(model_map or {}).items():
        alias = str(raw_alias or "").strip()
        target = str(raw_target or "").strip()
        if alias and target:
            merged_model_map[alias] = target

    normalized_models: List[str] = []
    seen: set[str] = set()
    for raw_name in list(models or []) + list(template.get("default_models") or []):
        model_name = str(raw_name or "").strip()
        if not model_name or model_name in seen:
            continue
        seen.add(model_name)
        normalized_models.append(model_name)
    for alias in merged_model_map.keys():
        if alias in seen:
            continue
        seen.add(alias)
        normalized_models.append(alias)

    return {
        "provider": normalized_provider,
        "api_style": normalized_style,
        "base_url": normalized_base,
        "models": normalized_models,
        "model_map": merged_model_map,
        "template_id": str(template.get("id") or "").strip(),
        "template_label": str(template.get("label") or "").strip(),
        "template_docs_url": str(template.get("docs_url") or "").strip(),
    }


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _safe_cost(response, model: str) -> float:
    """Extract cost from a LiteLLM response, returning 0.0 on any failure."""
    try:
        import litellm
        return float(litellm.completion_cost(completion_response=response, model=model))
    except Exception:
        return 0.0


class RelayEndpoint:
    """A configured proxy/relay API endpoint."""
    VALID_API_STYLES = {"openai_compatible", "litellm_proxy"}

    def __init__(
        self,
        id: str,
        name: str,
        base_url: str,
        api_key: str = "",
        models: Optional[List[str]] = None,
        enabled: bool = True,
        headers: Optional[Dict[str, str]] = None,
        max_retries: int = 2,
        timeout: int = 120,
        provider: str = "openai",
        api_style: str = "openai_compatible",
        model_map: Optional[Dict[str, str]] = None,
        template_id: str = "",
    ):
        template_defaults = _apply_relay_template_defaults(
            provider=provider,
            api_style=api_style,
            base_url=base_url,
            models=models,
            model_map=model_map,
            template_id=template_id,
        )
        self.id = id
        self.name = name
        self.base_url = str(template_defaults.get("base_url") or "").strip().rstrip("/")
        self.api_key = api_key
        self.provider = str(template_defaults.get("provider") or provider or "openai").strip().lower() or "openai"
        requested_api_style = str(template_defaults.get("api_style") or api_style or "openai_compatible").strip().lower()
        self.api_style = requested_api_style if requested_api_style in self.VALID_API_STYLES else "openai_compatible"
        self.model_map = self._normalize_model_map(template_defaults.get("model_map") or {})
        self.models = self._normalize_models(template_defaults.get("models") or [])
        self.template_id = str(template_defaults.get("template_id") or "").strip()
        self.template_label = str(template_defaults.get("template_label") or "").strip()
        self.template_docs_url = str(template_defaults.get("template_docs_url") or "").strip()
        self.enabled = enabled
        self.headers = headers or {}
        try:
            self.max_retries = max(0, int(max_retries))
        except Exception:
            self.max_retries = 2
        try:
            timeout_value = int(timeout)
        except Exception:
            timeout_value = 120
        self.timeout = min(max(timeout_value, 1), MAX_TIMEOUT_SECS)  # hard cap
        self.last_test: Optional[Dict] = None  # last health check result
        self._last_used: float = 0.0  # timestamp of last API call
        self._last_latency_ms: Optional[float] = None
        self._latency_ewma_ms: Optional[float] = None
        # Circuit-breaker state
        self._consecutive_failures: int = 0
        self._circuit_open_until: float = 0.0
        # TLS safety warning
        if not self.base_url.startswith("https://"):
            logger.warning(
                f"Relay '{name}' uses non-HTTPS URL ({self.base_url}). "
                f"API keys may be transmitted in plaintext. Consider using HTTPS."
            )

    # ── Circuit-breaker with exponential backoff ──
    CIRCUIT_FAILURE_THRESHOLD = 3
    CIRCUIT_BASE_RECOVERY_SECS = 30.0
    CIRCUIT_MAX_RECOVERY_SECS = 300.0  # Cap at 5 minutes

    @property
    def circuit_open(self) -> bool:
        if self._consecutive_failures < self.CIRCUIT_FAILURE_THRESHOLD:
            return False
        return time.time() < self._circuit_open_until

    def _record_success(self):
        self._consecutive_failures = 0

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.CIRCUIT_FAILURE_THRESHOLD:
            # v3.1: Exponential backoff — recovery time doubles with each
            # failure beyond the threshold, capped at CIRCUIT_MAX_RECOVERY_SECS.
            # Previous fixed 60s was too aggressive during sustained outages and
            # too slow to recover from brief network glitches.
            backoff_exponent = self._consecutive_failures - self.CIRCUIT_FAILURE_THRESHOLD
            recovery_secs = min(
                self.CIRCUIT_BASE_RECOVERY_SECS * (2 ** backoff_exponent),
                self.CIRCUIT_MAX_RECOVERY_SECS,
            )
            self._circuit_open_until = time.time() + recovery_secs
            logger.warning(
                f"Circuit OPEN for relay '{self.name}' after {self._consecutive_failures} consecutive failures "
                f"(recovery in {recovery_secs:.0f}s, backoff level {backoff_exponent})"
            )

    def _touch(self):
        """Update last-used timestamp."""
        self._last_used = time.time()

    def _record_latency(self, latency_ms: Optional[float]):
        """Track recent relay latency for routing decisions."""
        try:
            latency = float(latency_ms or 0)
        except Exception:
            return
        if latency <= 0:
            return
        self._last_latency_ms = latency
        if self._latency_ewma_ms is None:
            self._latency_ewma_ms = latency
        else:
            self._latency_ewma_ms = round((latency * 0.35) + (self._latency_ewma_ms * 0.65), 2)

    def _serialize(self, mask_secret: bool) -> Dict:
        result = {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": (self.api_key[:8] + "...") if mask_secret and len(self.api_key) > 8 else ("***" if mask_secret else self.api_key),
            "models": self.models,
            "provider": self.provider,
            "api_style": self.api_style,
            "model_map": self.model_map,
            "template_id": self.template_id,
            "template_label": self.template_label,
            "template_docs_url": self.template_docs_url,
            "enabled": self.enabled,
            "headers": self.headers,
            "max_retries": self.max_retries,
            "timeout": self.timeout,
            "last_test": self.last_test,
        }
        # Add TLS warning flag for frontend display
        if not self.base_url.startswith("https://"):
            result["tls_warning"] = True
        return result

    def to_dict(self) -> Dict:
        return self._serialize(mask_secret=True)

    def to_config(self) -> Dict:
        """Full-fidelity settings payload used for persistence."""
        return self._serialize(mask_secret=False)

    @staticmethod
    def _normalize_model_map(value: Dict[str, Any]) -> Dict[str, str]:
        normalized: Dict[str, str] = {}
        if not isinstance(value, dict):
            return normalized
        for raw_alias, raw_target in value.items():
            alias = str(raw_alias or "").strip()
            target = str(raw_target or "").strip()
            if alias and target:
                normalized[alias] = target
        return normalized

    def _normalize_models(self, models: List[str]) -> List[str]:
        normalized: List[str] = []
        seen: set[str] = set()
        items = list(models or [])
        items.extend(self.model_map.keys())
        for raw_name in items:
            model_name = str(raw_name or "").strip()
            if not model_name or model_name in seen:
                continue
            seen.add(model_name)
            normalized.append(model_name)
        return normalized

    def supports_model(self, model_name: str) -> bool:
        target = str(model_name or "").strip()
        return bool(target and target in self.models)

    def resolve_request_model(self, model_name: str) -> Dict[str, str]:
        alias = str(model_name or "").strip()
        mapped = str(self.model_map.get(alias) or alias).strip()
        if not mapped:
            return {"model_name": alias, "mapped_model": "", "request_model": ""}
        if self.api_style == "litellm_proxy":
            request_model = mapped
        else:
            request_model = mapped if mapped.lower().startswith("openai/") else f"openai/{mapped}"
        return {
            "model_name": alias,
            "mapped_model": mapped,
            "request_model": request_model,
        }

    def to_model_registry_entries(self) -> Dict[str, Dict]:
        """Generate MODEL_REGISTRY-compatible entries for this relay's models.
        SECURITY: api_key is intentionally excluded — it is resolved at call time
        from the endpoint object to prevent accidental exposure in model listings.
        """
        entries = {}
        for model_name in self.models:
            resolved = self.resolve_request_model(model_name)
            relay_id = f"relay/{self.id}/{model_name}"
            entries[relay_id] = {
                "provider": "relay",
                "litellm_id": resolved.get("request_model") or model_name,
                "supports_tools": True,
                "supports_cua": False,
                "api_base": self.base_url,
                # api_key intentionally omitted for security
                "relay_id": self.id,
                "relay_name": self.name,
                "relay_model_name": model_name,
                "relay_target_model": resolved.get("mapped_model") or model_name,
                "relay_provider": self.provider,
                "relay_api_style": self.api_style,
            }
        return entries


class RelayManager:
    """
    Manages proxy/relay API endpoints.
    Supports adding, removing, testing, and routing through relay services.
    """

    def __init__(self):
        self._endpoints: Dict[str, RelayEndpoint] = {}
        self._counter = 0
        logger.info("RelayManager initialized")

    @staticmethod
    def _choice_message_content(response: Any) -> str:
        try:
            choices = getattr(response, "choices", None)
            if choices is None and isinstance(response, dict):
                choices = response.get("choices")
            if not choices:
                return ""
            first = choices[0]
            message = getattr(first, "message", None)
            if message is None and isinstance(first, dict):
                message = first.get("message")
            if message is None:
                return ""
            content = getattr(message, "content", None)
            if content is None and isinstance(message, dict):
                content = message.get("content")
            return str(content or "").strip()
        except Exception:
            return ""

    @staticmethod
    async def _stream_collect(litellm_module: Any, call_kwargs: Dict[str, Any]) -> Any:
        """Call litellm.acompletion with stream=True, collect chunks into a
        complete response that matches the non-streaming response shape.

        Falls back to non-streaming acompletion if streaming fails to start.
        This is the core optimization: streaming lets the relay send tokens
        as they arrive, avoiding the "wait for all tokens" bottleneck.
        """
        stream_kwargs = {**call_kwargs, "stream": True}
        content_parts: List[str] = []
        model_name: Optional[str] = None
        finish_reason: Optional[str] = None
        usage_data: Optional[Dict[str, Any]] = None

        try:
            stream_resp = await litellm_module.acompletion(**stream_kwargs)
            async for chunk in stream_resp:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                if delta:
                    part = getattr(delta, "content", None)
                    if part:
                        content_parts.append(part)
                fr = getattr(choices[0], "finish_reason", None)
                if fr:
                    finish_reason = fr
                if not model_name:
                    model_name = getattr(chunk, "model", None)
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage_data = {
                        "prompt_tokens": getattr(chunk_usage, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(chunk_usage, "completion_tokens", 0) or 0,
                        "total_tokens": getattr(chunk_usage, "total_tokens", 0) or 0,
                    }
        except Exception:
            # Only fall back to non-streaming if NO chunks were received.
            # If partial content arrived, use what we have to avoid double billing.
            if not content_parts:
                resp = await litellm_module.acompletion(**call_kwargs)
                return resp

        full_content = "".join(content_parts)
        if not usage_data:
            usage_data = {
                "prompt_tokens": 0,
                "completion_tokens": len(full_content) // 4,
                "total_tokens": len(full_content) // 4,
            }

        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=full_content, tool_calls=None),
                finish_reason=finish_reason or "stop",
            )],
            model=model_name or call_kwargs.get("model", ""),
            usage=usage_data,
        )

    @staticmethod
    def _response_has_named_tool_call(response: Any, expected_name: str) -> bool:
        try:
            choices = getattr(response, "choices", None)
            if choices is None and isinstance(response, dict):
                choices = response.get("choices")
            if not choices:
                return False
            first = choices[0]
            message = getattr(first, "message", None)
            if message is None and isinstance(first, dict):
                message = first.get("message")
            if message is None:
                return False
            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls is None and isinstance(message, dict):
                tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                return False
            for tool_call in tool_calls:
                function = getattr(tool_call, "function", None)
                if function is None and isinstance(tool_call, dict):
                    function = tool_call.get("function")
                if function is None:
                    continue
                name = getattr(function, "name", None)
                if name is None and isinstance(function, dict):
                    name = function.get("name")
                if str(name or "").strip() == expected_name:
                    return True
        except Exception:
            return False
        return False

    @staticmethod
    def _stream_probe_has_content(stream: Any) -> bool:
        try:
            for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if choices is None and isinstance(chunk, dict):
                    choices = chunk.get("choices")
                if not choices:
                    continue
                first = choices[0]
                delta = getattr(first, "delta", None)
                if delta is None and isinstance(first, dict):
                    delta = first.get("delta")
                if delta is None:
                    continue
                content = getattr(delta, "content", None)
                if content is None and isinstance(delta, dict):
                    content = delta.get("content")
                if str(content or "").strip():
                    return True
        except Exception:
            return False
        return False

    def add(
        self,
        name: str,
        base_url: str,
        api_key: str = "",
        models: Optional[List[str]] = None,
        headers: Optional[Dict[str, str]] = None,
        provider: str = "openai",
        api_style: str = "openai_compatible",
        model_map: Optional[Dict[str, str]] = None,
        template_id: str = "",
        max_retries: int = 2,
        timeout: int = 120,
    ) -> RelayEndpoint:
        """Add a new relay endpoint."""
        self._counter += 1
        endpoint_id = f"relay_{self._counter}_{int(time.time())}"

        template_defaults = _apply_relay_template_defaults(
            provider=provider,
            api_style=api_style,
            base_url=base_url,
            models=models,
            model_map=model_map,
            template_id=template_id,
        )
        base_url = str(template_defaults.get("base_url") or base_url)
        provider = str(template_defaults.get("provider") or provider)
        api_style = str(template_defaults.get("api_style") or api_style)
        template_id = str(template_defaults.get("template_id") or template_id)
        # Auto-detect models if not specified
        if not template_defaults.get("models") and not template_defaults.get("model_map"):
            models = ["gpt-4o", "gpt-3.5-turbo"]
        else:
            models = list(template_defaults.get("models") or [])
            model_map = dict(template_defaults.get("model_map") or {})

        endpoint = RelayEndpoint(
            id=endpoint_id,
            name=name,
            base_url=base_url,
            api_key=api_key,
            models=models,
            headers=headers or {},
            provider=provider,
            api_style=api_style,
            model_map=model_map or {},
            template_id=template_id,
            max_retries=max_retries,
            timeout=timeout,
        )
        self._endpoints[endpoint_id] = endpoint
        logger.info(f"Added relay endpoint: {name} ({base_url}) with {len(endpoint.models)} models")
        return endpoint

    def remove(self, endpoint_id: str) -> bool:
        """Remove a relay endpoint."""
        if endpoint_id in self._endpoints:
            name = self._endpoints[endpoint_id].name
            del self._endpoints[endpoint_id]
            logger.info(f"Removed relay endpoint: {name}")
            return True
        return False

    def get(self, endpoint_id: str) -> Optional[RelayEndpoint]:
        return self._endpoints.get(endpoint_id)

    def load(self, endpoints: List[Dict]):
        """Hydrate relay endpoints from saved settings."""
        self._endpoints = {}
        self._counter = 0
        for item in endpoints or []:
            endpoint_id = item.get("id") or f"relay_{self._counter + 1}_{int(time.time())}"
            raw_models = item.get("models", []) or []
            if not raw_models and not (item.get("model_map") or {}):
                raw_models = ["gpt-4o"]
            endpoint = RelayEndpoint(
                id=endpoint_id,
                name=item.get("name", "Unnamed Relay"),
                base_url=item.get("base_url", ""),
                api_key=item.get("api_key", ""),
                models=raw_models,
                enabled=item.get("enabled", True),
                headers=item.get("headers", {}) or {},
                max_retries=item.get("max_retries", 2),
                timeout=item.get("timeout", 120),
                provider=item.get("provider", "openai"),
                api_style=item.get("api_style", "openai_compatible"),
                model_map=item.get("model_map", {}) or {},
                template_id=item.get("template_id", ""),
            )
            endpoint.last_test = item.get("last_test")
            if isinstance(endpoint.last_test, dict) and (
                endpoint.last_test.get("success") or endpoint.last_test.get("connectivity_ok")
            ):
                endpoint._record_latency(
                    endpoint.last_test.get("latency_ms")
                    or endpoint.last_test.get("streaming_latency_ms")
                    or endpoint.last_test.get("tool_latency_ms")
                )
            self._endpoints[endpoint_id] = endpoint
            self._counter += 1
        logger.info(f"Loaded {len(self._endpoints)} relay endpoint(s) from settings")

    def export(self) -> List[Dict]:
        """Export relay endpoints for settings persistence."""
        return [ep.to_config() for ep in self._endpoints.values()]

    def list(self) -> List[Dict]:
        """List all configured relay endpoints."""
        return [ep.to_dict() for ep in self._endpoints.values()]

    def get_all_models(self) -> Dict[str, Dict]:
        """Get combined MODEL_REGISTRY entries from all enabled relays."""
        all_models = {}
        all_models.update(self._pool_model_registry_entries())
        for ep in self._endpoints.values():
            if ep.enabled:
                all_models.update(ep.to_model_registry_entries())
        return all_models

    def matching_endpoints_for_model(self, model_name: str) -> List[RelayEndpoint]:
        """Return enabled relay endpoints that can serve the given model."""
        target = str(model_name or "").strip()
        if not target:
            return []
        matches: List[RelayEndpoint] = []
        for endpoint in self._endpoints.values():
            if not endpoint.enabled:
                continue
            if endpoint.supports_model(target):
                matches.append(endpoint)
        return matches

    def ranked_endpoints_for_model(self, model_name: str) -> List[RelayEndpoint]:
        """Rank relay endpoints by health and observed latency for a model."""
        ranked = []
        for endpoint in self.matching_endpoints_for_model(model_name):
            last_test = endpoint.last_test if isinstance(endpoint.last_test, dict) else {}
            last_test_success = bool(last_test.get("success")) if last_test else False
            builder_profile_ok = bool(last_test.get("builder_profile_ok")) if last_test else False
            latency = endpoint._latency_ewma_ms
            if latency is None:
                latency = endpoint._last_latency_ms
            if latency is None and last_test:
                try:
                    latency = float(last_test.get("latency_ms") or 0)
                except Exception:
                    latency = 0.0
            ranked.append((
                (
                    1 if endpoint.circuit_open else 0,
                    0 if builder_profile_ok else 1,
                    0 if (last_test_success or endpoint._last_latency_ms is not None) else 1,
                    int(endpoint._consecutive_failures or 0),
                    float(latency or 999999.0),
                    float(endpoint._last_used or 0.0),
                    endpoint.name.lower(),
                ),
                endpoint,
            ))
        ranked.sort(key=lambda item: item[0])
        return [endpoint for _sort_key, endpoint in ranked]

    def relay_model_candidates_for(self, model_name: str) -> List[str]:
        """Return automatic candidate ids for a model across configured relays."""
        ranked = self.ranked_endpoints_for_model(model_name)
        if len(ranked) >= 2:
            return [relay_pool_model_id(model_name)]
        if len(ranked) == 1:
            normalized = str(model_name or "").strip()
            return [f"relay/{ranked[0].id}/{normalized}"]
        return []

    def _pool_model_registry_entries(self) -> Dict[str, Dict]:
        """Expose a virtual pool model when multiple relays serve the same model."""
        grouped: Dict[str, List[RelayEndpoint]] = {}
        for endpoint in self._endpoints.values():
            if not endpoint.enabled:
                continue
            for model_name in endpoint.models:
                normalized = str(model_name or "").strip()
                if not normalized:
                    continue
                grouped.setdefault(normalized, []).append(endpoint)

        entries: Dict[str, Dict] = {}
        for model_name, endpoints in grouped.items():
            if len(endpoints) < 2:
                continue
            ranked_endpoints = self.ranked_endpoints_for_model(model_name)
            preferred_endpoint = ranked_endpoints[0] if ranked_endpoints else endpoints[0]
            resolved = preferred_endpoint.resolve_request_model(model_name)
            entries[relay_pool_model_id(model_name)] = {
                "provider": "relay",
                "litellm_id": resolved.get("request_model") or model_name,
                "supports_tools": True,
                "supports_cua": False,
                "relay_strategy": "pool",
                "relay_pool_model": model_name,
                "relay_pool_size": len(endpoints),
                "relay_pool_endpoints": [endpoint.id for endpoint in ranked_endpoints],
                "relay_name": f"Relay Pool ({model_name})",
                "relay_model_name": model_name,
                "relay_target_model": resolved.get("mapped_model") or model_name,
                "relay_provider": preferred_endpoint.provider,
                "relay_api_style": preferred_endpoint.api_style,
            }
        return entries

    @staticmethod
    def _should_failover_pool_error(error: str) -> bool:
        """Return whether the relay pool should try the next endpoint."""
        message = str(error or "").lower()
        if not message:
            return True
        non_failover_markers = (
            "context length",
            "maximum context",
            "prompt too long",
            "invalid_request_error",
            "content policy",
            "safety system",
            "messages must",
            "tool schema",
            "invalid image",
            "unsupported input",
        )
        return not any(marker in message for marker in non_failover_markers)

    async def test(self, endpoint_id: str) -> Dict:
        """Test connectivity to a relay endpoint."""
        endpoint = self._endpoints.get(endpoint_id)
        if not endpoint:
            return {"success": False, "error": "Endpoint not found"}

        try:
            import litellm

            test_model = endpoint.models[0] if endpoint.models else "gpt-3.5-turbo"
            resolved = endpoint.resolve_request_model(test_model)
            litellm_model = resolved.get("request_model") or f"openai/{test_model}"
            effective_api_key = str(endpoint.api_key or _provider_env_api_key(endpoint.provider) or "").strip()
            test_timeout = min(max(8, int(endpoint.timeout or 10)), 20)
            base_kwargs = {
                "model": litellm_model,
                "api_base": endpoint.base_url,
                "api_key": effective_api_key,
                "timeout": test_timeout,
            }
            connectivity_ok = False
            streaming_ok = False
            tool_calling_ok = False
            basic_latency = 0
            streaming_latency = 0
            tool_latency = 0
            model_seen = "unknown"
            errors: List[str] = []

            start = time.time()
            response = await asyncio.to_thread(
                litellm.completion,
                **base_kwargs,
                messages=[{"role": "user", "content": "Reply with exactly OK"}],
                max_tokens=8,
            )
            basic_latency = round((time.time() - start) * 1000)
            model_seen = getattr(response, "model", None) or model_seen
            connectivity_ok = bool(self._choice_message_content(response))
            if not connectivity_ok:
                errors.append("basic completion returned empty content")

            if connectivity_ok:
                def _stream_probe() -> bool:
                    stream = litellm.completion(
                        **base_kwargs,
                        messages=[{"role": "user", "content": "Reply with exactly OK"}],
                        max_tokens=8,
                        stream=True,
                    )
                    return self._stream_probe_has_content(stream)

                try:
                    start = time.time()
                    streaming_ok = await asyncio.to_thread(_stream_probe)
                    streaming_latency = round((time.time() - start) * 1000)
                    if not streaming_ok:
                        errors.append("streaming probe returned no content chunks")
                except Exception as stream_err:
                    errors.append(f"streaming probe failed: {_sanitize_log(str(stream_err))}")

                tool_schema = [{
                    "type": "function",
                    "function": {
                        "name": "ping",
                        "description": "Return a ping payload.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "message": {"type": "string"},
                            },
                            "required": ["message"],
                            "additionalProperties": False,
                        },
                    },
                }]
                try:
                    start = time.time()
                    tool_response = await asyncio.to_thread(
                        litellm.completion,
                        **base_kwargs,
                        messages=[{
                            "role": "user",
                            "content": "Call the ping tool with message set to pong. Do not answer with prose.",
                        }],
                        tools=tool_schema,
                        tool_choice={"type": "function", "function": {"name": "ping"}},
                        max_tokens=32,
                    )
                    tool_latency = round((time.time() - start) * 1000)
                    tool_calling_ok = self._response_has_named_tool_call(tool_response, "ping")
                    if not tool_calling_ok:
                        errors.append("tool-calling probe did not return the forced ping tool call")
                except Exception as tool_err:
                    errors.append(f"tool-calling probe failed: {_sanitize_log(str(tool_err))}")

            builder_profile_ok = bool(connectivity_ok and streaming_ok and tool_calling_ok)
            result = {
                "success": builder_profile_ok,
                "connectivity_ok": connectivity_ok,
                "streaming_ok": streaming_ok,
                "tool_calling_ok": tool_calling_ok,
                "builder_profile_ok": builder_profile_ok,
                "latency_ms": basic_latency,
                "streaming_latency_ms": streaming_latency,
                "tool_latency_ms": tool_latency,
                "model": model_seen,
                "requested_model": test_model,
                "resolved_model": litellm_model,
                "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if errors:
                result["error"] = "; ".join(errors[:4])
            endpoint.last_test = result
            if builder_profile_ok:
                endpoint._record_success()
                endpoint._record_latency(basic_latency or streaming_latency or tool_latency)
                logger.info(
                    "Relay builder-profile test passed: %s (basic=%sms stream=%sms tool=%sms)",
                    endpoint.name,
                    basic_latency,
                    streaming_latency,
                    tool_latency,
                )
            else:
                endpoint._record_failure()
                logger.warning(
                    "Relay builder-profile test failed: %s connectivity=%s streaming=%s tools=%s error=%s",
                    endpoint.name,
                    connectivity_ok,
                    streaming_ok,
                    tool_calling_ok,
                    _sanitize_log(str(result.get("error") or "")),
                )
            return result

        except Exception as e:
            result = {
                "success": False,
                "error": str(e),
                "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            endpoint.last_test = result
            endpoint._record_failure()
            logger.warning(f"Relay test failed: {endpoint.name}: {_sanitize_log(str(e))}")
            return result

    async def call(
        self,
        endpoint_id: str,
        model: str,
        messages: List[Dict],
        **kwargs,
    ) -> Dict:
        """Make an API call through a relay endpoint."""
        pool_mode = bool(kwargs.pop("_pool_mode", False))
        endpoint = self._endpoints.get(endpoint_id)
        if not endpoint:
            return {"success": False, "error": "Relay endpoint not found"}
        if not endpoint.enabled:
            return {"success": False, "error": "Relay endpoint is disabled"}
        if endpoint.circuit_open:
            return {"success": False, "error": f"Circuit breaker open for '{endpoint.name}' — retrying in {int(endpoint._circuit_open_until - time.time())}s"}

        try:
            import litellm
        except Exception as import_err:
            endpoint._record_failure()
            return {"success": False, "error": f"LiteLLM unavailable: {import_err}", "relay": endpoint.name}

        resolved = endpoint.resolve_request_model(model)
        litellm_model = resolved.get("request_model") or model
        if not litellm_model:
            endpoint._record_failure()
            return {"success": False, "error": "Relay request model resolved to empty value", "relay": endpoint.name}
        effective_api_key = str(endpoint.api_key or _provider_env_api_key(endpoint.provider) or "").strip()
        call_kwargs = {
            "model": litellm_model,
            "api_base": endpoint.base_url,
            "api_key": effective_api_key,
            "messages": messages,
            "timeout": endpoint.timeout,
            **kwargs,
        }

        # Add custom headers
        if endpoint.headers:
            call_kwargs["extra_headers"] = endpoint.headers

        try:
            # Hard timeout: asyncio.wait_for ensures the call cannot hang beyond endpoint.timeout + buffer
            hard_timeout = min(endpoint.timeout, MAX_TIMEOUT_SECS - 15) + 15
            started_at = time.perf_counter()
            # v4.1: Stream-collect — async streaming avoids "wait for all tokens" relay bottleneck
            response = await asyncio.wait_for(
                self._stream_collect(litellm, call_kwargs),
                timeout=hard_timeout,
            )
            latency_ms = round((time.perf_counter() - started_at) * 1000)
            cost = _safe_cost(response, litellm_model)
            endpoint._record_success()
            endpoint._touch()
            endpoint._record_latency(latency_ms)
            return {
                "success": True,
                "content": response.choices[0].message.content or "",
                "model": response.model if hasattr(response, "model") else (resolved.get("mapped_model") or model),
                "usage": dict(response.usage) if hasattr(response, "usage") and response.usage else {},
                "relay": endpoint.name,
                "latency_ms": latency_ms,
                "cost": cost,
                "requested_model": model,
                "resolved_model": litellm_model,
            }

        except asyncio.TimeoutError:
            last_error = f"Hard timeout ({endpoint.timeout + 15}s) exceeded for relay '{endpoint.name}'"
            logger.error(last_error)
            endpoint._record_failure()
            return {"success": False, "error": last_error, "relay": endpoint.name}

        except Exception as e:
            last_error = _sanitize_log(str(e))
            logger.error(f"Relay call failed ({endpoint.name}): {last_error}")
            # Retry logic with exponential backoff
            for retry in range(0 if pool_mode else endpoint.max_retries):
                try:
                    await asyncio.sleep(min(2 ** retry, 8))
                    retry_timeout = min(endpoint.timeout, MAX_TIMEOUT_SECS - 15) + 15
                    retry_started_at = time.perf_counter()
                    # v4.1: Stream-collect on retry path too
                    response = await asyncio.wait_for(
                        self._stream_collect(litellm, call_kwargs),
                        timeout=retry_timeout,
                    )
                    latency_ms = round((time.perf_counter() - retry_started_at) * 1000)
                    cost = _safe_cost(response, litellm_model)
                    endpoint._record_success()
                    endpoint._touch()
                    endpoint._record_latency(latency_ms)
                    return {
                        "success": True,
                        "content": response.choices[0].message.content or "",
                        "model": response.model if hasattr(response, "model") else (resolved.get("mapped_model") or model),
                        "relay": endpoint.name,
                        "retried": retry + 1,
                        "latency_ms": latency_ms,
                        "usage": dict(response.usage) if hasattr(response, "usage") and response.usage else {},
                        "cost": cost,
                        "requested_model": model,
                        "resolved_model": litellm_model,
                    }
                except asyncio.TimeoutError:
                    last_error = f"Retry {retry + 1} hard timeout for relay '{endpoint.name}'"
                    logger.warning(last_error)
                    continue
                except Exception as retry_err:
                    last_error = _sanitize_log(str(retry_err))
                    continue

            # v3.1: In pool mode, individual endpoint failures should not trigger
            # the circuit breaker — the pool's call_best handles failover instead.
            if not pool_mode:
                endpoint._record_failure()
            return {"success": False, "error": last_error, "relay": endpoint.name}

    async def call_best(
        self,
        model: str,
        messages: List[Dict],
        **kwargs,
    ) -> Dict:
        """Route a same-model relay request through the healthiest available endpoint."""
        ranked = self.ranked_endpoints_for_model(model)
        if not ranked:
            return {"success": False, "error": f"No enabled relay endpoint supports model '{model}'"}

        available = [endpoint for endpoint in ranked if not endpoint.circuit_open]
        if not available:
            return {
                "success": False,
                "error": f"All relay endpoints for '{model}' are currently circuit-open",
                "relay_pool_model": model,
                "relay_pool_candidates": [endpoint.name for endpoint in ranked],
            }

        tried: List[str] = []
        errors: List[Dict[str, str]] = []
        last_error = ""
        for endpoint in available:
            tried.append(endpoint.name)
            result = await self.call(
                endpoint_id=endpoint.id,
                model=model,
                messages=messages,
                _pool_mode=True,
                **kwargs,
            )
            if result.get("success"):
                result["relay_pool_model"] = model
                result["relay_pool_tried"] = tried[:]
                result["relay_pool_attempts"] = len(tried)
                result["relay_pool_failover"] = len(tried) > 1
                return result

            last_error = str(result.get("error") or "").strip()
            errors.append({
                "relay": endpoint.name,
                "error": last_error or "Relay call failed",
            })
            if not self._should_failover_pool_error(last_error):
                break

        return {
            "success": False,
            "error": last_error or f"Relay pool failed for model '{model}'",
            "relay_pool_model": model,
            "relay_pool_tried": tried,
            "relay_pool_attempts": len(tried),
            "relay_errors": errors,
        }


# ─────────────────────────────────────────────
# Global instance
# ─────────────────────────────────────────────
_global_relay_manager: Optional[RelayManager] = None


def get_relay_manager() -> RelayManager:
    global _global_relay_manager
    if _global_relay_manager is None:
        _global_relay_manager = RelayManager()
    return _global_relay_manager
