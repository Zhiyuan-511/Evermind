"""Provider registry. `resolve_provider(model)` returns the right plugin."""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Type

from .base import BaseProvider

logger = logging.getLogger("evermind.providers.registry")

_LOCK = threading.RLock()
_REGISTRY: List[Type[BaseProvider]] = []


def register_provider(cls: Type[BaseProvider]) -> Type[BaseProvider]:
    """Decorator / function to register a BaseProvider subclass.

    Providers are matched in registration order; the first one whose
    `matches(model_name)` returns True wins. Register vendor-specific
    providers before generic fallbacks.
    """
    if not issubclass(cls, BaseProvider):
        raise TypeError(f"{cls.__name__} must inherit BaseProvider")
    if not cls.name:
        raise ValueError(f"{cls.__name__}.name must be non-empty")
    with _LOCK:
        for existing in _REGISTRY:
            if existing is cls:
                return cls
            if existing.name == cls.name:
                logger.info("provider %s re-registered, replacing %s", cls.name, existing.__name__)
                _REGISTRY.remove(existing)
                break
        _REGISTRY.append(cls)
    return cls


def resolve_provider(
    model_name: str,
    *,
    api_key: str = "",
    api_base: str = "",
    extra_headers: Optional[Dict[str, str]] = None,
) -> Optional[BaseProvider]:
    """Find the provider for a model and instantiate it.

    Returns None if no provider matches. Caller (ai_bridge) can fall back
    to the legacy code path when None is returned, which keeps the
    migration incremental.
    """
    if not model_name:
        return None
    target = str(model_name).strip()
    with _LOCK:
        for cls in _REGISTRY:
            try:
                if cls.matches(target):
                    return cls(
                        api_key=api_key,
                        api_base=api_base,
                        extra_headers=extra_headers,
                    )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("provider %s match failed for %s: %s", cls.name, target, exc)
    return None


def known_providers() -> List[str]:
    """Return registered provider names in registration order."""
    with _LOCK:
        return [cls.name for cls in _REGISTRY]


def reset_registry_for_tests() -> None:
    """Clear the registry. Tests only — never call in production code."""
    with _LOCK:
        _REGISTRY.clear()
