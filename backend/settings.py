"""
Evermind Backend — Settings Persistence
Saves/loads user settings (API keys, privacy, relay config) to ~/.evermind/config.json
with encrypted secrets at rest.
"""

import base64
import hashlib
import json
import logging
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Dict, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger("evermind.settings")

SETTINGS_DIR = Path.home() / ".evermind"
SETTINGS_FILE = SETTINGS_DIR / "config.json"
SETTINGS_KEY_FILE = SETTINGS_DIR / "settings.key"
SETTINGS_SALT_FILE = SETTINGS_DIR / "settings.salt"
ENCRYPTED_PREFIX = "enc:v1:"

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

_cached_cipher: Optional[Fernet] = None
_cached_cipher_token: Optional[str] = None
_cipher_lock = threading.Lock()


def _chmod_600(path: Path):
    try:
        os.chmod(path, 0o600)
    except PermissionError:
        pass


def _get_or_create_file(path: Path, generator) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    value = generator()
    path.write_bytes(value)
    _chmod_600(path)
    return value


def _get_or_create_salt() -> bytes:
    env_salt = os.getenv("EVERMIND_MASTER_SALT", "").strip()
    if env_salt:
        return env_salt.encode("utf-8")
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    return _get_or_create_file(SETTINGS_SALT_FILE, lambda: os.urandom(16))


def _derive_key(secret: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390000,
    )
    return base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))


def _get_cipher() -> Fernet:
    global _cached_cipher, _cached_cipher_token

    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)

    env_secret = os.getenv("EVERMIND_MASTER_KEY", "").strip()
    if env_secret:
        salt = _get_or_create_salt()
        key = _derive_key(env_secret, salt)
        token = f"env:{hashlib.sha256(key).hexdigest()}"
    else:
        key = _get_or_create_file(SETTINGS_KEY_FILE, Fernet.generate_key)
        token = f"file:{hashlib.sha256(key).hexdigest()}"

    with _cipher_lock:
        if _cached_cipher is not None and _cached_cipher_token == token:
            return _cached_cipher
        _cached_cipher = Fernet(key)
        _cached_cipher_token = token
        return _cached_cipher


