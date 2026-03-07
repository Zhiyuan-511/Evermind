"""
Evermind Backend — Proxy/Relay API Plugin (中转 API)
Allows connecting to any OpenAI-compatible endpoint.
References: LiteLLM Proxy, OneAPI, New API patterns.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("evermind.proxy_relay")


class RelayEndpoint:
    """A configured proxy/relay API endpoint."""

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
    ):
        self.id = id
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.models = models or []
        self.enabled = enabled
        self.headers = headers or {}
        self.max_retries = max_retries
        self.timeout = timeout
        self.last_test: Optional[Dict] = None  # last health check result

    def _serialize(self, mask_secret: bool) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": (self.api_key[:8] + "...") if mask_secret and len(self.api_key) > 8 else ("***" if mask_secret else self.api_key),
            "models": self.models,
            "enabled": self.enabled,
            "headers": self.headers,
            "max_retries": self.max_retries,
            "timeout": self.timeout,
            "last_test": self.last_test,
        }

    def to_dict(self) -> Dict:
        return self._serialize(mask_secret=True)

    def to_config(self) -> Dict:
        """Full-fidelity settings payload used for persistence."""
        return self._serialize(mask_secret=False)

    def to_model_registry_entries(self) -> Dict[str, Dict]:
        """Generate MODEL_REGISTRY-compatible entries for this relay's models."""
        entries = {}
        for model_name in self.models:
            relay_id = f"relay/{self.id}/{model_name}"
            entries[relay_id] = {
                "provider": "relay",
                "litellm_id": f"openai/{model_name}",
                "supports_tools": True,
                "supports_cua": False,
                "api_base": self.base_url,
                "api_key": self.api_key,
                "relay_id": self.id,
                "relay_name": self.name,
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

    def add(
        self,
        name: str,
        base_url: str,
        api_key: str = "",
        models: Optional[List[str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> RelayEndpoint:
        """Add a new relay endpoint."""
        self._counter += 1
        endpoint_id = f"relay_{self._counter}_{int(time.time())}"

        # Auto-detect models if not specified
        if not models:
            models = ["gpt-4o", "gpt-3.5-turbo"]

        endpoint = RelayEndpoint(
            id=endpoint_id,
            name=name,
            base_url=base_url,
            api_key=api_key,
            models=models,
            headers=headers or {},
        )
        self._endpoints[endpoint_id] = endpoint
        logger.info(f"Added relay endpoint: {name} ({base_url}) with {len(models)} models")
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
            endpoint = RelayEndpoint(
                id=endpoint_id,
                name=item.get("name", "Unnamed Relay"),
                base_url=item.get("base_url", ""),
                api_key=item.get("api_key", ""),
                models=item.get("models", []) or ["gpt-4o"],
                enabled=item.get("enabled", True),
                headers=item.get("headers", {}) or {},
                max_retries=item.get("max_retries", 2),
                timeout=item.get("timeout", 120),
            )
            endpoint.last_test = item.get("last_test")
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
        for ep in self._endpoints.values():
            if ep.enabled:
                all_models.update(ep.to_model_registry_entries())
        return all_models

    async def test(self, endpoint_id: str) -> Dict:
        """Test connectivity to a relay endpoint."""
        endpoint = self._endpoints.get(endpoint_id)
        if not endpoint:
            return {"success": False, "error": "Endpoint not found"}

        try:
            import litellm

            start = time.time()
            # Send a minimal request to test the connection
            response = await asyncio.to_thread(
                litellm.completion,
                model=f"openai/{endpoint.models[0]}" if endpoint.models else "openai/gpt-3.5-turbo",
                api_base=endpoint.base_url,
                api_key=endpoint.api_key,
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5,
                timeout=10,
            )
            latency = round((time.time() - start) * 1000)
            result = {
                "success": True,
                "latency_ms": latency,
                "model": response.model if hasattr(response, "model") else "unknown",
                "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            endpoint.last_test = result
            logger.info(f"Relay test passed: {endpoint.name} ({latency}ms)")
            return result

        except Exception as e:
            result = {
                "success": False,
                "error": str(e),
                "tested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            endpoint.last_test = result
            logger.warning(f"Relay test failed: {endpoint.name}: {e}")
            return result

    async def call(
        self,
        endpoint_id: str,
        model: str,
        messages: List[Dict],
        **kwargs,
    ) -> Dict:
        """Make an API call through a relay endpoint."""
        endpoint = self._endpoints.get(endpoint_id)
        if not endpoint:
            return {"success": False, "error": "Relay endpoint not found"}
        if not endpoint.enabled:
            return {"success": False, "error": "Relay endpoint is disabled"}

        try:
            import litellm

            call_kwargs = {
                "model": f"openai/{model}",
                "api_base": endpoint.base_url,
                "api_key": endpoint.api_key,
                "messages": messages,
                "timeout": endpoint.timeout,
                **kwargs,
            }

            # Add custom headers
            if endpoint.headers:
                call_kwargs["extra_headers"] = endpoint.headers

            response = await asyncio.to_thread(litellm.completion, **call_kwargs)
            try:
                cost = float(litellm.completion_cost(completion_response=response, model=f"openai/{model}"))
            except Exception:
                cost = 0.0
            return {
                "success": True,
                "content": response.choices[0].message.content or "",
                "model": response.model if hasattr(response, "model") else model,
                "usage": dict(response.usage) if hasattr(response, "usage") and response.usage else {},
                "relay": endpoint.name,
                "cost": cost,
            }

        except Exception as e:
            logger.error(f"Relay call failed ({endpoint.name}): {e}")
            # Retry logic
            for retry in range(endpoint.max_retries):
                try:
                    await asyncio.sleep(1 * (retry + 1))
                    response = await asyncio.to_thread(litellm.completion, **call_kwargs)
                    try:
                        cost = float(litellm.completion_cost(completion_response=response, model=f"openai/{model}"))
                    except Exception:
                        cost = 0.0
                    return {
                        "success": True,
                        "content": response.choices[0].message.content or "",
                        "model": response.model if hasattr(response, "model") else model,
                        "relay": endpoint.name,
                        "retried": retry + 1,
                        "usage": dict(response.usage) if hasattr(response, "usage") and response.usage else {},
                        "cost": cost,
                    }
                except Exception:
                    continue

            return {"success": False, "error": str(e), "relay": endpoint.name}


# ─────────────────────────────────────────────
# Global instance
# ─────────────────────────────────────────────
_global_relay_manager: Optional[RelayManager] = None


def get_relay_manager() -> RelayManager:
    global _global_relay_manager
    if _global_relay_manager is None:
        _global_relay_manager = RelayManager()
    return _global_relay_manager
