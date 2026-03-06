"""
Evermind Backend — Settings Persistence
Saves/loads user settings (API keys, privacy, relay config) to ~/.evermind/config.json
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("evermind.settings")

SETTINGS_DIR = Path.home() / ".evermind"
SETTINGS_FILE = SETTINGS_DIR / "config.json"

DEFAULT_SETTINGS = {
    "api_keys": {
        "openai": "",
        "anthropic": "",
        "gemini": "",
        "deepseek": "",
        "kimi": "",
        "qwen": "",
    },
    "workspace": str(Path.home() / "Desktop"),
    "output_dir": "/tmp/evermind_output",
    "privacy": {
        "enabled": True,
        "showIndicator": True,
        "excludeNodeTypes": ["localshell", "fileread", "filewrite"],
        "customPatterns": [],
    },
    "relay_endpoints": [],
    "control": {
        "mouseEnabled": True,
        "keyboardEnabled": True,
        "screenCapture": True,
        "maxTimeout": 30,
    },
    "default_model": "gpt-5.4",
    "max_retries": 3,
    "shell_timeout": 30,
}


def load_settings() -> Dict:
    """Load settings from disk or return defaults."""
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # Merge with defaults (so new fields are added)
            merged = {**DEFAULT_SETTINGS}
            for key, val in saved.items():
                if isinstance(val, dict) and key in merged and isinstance(merged[key], dict):
                    merged[key] = {**merged[key], **val}
                else:
                    merged[key] = val
            logger.info(f"Loaded settings from {SETTINGS_FILE}")
            return merged
    except Exception as e:
        logger.warning(f"Failed to load settings: {e}")
    return DEFAULT_SETTINGS.copy()


def save_settings(settings: Dict) -> bool:
    """Save settings to disk."""
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        # Scrub sensitive data for logging
        safe = {k: ("***" if "key" in k.lower() else v) for k, v in settings.get("api_keys", {}).items()}
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        logger.info(f"Settings saved to {SETTINGS_FILE} (keys: {list(safe.keys())})")
        return True
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")
        return False


def apply_api_keys(settings: Dict):
    """Set API keys as environment variables for LiteLLM."""
    key_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "kimi": "KIMI_API_KEY",
        "qwen": "QWEN_API_KEY",
    }
    count = 0
    for name, env_key in key_map.items():
        val = settings.get("api_keys", {}).get(name, "")
        if val:
            os.environ[env_key] = val
            count += 1
    logger.info(f"Applied {count} API keys to environment")
    return count


def validate_api_key(provider: str, key: str) -> Dict:
    """Quick validation of an API key by making a minimal request."""
    if not key:
        return {"valid": False, "error": "No key provided"}

    try:
        import litellm

        model_map = {
            "openai": "gpt-4o-mini",
            "anthropic": "claude-3-haiku-20240307",
            "gemini": "gemini/gemini-2.0-flash",
            "deepseek": "deepseek/deepseek-chat",
            "kimi": "openai/moonshot-v1-8k",
            "qwen": "openai/qwen-turbo",
        }
        model = model_map.get(provider, "gpt-4o-mini")

        kwargs = {"model": model, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5, "timeout": 10}
        if provider == "kimi":
            kwargs["api_base"] = "https://api.moonshot.cn/v1"
            kwargs["api_key"] = key
        elif provider == "qwen":
            kwargs["api_base"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            kwargs["api_key"] = key
        else:
            env_key = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                       "gemini": "GEMINI_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}.get(provider, "OPENAI_API_KEY")
            os.environ[env_key] = key

        resp = litellm.completion(**kwargs)
        return {"valid": True, "model": resp.model if hasattr(resp, "model") else model}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


# ─────────────────────────────────────────────
# Usage Tracker
# ─────────────────────────────────────────────
class UsageTracker:
    """Track token usage per model."""

    def __init__(self):
        self._usage: Dict[str, Dict] = {}

    def record(self, model: str, prompt_tokens: int, completion_tokens: int, cost: float = 0):
        if model not in self._usage:
            self._usage[model] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0}
        u = self._usage[model]
        u["calls"] += 1
        u["prompt_tokens"] += prompt_tokens
        u["completion_tokens"] += completion_tokens
        u["total_tokens"] += prompt_tokens + completion_tokens
        u["cost"] += cost

    def get_usage(self) -> Dict:
        total_tokens = sum(u["total_tokens"] for u in self._usage.values())
        total_calls = sum(u["calls"] for u in self._usage.values())
        total_cost = sum(u["cost"] for u in self._usage.values())
        return {
            "by_model": self._usage,
            "total_tokens": total_tokens,
            "total_calls": total_calls,
            "total_cost": round(total_cost, 4),
        }

    def reset(self):
        self._usage.clear()


_global_tracker = UsageTracker()


def get_usage_tracker() -> UsageTracker:
    return _global_tracker
