"""
Evermind Backend — Settings Persistence
Saves/loads user settings (API keys, privacy, relay config) to ~/.evermind/config.json
with encrypted secrets at rest.
"""

import base64
try:
    import fcntl
except ImportError:
    fcntl = None  # Windows: file locking not available
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
SETTINGS_HASH_FILE = SETTINGS_DIR / "config.json.sha256"
SETTINGS_BACKUP_FILE = SETTINGS_DIR / "config.json.bak"
ENCRYPTED_PREFIX = "enc:v1:"
KEY_ROTATION_DAYS = 90  # warn after this many days

DEFAULT_SETTINGS = {
    "api_keys": {
        "openai": "",
        "anthropic": "",
        "gemini": "",
        "deepseek": "",
        "kimi": "",
        "qwen": "",
        # v5.8.6: new providers added to UI — they were always in MODEL_REGISTRY
        # but users had no way to configure their keys until now.
        "minimax": "",
        "zhipu": "",
        "doubao": "",
        "yi": "",
        "aigate": "",  # v5.8.6: relay multi-model relay (sk-ag-*)
        # v5.8.6: optional secondary keys — populated via Settings UI. When
        # present, ai_bridge round-robins between primary and secondary to
        # unblock rate limits on concurrent nodes.
        "openai_2": "",
        "anthropic_2": "",
        "gemini_2": "",
        "deepseek_2": "",
        "kimi_2": "",
        "qwen_2": "",
        "minimax_2": "",
        "zhipu_2": "",
        "doubao_2": "",
        "yi_2": "",
        "aigate_2": "",
    },
    "api_bases": {
        "openai": "",
        "anthropic": "",
        "gemini": "",
        "deepseek": "",
        "kimi": "",
        "qwen": "",
        "minimax": "",
        "zhipu": "",
        "doubao": "",
        "yi": "",
        "aigate": "https://llm.relay/v1",
    },
    "workspace": str(Path.home() / ".evermind" / "workspace"),
    "artifact_sync_dir": "",
    "output_dir": "/tmp/evermind_output",
    "privacy": {
        "enabled": True,
        "showIndicator": True,
        "excludeNodeTypes": ["localshell", "fileread", "filewrite"],
        "customPatterns": [],
    },
    "relay_endpoints": [],
    # v7.1i (2026-04-25): CLI 中转 key 配置
    # 区分订阅类 vs 中转类 CLI：
    #   - claude / gemini = 官方订阅，CLI 内部 OAuth，不需要 key（留空即可）
    #   - codex / kimi / qwen = 走中转或 API key，必须填 key 否则 401
    # 启动时 apply_api_keys() 会把这些 key 注入对应的 env 变量
    # （codex → GMN_OPENAI_API_KEY，kimi → KIMI_API_KEY，qwen → DASHSCOPE_API_KEY），
    # cli_backend.py 的 _build_*_cmd 读 env 决定走中转还是订阅。
    "cli_relay_keys": {
        "codex": {
            "api_key": "",
            "base_url": "https://relay",
            "subscription_mode": False,  # True = 走 ChatGPT 官方 OAuth (codex auth login)
        },
        "kimi": {
            "api_key": "",
            "base_url": "https://api.kimi.com/coding/v1",
            "subscription_mode": False,
        },
        "qwen": {
            "api_key": "",
            "base_url": "",
            "subscription_mode": False,
        },
        "claude": {"subscription_mode": True},  # 官方订阅，不需要 key
        "gemini": {"subscription_mode": True},  # 官方订阅，不需要 key
    },
    "control": {
        "mouseEnabled": True,
        "keyboardEnabled": True,
        "screenCapture": True,
        "maxTimeout": 30,
    },
    "default_model": "gpt-5.4-mini",
    "node_model_preferences": {
        "router": ["gpt-5.4-mini", "kimi-coding"],
        "planner": ["gpt-5.4-mini", "kimi-coding"],
        "analyst": ["gpt-5.4-mini", "kimi-coding"],
        "uidesign": ["gpt-5.4-mini", "kimi-coding"],
        "scribe": ["gpt-5.4-mini", "kimi-coding"],
        "builder": ["gpt-5.4-mini", "kimi-coding"],
        "polisher": ["gpt-5.4-mini", "kimi-coding"],
        "reviewer": ["gpt-5.4-mini", "kimi-coding"],
        "tester": ["gpt-5.4-mini", "kimi-coding"],
        "debugger": ["gpt-5.4-mini", "kimi-coding"],
        "deployer": ["gpt-5.4-mini", "kimi-coding"],
        "imagegen": ["gpt-5.4-mini", "kimi-coding"],
        "spritesheet": ["gpt-5.4-mini", "kimi-coding"],
        "assetimport": ["gpt-5.4-mini", "kimi-coding"],
        "merger": ["gpt-5.4-mini", "kimi-coding"],
    },
    "max_retries": 3,
    "tester_run_smoke": True,
    "browser_headful": False,
    "reviewer_tester_force_headful": False,
    "browser_capture_trace": False,
    "qa_enable_browser_use": False,
    "browser_use_python": "",
    "shell_timeout": 30,
    "builder": {
        "enable_browser_search": False,
    },
    "analyst": {
        "preferred_sites": [
            "https://github.com",
            "https://github.com/KhronosGroup/glTF-Sample-Assets",
            "https://github.com/gdquest-demos/godot-4-3d-third-person-controller",
            "https://github.com/donmccurdy/three-pathfinding",
            "https://github.com/Mugen87/yuka",
            "https://github.com/D4Vinci/Scrapling",
            "https://threejs.org",
            "https://docs.pmnd.rs",
            "https://developer.mozilla.org",
            "https://kenney.nl",
            "https://quaternius.com",
            "https://ambientcg.com",
        ],
        "crawl_intensity": "medium",
        "use_scrapling_when_available": True,
        "enable_query_search": True,
    },
    "image_generation": {
        # v6.1.15 (maintainer): 独立图片生成 API 配置.
        # 用户填了 provider + api_key 就启用真图片生成；否则 imagegen 节点
        # 降级为 SVG/CSS 占位符（不产生 broken <img src> 链接）.
        "provider": "",              # "tongyi" | "doubao-image" | "wenxin" | "seedream" | "flux-fal" | "dalle-3" | "openai-compat"
        "api_key": "",               # provider 专用 key
        "base_url": "",              # 仅当 provider="openai-compat" 或自定义中转站时填
        "default_size": "1024x1024", # 1024x1024 / 1536x1024 / 1024x1536 / 2048x2048
        "default_model": "",         # 留空 → provider 默认；如 wanx2.1-t2i-turbo / doubao-seedream-3-0-t2i-250428
        "max_images_per_run": 10,    # 限量防炸
        "auto_crop": True,           # 生成后是否自动 rembg + PIL 裁剪 4 种比例
        # 旧 ComfyUI 字段保留兼容
        "comfyui_url": "",
        "workflow_template": "",
    },
    "cli_mode": {
        "enabled": False,
        "preferred_cli": "",           # "" = auto-select per node type
        "preferred_model": "",         # "" = use CLI's default model
        "detected_clis": {},           # Populated by /api/cli/detect
        "node_cli_overrides": {},      # {"builder": {"cli": "claude", "model": "sonnet"}, ...}
        # v7.1 (maintainer) ULTRA MODE — 顶级玩家"一次到位"长任务模式
        # 启用后：所有 timeout ×10，iter cap ×4，max_rejections=5，
        # builder 并行 ×4，支持多文件项目脚手架 + 图片生成/爬取 + 打包部署。
        # 任务时长预期 3-4 小时到 1 天。质量 > 速度。
        "ultra_mode": False,
        "ultra_parallel_builders": 4,     # 默认 4 个 builder 并行
        "ultra_max_rejections": 5,        # reviewer 可打回 5 次
        "ultra_total_timeout_sec": 86400, # 24 小时总超时兜底
        "ultra_project_scaffold": True,   # 支持多文件脚手架（非单 HTML）
        "ultra_asset_tools": True,        # 图片生成/爬取/favicon 多尺寸
    },
    "ui_language": "zh",
    # v6.1.3 (maintainer): separate language toggle for node walkthrough
    # reports. "" means "inherit ui_language". When set to "zh" or "en"
    # explicitly, the walkthrough text is forced to that language regardless
    # of UI language.
    "walkthrough_language": "",
    # v6.1.10 (maintainer): when True and user configured TWO API keys
    # for the primary builder provider (e.g. kimi_api_key + kimi_api_key_2),
    # parallel peer builders all use the PREFERRED first model and round-robin
    # across both keys — avoids provider rotation to a weaker fallback model.
    # When False or user only has one key, peer builder #2+ falls through to
    # the user's secondary model to avoid rate-limit collision.
    "peer_builders_share_model_when_multikey": True,
}