def deep_merge_dicts(base: Dict, patch: Dict) -> Dict:
    """Recursively merge nested dicts without losing deep fields."""
    merged = deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dicts(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _encrypt_secret(value: str) -> str:
    if not value:
        return ""
    cipher = _get_cipher()
    return ENCRYPTED_PREFIX + cipher.encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt_secret(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(ENCRYPTED_PREFIX):
        return value  # backward compatibility for old plaintext configs
    cipher = _get_cipher()
    token = value[len(ENCRYPTED_PREFIX):].encode("utf-8")
    try:
        return cipher.decrypt(token).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt settings secret — invalid key or corrupted token")
        return ""


def _encrypt_api_keys(api_keys: Dict[str, str]) -> Dict[str, str]:
    return {name: _encrypt_secret(value) for name, value in (api_keys or {}).items()}


def _decrypt_api_keys(api_keys: Dict[str, str]) -> Dict[str, str]:
    return {name: _decrypt_secret(value) for name, value in (api_keys or {}).items()}


def _encrypt_relay_endpoints(endpoints):
    encrypted = []
    for endpoint in endpoints or []:
        item = dict(endpoint)
        if item.get("api_key"):
            item["api_key"] = _encrypt_secret(item["api_key"])
        encrypted.append(item)
    return encrypted


def _decrypt_relay_endpoints(endpoints):
    decrypted = []
    for endpoint in endpoints or []:
        item = dict(endpoint)
        if item.get("api_key"):
            item["api_key"] = _decrypt_secret(item["api_key"])
        decrypted.append(item)
    return decrypted


def _merge_defaults(saved: Dict) -> Dict:
    return deep_merge_dicts(DEFAULT_SETTINGS, saved or {})


def load_settings() -> Dict:
    """Load settings from disk or return defaults, decrypting secrets in memory."""
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)

            merged = _merge_defaults(saved)

            encrypted_keys = saved.get("api_keys_encrypted") or {}
            source_keys = encrypted_keys or saved.get("api_keys", {})
            merged["api_keys"] = deep_merge_dicts(DEFAULT_SETTINGS["api_keys"], _decrypt_api_keys(source_keys))
            merged["relay_endpoints"] = _decrypt_relay_endpoints(saved.get("relay_endpoints", []))
            logger.info(f"Loaded settings from {SETTINGS_FILE}")
            return merged
    except Exception as e:
        logger.warning(f"Failed to load settings: {e}")
    return deepcopy(DEFAULT_SETTINGS)


def save_settings(settings: Dict) -> bool:
    """Save settings to disk with encrypted API keys and relay secrets."""
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        payload = _merge_defaults(settings)
        decrypted_keys = deep_merge_dicts(DEFAULT_SETTINGS["api_keys"], payload.get("api_keys", {}))
        payload["api_keys_encrypted"] = _encrypt_api_keys(decrypted_keys)
        payload["api_keys"] = {name: "" for name in DEFAULT_SETTINGS["api_keys"].keys()}
        payload["relay_endpoints"] = _encrypt_relay_endpoints(payload.get("relay_endpoints", []))

        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        _chmod_600(SETTINGS_FILE)

        logger.info(
            f"Settings saved to {SETTINGS_FILE} (encrypted api keys: {sum(1 for v in decrypted_keys.values() if v)})"
        )
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
        else:
            os.environ.pop(env_key, None)
    logger.info(f"Applied {count} API keys to environment")
    return count


@contextmanager
def _temporary_env(env_key: Optional[str], value: Optional[str]):
    old_env = os.environ.get(env_key) if env_key else None
    try:
        if env_key and value is not None:
            os.environ[env_key] = value
        yield
    finally:
        if env_key:
            if old_env is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = old_env


def validate_api_key(provider: str, key: str) -> Dict:
    """Quick validation of an API key by making a minimal request without polluting env state."""
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

        env_key = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
        }.get(provider)

        if provider == "kimi":
            kwargs["api_base"] = "https://api.moonshot.cn/v1"
            kwargs["api_key"] = key
            context = _temporary_env(None, None)
        elif provider == "qwen":
            kwargs["api_base"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            kwargs["api_key"] = key
            context = _temporary_env(None, None)
        else:
            context = _temporary_env(env_key, key)

        with context:
            resp = litellm.completion(**kwargs)
            return {"valid": True, "model": resp.model if hasattr(resp, "model") else model}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


# ─────────────────────────────────────────────
# Usage Tracker
# ─────────────────────────────────────────────
class UsageTracker:
    """Track token usage, provider mix, and estimated cost."""

    def __init__(self):
        self._usage: Dict[str, Dict] = {}
        self._recent: list[Dict] = []
        self._lock = threading.Lock()

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float = 0,
        provider: str = "unknown",
        mode: str = "unknown",
    ):
        model_key = model or "unknown"
        with self._lock:
            if model_key not in self._usage:
                self._usage[model_key] = {
                    "provider": provider,
                    "mode": mode,
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost": 0,
                }

            u = self._usage[model_key]
            u["provider"] = provider or u.get("provider", "unknown")
            u["mode"] = mode or u.get("mode", "unknown")
            u["calls"] += 1
            u["prompt_tokens"] += prompt_tokens
            u["completion_tokens"] += completion_tokens
            u["total_tokens"] += prompt_tokens + completion_tokens
            u["cost"] += cost

            self._recent.append({
                "model": model_key,
                "provider": provider,
                "mode": mode,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cost": round(cost, 6),
            })
            self._recent = self._recent[-50:]

    def get_usage(self) -> Dict:
        with self._lock:
            usage = deepcopy(self._usage)
            recent = list(self._recent)

        total_tokens = sum(u["total_tokens"] for u in usage.values())
        total_calls = sum(u["calls"] for u in usage.values())
        total_cost = sum(u["cost"] for u in usage.values())
        return {
            "by_model": {
                model: {**data, "cost": round(data["cost"], 6)}
                for model, data in usage.items()
            },
            "recent_calls": recent,
            "total_tokens": total_tokens,
            "total_calls": total_calls,
            "total_cost": round(total_cost, 6),
        }

    def reset(self):
        with self._lock:
            self._usage.clear()
            self._recent.clear()


_global_tracker = UsageTracker()


def get_usage_tracker() -> UsageTracker:
    return _global_tracker
