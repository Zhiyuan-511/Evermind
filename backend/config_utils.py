"""
Shared helpers for coercing untrusted config values.
"""

from typing import Any, Optional


_TRUE_SET = {"1", "true", "yes", "on", "enabled"}
_FALSE_SET = {"0", "false", "no", "off", "disabled", ""}


def coerce_bool(value: Any, default: bool = False) -> bool:
    """
    Parse booleans from mixed input types safely.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0

    normalized = str(value).strip().lower()
    if normalized in _TRUE_SET:
        return True
    if normalized in _FALSE_SET:
        return False
    return default


def coerce_int(
    value: Any,
    default: int,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """
    Parse integer with fallback and optional clamp.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)

    if minimum is not None and parsed < minimum:
        parsed = minimum
    if maximum is not None and parsed > maximum:
        parsed = maximum
    return parsed