_cached_cipher: Optional[Fernet] = None
_cached_cipher_token: Optional[str] = None
_cipher_lock = threading.Lock()
_integrity_warning_fingerprints: set[str] = set()


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

        # Warn if key file is older than KEY_ROTATION_DAYS
        if SETTINGS_KEY_FILE.exists():
            import time as _time
            key_age_days = (_time.time() - SETTINGS_KEY_FILE.stat().st_mtime) / 86400
            if key_age_days > KEY_ROTATION_DAYS:
                logger.warning(
                    f"Encryption key is {int(key_age_days)} days old. "
                    f"Consider rotating it for better security. "
                    f"Set EVERMIND_MASTER_KEY env variable to use a new key."
                )

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


def _write_integrity_hash(raw_bytes: bytes) -> None:
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    SETTINGS_HASH_FILE.write_text(file_hash, encoding="utf-8")
    _chmod_600(SETTINGS_HASH_FILE)


def load_settings() -> Dict:
    """Load settings from disk or return defaults, decrypting secrets in memory."""
    try:
        if SETTINGS_FILE.exists():
            raw_bytes = SETTINGS_FILE.read_bytes()

            # ── Integrity verification ──
            if SETTINGS_HASH_FILE.exists():
                expected_hash = SETTINGS_HASH_FILE.read_text("utf-8").strip()
                actual_hash = hashlib.sha256(raw_bytes).hexdigest()
                if expected_hash != actual_hash:
                    fingerprint = f"{SETTINGS_FILE}:{expected_hash}:{actual_hash}"
                    if fingerprint not in _integrity_warning_fingerprints:
                        _integrity_warning_fingerprints.add(fingerprint)
                        logger.warning(
                            "Settings file integrity check FAILED — file may have been tampered with. "
                            "Expected hash does not match. Loading anyway, but review your config. "
                            "Refreshing the local hash to prevent repeated noise."
                        )
                    else:
                        logger.debug("Settings integrity hash mismatch (suppressed repeat warning)")
                    try:
                        _write_integrity_hash(raw_bytes)
                    except Exception as hash_err:
                        logger.warning(f"Failed to refresh integrity hash after mismatch: {hash_err}")

            saved = json.loads(raw_bytes.decode("utf-8"))

            merged = _merge_defaults(saved)

            encrypted_keys = saved.get("api_keys_encrypted") or {}
            source_keys = encrypted_keys or saved.get("api_keys", {})
            merged["api_keys"] = deep_merge_dicts(DEFAULT_SETTINGS["api_keys"], _decrypt_api_keys(source_keys))
            merged["relay_endpoints"] = _decrypt_relay_endpoints(saved.get("relay_endpoints", []))
            logger.debug(f"Loaded settings from {SETTINGS_FILE}")
            return merged
    except Exception as e:
        logger.warning(f"Failed to load settings: {e}")
    return deepcopy(DEFAULT_SETTINGS)


def save_settings(settings: Dict) -> bool:
    """Save settings to disk with encrypted API keys and relay secrets."""
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)

        # ── Auto-backup before overwriting ──
        if SETTINGS_FILE.exists():
            try:
                import shutil
                shutil.copy2(SETTINGS_FILE, SETTINGS_BACKUP_FILE)
                logger.info(f"Backed up settings to {SETTINGS_BACKUP_FILE}")
            except Exception as bak_err:
                logger.warning(f"Failed to create settings backup: {bak_err}")

        payload = _merge_defaults(settings)
        decrypted_keys = deep_merge_dicts(DEFAULT_SETTINGS["api_keys"], payload.get("api_keys", {}))
        payload["api_keys_encrypted"] = _encrypt_api_keys(decrypted_keys)
        payload["api_keys"] = {name: "" for name in DEFAULT_SETTINGS["api_keys"].keys()}
        payload["relay_endpoints"] = _encrypt_relay_endpoints(payload.get("relay_endpoints", []))

        # On POSIX, lock before truncating to avoid races where another writer
        # opens with "w" and clears the file before acquiring the lock.
        open_mode = "a+" if fcntl else "w"
        with open(SETTINGS_FILE, open_mode, encoding="utf-8") as f:
            if fcntl:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                if fcntl:
                    f.seek(0)
                    f.truncate()
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
            finally:
                if fcntl:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        _chmod_600(SETTINGS_FILE)

        # ── Write SHA-256 integrity hash ──
        try:
            _write_integrity_hash(SETTINGS_FILE.read_bytes())
        except Exception as hash_err:
            logger.warning(f"Failed to write integrity hash: {hash_err}")

        logger.info(
            f"Settings saved to {SETTINGS_FILE} (encrypted api keys: {sum(1 for v in decrypted_keys.values() if v)})"
        )
        return True
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")
        return False


def apply_api_keys(settings: Dict):
    """Set API keys and base URLs as environment variables for LiteLLM.

    v5.8.6: expanded to cover every provider in MODEL_REGISTRY plus optional
    secondary keys (``_2`` suffix) for concurrent-node load balancing. Missing
    providers here caused "paste a MiniMax key, see zero models" symptoms.
    """
    key_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "kimi": "KIMI_API_KEY",
        "qwen": "QWEN_API_KEY",
        "minimax": "MINIMAX_API_KEY",
        "zhipu": "ZHIPU_API_KEY",
        "doubao": "DOUBAO_API_KEY",
        "yi": "YI_API_KEY",
        "aigate": "AIGATE_API_KEY",   # v5.8.6: relay multi-model relay
    }
    base_map = {
        "openai": "OPENAI_API_BASE",
        "anthropic": "ANTHROPIC_API_BASE",
        "gemini": "GEMINI_API_BASE",
        "deepseek": "DEEPSEEK_API_BASE",
        "kimi": "KIMI_API_BASE",
        "qwen": "QWEN_API_BASE",
        "minimax": "MINIMAX_API_BASE",
        "zhipu": "ZHIPU_API_BASE",
        "doubao": "DOUBAO_API_BASE",
        "yi": "YI_API_BASE",
        "aigate": "AIGATE_API_BASE",
    }
    count = 0
    for name, env_key in key_map.items():
        val = settings.get("api_keys", {}).get(name, "")
        if val:
            os.environ[env_key] = val
            count += 1
        else:
            os.environ.pop(env_key, None)
        # Secondary key — pool reads {ENV}_2 for concurrent load balancing
        sec_val = settings.get("api_keys", {}).get(f"{name}_2", "")
        sec_env = f"{env_key}_2"
        if sec_val:
            os.environ[sec_env] = sec_val
            count += 1
        else:
            os.environ.pop(sec_env, None)
    # Apply relay/proxy base URLs
    base_count = 0
    for name, env_key in base_map.items():
        val = settings.get("api_bases", {}).get(name, "")
        if val:
            os.environ[env_key] = val
            base_count += 1
        else:
            os.environ.pop(env_key, None)
    # v7.1i: CLI relay keys → env vars consumed by cli_backend._build_*_cmd
    # codex/kimi/qwen 走中转时要这些 env，订阅模式（claude/gemini）不需要。
    cli_relay = settings.get("cli_relay_keys", {}) or {}
    cli_env_map = {
        "codex": "GMN_OPENAI_API_KEY",
        "kimi": "KIMI_API_KEY",
        "qwen": "DASHSCOPE_API_KEY",
    }
    cli_count = 0
    for cli_name, env_key in cli_env_map.items():
        cli_cfg = cli_relay.get(cli_name, {}) or {}
        # subscription_mode=True → 主动清掉 env，确保 cli_backend 走订阅路径
        if cli_cfg.get("subscription_mode"):
            os.environ.pop(env_key, None)
            continue
        api_key = (cli_cfg.get("api_key") or "").strip()
        if api_key:
            os.environ[env_key] = api_key
            cli_count += 1
        else:
            os.environ.pop(env_key, None)
    logger.info(
        f"Applied {count} API keys, {base_count} base URLs, {cli_count} CLI relay keys to environment"
    )
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
            "kimi": "openai/kimi-k2.5",
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
            # v6.1: coding-plan keys (sk-kimi-*) must hit api.kimi.com/coding/v1
            # with UA=claude-code/0.1.0 (official white-listed). Legacy Moonshot
            # platform keys use moonshot.cn with vanilla Authorization headers.
            if key.startswith("sk-kimi-"):
                kwargs["model"] = "openai/kimi-k2.5"
                kwargs["api_base"] = "https://api.kimi.com/coding/v1"
                kwargs["extra_headers"] = {
                    "User-Agent": "claude-code/0.1.0",
                    "X-Client-Name": "claude-code",
                }
            else:
                kwargs["model"] = "openai/moonshot-v1-8k"
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
