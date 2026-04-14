from __future__ import annotations

import os
import re
import socket
import time
from collections import deque
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from plugins.base import is_image_generation_available
from proxy_relay import get_relay_manager
from runtime_paths import resolve_state_dir
from settings import SETTINGS_FILE, SETTINGS_HASH_FILE, load_settings


CRITICAL_BACKEND_FILES = [
    "ai_bridge.py",
    "html_postprocess.py",
    "orchestrator.py",
    "plugins/implementations.py",
    "preview_validation.py",
    "proxy_relay.py",
    "release_doctor.py",
    "repo_map.py",
    "scripts/desktop_run_goal_monitor.py",
    "scripts/release_doctor.py",
    "server.py",
    "task_classifier.py",
    "runtime_vendor/three/three.min.js",
    "runtime_vendor/phaser/phaser.min.js",
    "runtime_vendor/howler/howler.min.js",
    "workflow_templates.py",
]

RUNTIME_VENDOR_FILES = [
    "runtime_vendor/three/three.min.js",
    "runtime_vendor/phaser/phaser.min.js",
    "runtime_vendor/howler/howler.min.js",
]

_PROVIDER_ENV_MAP = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "KIMI_API_KEY",
    "qwen": "QWEN_API_KEY",
}

_PROVIDER_BASE_ENV_MAP = {
    "openai": "OPENAI_API_BASE",
    "anthropic": "ANTHROPIC_API_BASE",
    "gemini": "GEMINI_API_BASE",
    "deepseek": "DEEPSEEK_API_BASE",
    "kimi": "KIMI_API_BASE",
    "qwen": "QWEN_API_BASE",
}

LOG_FILE = resolve_state_dir() / "logs" / "evermind-backend.log"

_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")
_RELAY_LOAD_RE = re.compile(r"Loaded (\d+) relay endpoint\(s\) from settings")
_GATEWAY_REJECTION_RE = re.compile(
    r"Compatible gateway rejection cooldown: provider=(?P<provider>\w+) host=(?P<host>[^\s]+) cooldown=(?P<cooldown>\d+)s error=(?P<error>.+)$"
)
_GATEWAY_CIRCUIT_RE = re.compile(
    r"Compatible gateway circuit OPEN: provider=(?P<provider>\w+) host=(?P<host>[^\s]+) failures=(?P<failures>\d+) error=(?P<error>.+)$"
)
_GATEWAY_RECOVERED_RE = re.compile(
    r"Compatible gateway recovered: provider=(?P<provider>\w+) host=(?P<host>[^\s]+) latency_ms=(?P<latency>[^\s]+) ewma_ms=(?P<ewma>[^\s]+)"
)
_MODEL_FALLBACK_RE = re.compile(
    r"Model fallback: node=(?P<node>[\w-]+) from=(?P<from_model>[^\s]+) to=(?P<to_model>[^\s]+) error=(?P<error>.+)$"
)


def _detect_source_root(current_backend_dir: Path) -> Optional[Path]:
    try:
        candidates = [current_backend_dir.resolve(), *current_backend_dir.resolve().parents]
    except Exception:
        candidates = [current_backend_dir, *current_backend_dir.parents]
    for candidate in candidates:
        if all((candidate / rel).exists() for rel in ("backend", "electron", "frontend")):
            return candidate
    return None


def _detect_current_app(current_backend_dir: Path) -> Optional[Path]:
    resources_dir = current_backend_dir.parent
    if resources_dir.name != "Resources":
        return None
    contents_dir = resources_dir.parent
    app_dir = contents_dir.parent
    if contents_dir.name != "Contents":
        return None
    if not app_dir.name.endswith(".app"):
        return None
    return app_dir


def _detect_project_paths(current_backend_dir: Optional[Path] = None) -> Dict[str, Optional[Path]]:
    backend_dir = Path(current_backend_dir or Path(__file__).resolve().parent)
    source_root = _detect_source_root(backend_dir)
    current_app = _detect_current_app(backend_dir)
    return {
        "backend_dir": backend_dir,
        "source_root": source_root,
        "current_app": current_app,
        "local_app": (source_root / "Evermind.app") if source_root else None,
        "dist_app": (source_root / "electron" / "dist" / "mac-arm64" / "Evermind.app") if source_root else None,
        "desktop_app": Path.home() / "Desktop" / "Evermind.app",
    }


def _app_backend_dir(app_path: Optional[Path]) -> Optional[Path]:
    if not app_path:
        return None
    return app_path / "Contents" / "Resources" / "backend"


def _app_frontend_bundle(app_path: Optional[Path]) -> Optional[Path]:
    if not app_path:
        return None
    return app_path / "Contents" / "Resources" / "frontend-standalone" / "server.js"


def _app_has_runtime_bundle(app_path: Optional[Path]) -> bool:
    bundle = _app_frontend_bundle(app_path)
    return bool(bundle and bundle.exists())


def _app_freshness(app_path: Optional[Path]) -> float:
    if not app_path:
        return 0.0
    candidates = [
        _app_frontend_bundle(app_path),
        (_app_backend_dir(app_path) / "orchestrator.py") if _app_backend_dir(app_path) else None,
    ]
    freshest = 0.0
    for candidate in candidates:
        try:
            freshest = max(freshest, float(candidate.stat().st_mtime))
        except Exception:
            continue
    return freshest


def _resolve_desktop_sync_source(local_app: Optional[Path], dist_app: Optional[Path]) -> Optional[Path]:
    candidates = [app for app in (local_app, dist_app) if _app_has_runtime_bundle(app)]
    if not candidates:
        return None
    candidates.sort(key=_app_freshness, reverse=True)
    return candidates[0]


def _path_text(path: Optional[Path]) -> str:
    return str(path) if path else ""


def _issue(
    *,
    severity: str,
    code: str,
    message: str,
    hint: str = "",
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "hint": hint,
        "details": details or {},
    }


def _compare_file_sets(left_dir: Optional[Path], right_dir: Optional[Path], rel_paths: List[str]) -> Dict[str, Any]:
    result = {
        "status": "missing",
        "left": _path_text(left_dir),
        "right": _path_text(right_dir),
        "checked": 0,
        "missing_left": [],
        "missing_right": [],
        "mismatches": [],
    }
    if not left_dir or not right_dir or not left_dir.exists() or not right_dir.exists():
        return result

    missing_left: List[str] = []
    missing_right: List[str] = []
    mismatches: List[str] = []
    checked = 0
    for rel_path in rel_paths:
        left_path = left_dir / rel_path
        right_path = right_dir / rel_path
        if not left_path.exists():
            missing_left.append(rel_path)
            continue
        if not right_path.exists():
            missing_right.append(rel_path)
            continue
        checked += 1
        try:
            if left_path.read_bytes() != right_path.read_bytes():
                mismatches.append(rel_path)
        except Exception:
            mismatches.append(rel_path)

    result["checked"] = checked
    result["missing_left"] = missing_left
    result["missing_right"] = missing_right
    result["mismatches"] = mismatches
    if not missing_left and not missing_right and not mismatches:
        result["status"] = "pass"
    elif missing_left or missing_right:
        result["status"] = "missing"
    else:
        result["status"] = "drift"
    return result


def _is_port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _probe_path_writable(path: Path) -> Tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".release_doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _resolve_model_provider(model_name: str) -> str:
    normalized = str(model_name or "").strip()
    if not normalized:
        return ""
    if normalized.startswith(("relay/", "relay_pool/")):
        return "relay"
    try:
        from ai_bridge import MODEL_REGISTRY

        provider = str((MODEL_REGISTRY.get(normalized) or {}).get("provider") or "").strip().lower()
        if provider:
            return "gemini" if provider == "google" else provider
    except Exception:
        pass

    lower = normalized.lower()
    if lower.startswith(("gpt-", "o1", "o3")):
        return "openai"
    if lower.startswith("claude"):
        return "anthropic"
    if lower.startswith("gemini"):
        return "gemini"
    if lower.startswith("deepseek"):
        return "deepseek"
    if lower.startswith("kimi"):
        return "kimi"
    if lower.startswith("qwen"):
        return "qwen"
    return ""


def _provider_key_presence(settings_data: Dict[str, Any]) -> Dict[str, bool]:
    decrypted = settings_data.get("api_keys", {}) if isinstance(settings_data.get("api_keys"), dict) else {}
    result: Dict[str, bool] = {}
    for provider, env_key in _PROVIDER_ENV_MAP.items():
        canonical = "gemini" if provider == "google" else provider
        result[canonical] = bool(str(decrypted.get(canonical, "") or "").strip() or os.getenv(env_key))
    return result


def _provider_base_config(settings_data: Dict[str, Any]) -> Dict[str, str]:
    api_bases = settings_data.get("api_bases", {}) if isinstance(settings_data.get("api_bases"), dict) else {}
    result: Dict[str, str] = {}
    for provider, env_key in _PROVIDER_BASE_ENV_MAP.items():
        result[provider] = str(api_bases.get(provider, "") or os.getenv(env_key, "") or "").strip()
    return result


def _log_timestamp(line: str) -> Tuple[str, float]:
    match = _LOG_TS_RE.match(str(line or ""))
    if not match:
        return "", 0.0
    raw = f"{match.group(1)}.{match.group(2)}"
    try:
        return raw, datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except Exception:
        return raw, 0.0


def _tail_log_lines(path: Path, *, max_lines: int = 1200) -> List[str]:
    if not path.exists() or not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return list(deque(handle, maxlen=max_lines))
    except Exception:
        return []


def _recent_runtime_route_events(log_file: Optional[Path] = None) -> Dict[str, Any]:
    path = Path(log_file or LOG_FILE)
    lines = _tail_log_lines(path)
    snapshot: Dict[str, Any] = {
        "log_file": str(path),
        "log_exists": path.exists(),
        "relay_count": None,
        "gateway_states": {},
        "model_fallbacks": {},
    }
    if not lines:
        return snapshot

    gateway_states: Dict[str, Dict[str, Any]] = {}
    model_fallbacks: Dict[str, Dict[str, Any]] = {}
    relay_count: Optional[int] = None

    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        timestamp_raw, timestamp_epoch = _log_timestamp(line)

        relay_match = _RELAY_LOAD_RE.search(line)
        if relay_match:
            try:
                relay_count = int(relay_match.group(1))
            except Exception:
                pass

        rejection_match = _GATEWAY_REJECTION_RE.search(line)
        if rejection_match:
            provider = str(rejection_match.group("provider") or "").strip().lower()
            host = str(rejection_match.group("host") or "").strip()
            key = f"{provider}:{host}"
            gateway_states[key] = {
                "provider": provider,
                "host": host,
                "status": "rejection_cooldown",
                "cooldown_sec": int(rejection_match.group("cooldown") or 0),
                "last_error": str(rejection_match.group("error") or "").strip(),
                "observed_at": timestamp_raw,
                "observed_at_epoch": timestamp_epoch,
                "source": "logs",
            }
            continue

        circuit_match = _GATEWAY_CIRCUIT_RE.search(line)
        if circuit_match:
            provider = str(circuit_match.group("provider") or "").strip().lower()
            host = str(circuit_match.group("host") or "").strip()
            key = f"{provider}:{host}"
            gateway_states[key] = {
                "provider": provider,
                "host": host,
                "status": "circuit_open",
                "failure_count": int(circuit_match.group("failures") or 0),
                "last_error": str(circuit_match.group("error") or "").strip(),
                "observed_at": timestamp_raw,
                "observed_at_epoch": timestamp_epoch,
                "source": "logs",
            }
            continue

        recovered_match = _GATEWAY_RECOVERED_RE.search(line)
        if recovered_match:
            provider = str(recovered_match.group("provider") or "").strip().lower()
            host = str(recovered_match.group("host") or "").strip()
            key = f"{provider}:{host}"
            gateway_states[key] = {
                "provider": provider,
                "host": host,
                "status": "healthy",
                "last_latency_ms": float(recovered_match.group("latency") or 0),
                "ewma_latency_ms": float(recovered_match.group("ewma") or 0),
                "last_error": "",
                "observed_at": timestamp_raw,
                "observed_at_epoch": timestamp_epoch,
                "source": "logs",
            }
            continue

        fallback_match = _MODEL_FALLBACK_RE.search(line)
        if fallback_match:
            from_model = str(fallback_match.group("from_model") or "").strip()
            if not from_model:
                continue
            model_fallbacks[from_model] = {
                "model": from_model,
                "node": str(fallback_match.group("node") or "").strip(),
                "to_model": str(fallback_match.group("to_model") or "").strip(),
                "error": str(fallback_match.group("error") or "").strip(),
                "observed_at": timestamp_raw,
                "observed_at_epoch": timestamp_epoch,
                "source": "logs",
            }

    snapshot["relay_count"] = relay_count
    snapshot["gateway_states"] = gateway_states
    snapshot["model_fallbacks"] = model_fallbacks
    return snapshot


def _gateway_host_from_base(provider_base: str) -> str:
    value = str(provider_base or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        return str(parsed.netloc or parsed.path or value).strip()
    except Exception:
        return value


def _route_runtime_health(
    provider: str,
    provider_base: str,
    model_name: str,
    runtime_events: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    provider_name = str(provider or "").strip().lower()
    host = _gateway_host_from_base(provider_base)
    if not provider_name or not host or not isinstance(runtime_events, dict):
        return {}

    gateway_states = runtime_events.get("gateway_states")
    state = dict((gateway_states or {}).get(f"{provider_name}:{host}") or {})
    if not state:
        return {}

    now = time.time()
    observed_at_epoch = float(state.get("observed_at_epoch") or 0.0)
    status = str(state.get("status") or "").strip()
    if status == "rejection_cooldown":
        cooldown_sec = int(state.get("cooldown_sec") or 0)
        remaining_sec = max(0, cooldown_sec - int(max(0.0, now - observed_at_epoch)))
        state["remaining_sec"] = remaining_sec
        state["active"] = remaining_sec > 0
        if remaining_sec <= 0:
            state["status"] = "recent_rejection"
    elif status == "circuit_open":
        recovery_sec = 45
        remaining_sec = max(0, recovery_sec - int(max(0.0, now - observed_at_epoch)))
        state["remaining_sec"] = remaining_sec
        state["active"] = remaining_sec > 0
        state["circuit_open_sec"] = recovery_sec
        if remaining_sec <= 0:
            state["status"] = "recent_circuit_open"
    else:
        state["active"] = False

    fallback = dict(((runtime_events.get("model_fallbacks") or {}).get(str(model_name or "").strip())) or {})
    if fallback:
        state["fallback"] = fallback
    state["host"] = host
    return state


def _gateway_runtime_warning(
    route_info: Dict[str, Any],
    *,
    label: str,
) -> Optional[Dict[str, Any]]:
    health = route_info.get("gateway_health") if isinstance(route_info.get("gateway_health"), dict) else {}
    if not health:
        return None

    status = str(health.get("status") or "").strip()
    host = str(health.get("host") or route_info.get("gateway_host") or "gateway").strip()
    fallback = health.get("fallback") if isinstance(health.get("fallback"), dict) else {}
    fallback_model = str(fallback.get("to_model") or "").strip()
    fallback_node = str(fallback.get("node") or "").strip()
    observed_at = str(health.get("observed_at") or "").strip()
    last_error = str(health.get("last_error") or "").strip()
    remaining_sec = int(health.get("remaining_sec") or 0)

    if status == "rejection_cooldown" and remaining_sec > 0:
        message = (
            f"{label} is configured through compatible gateway '{host}', but runtime logs show an active rejection cooldown "
            f"({remaining_sec}s remaining)."
        )
        if last_error:
            message += f" Last error: {last_error}"
        hint = "Either remove the custom OPENAI-compatible base, or add a real relay endpoint/pool so the route is not pinned to one blocked gateway."
        if fallback_model:
            hint += f" Current fallback observed in logs: {fallback_node or 'node'} -> {fallback_model}."
        return {
            "severity": "warning",
            "code": "gateway-rejection-cooldown",
            "message": message,
            "hint": hint,
            "details": {"label": label, **route_info},
        }

    if status in {"circuit_open", "recent_circuit_open"}:
        message = f"{label} recently tripped the compatible gateway circuit for '{host}'."
        if status == "circuit_open" and remaining_sec > 0:
            message = f"{label} is still circuit-open on compatible gateway '{host}' ({remaining_sec}s remaining)."
        if observed_at:
            message += f" Observed at {observed_at}."
        if last_error:
            message += f" Last error: {last_error}"
        return {
            "severity": "warning",
            "code": "gateway-circuit-open",
            "message": message,
            "hint": "Add a second route or relay pool so transient gateway failures do not demote the whole node.",
            "details": {"label": label, **route_info},
        }

    if status == "recent_rejection":
        message = f"{label} recently hit a compatible gateway rejection on '{host}'."
        if observed_at:
            message += f" Latest event: {observed_at}."
        if last_error:
            message += f" Last error: {last_error}"
        return {
            "severity": "warning",
            "code": "gateway-recent-rejection",
            "message": message,
            "hint": "Even if the cooldown expired, repeated rejections usually mean the gateway is policy-blocking this workload.",
            "details": {"label": label, **route_info},
        }

    return None


def _model_ready(model_name: str, provider_keys: Dict[str, bool]) -> bool:
    normalized = str(model_name or "").strip()
    if not normalized:
        return False
    relay_mgr = get_relay_manager()
    try:
        if relay_mgr.relay_model_candidates_for(normalized):
            return True
    except Exception:
        pass
    provider = _resolve_model_provider(normalized)
    if not provider:
        return False
    if provider == "relay":
        return True
    return bool(provider_keys.get(provider))


def _model_route_info(
    model_name: str,
    provider_keys: Dict[str, bool],
    provider_bases: Dict[str, str],
    relay_mgr=None,
    runtime_events: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    normalized = str(model_name or "").strip()
    info = {
        "model": normalized,
        "provider": "",
        "ready": False,
        "provider_key_ready": False,
        "provider_base": "",
        "route_types": [],
        "preferred_route": "",
        "relay_candidates": [],
        "gateway_host": "",
        "gateway_health": {},
    }
    if not normalized:
        return info

    relay_mgr = relay_mgr or get_relay_manager()
    provider = _resolve_model_provider(normalized)
    info["provider"] = provider

    if provider == "relay":
        preferred_route = "relay_pool" if normalized.startswith("relay_pool/") else "relay"
        info["ready"] = True
        info["preferred_route"] = preferred_route
        info["route_types"] = [preferred_route]
        return info

    provider_key_ready = bool(provider_keys.get(provider)) if provider else False
    provider_base = str(provider_bases.get(provider, "") or "").strip()
    relay_candidates: List[str] = []
    try:
        relay_candidates = [
            str(candidate or "").strip()
            for candidate in relay_mgr.relay_model_candidates_for(normalized)
            if str(candidate or "").strip()
        ]
    except Exception:
        relay_candidates = []

    route_types: List[str] = []
    preferred_route = ""
    if provider_key_ready:
        preferred_route = "compatible_gateway" if provider_base else "direct_provider"
        route_types.append(preferred_route)
    if relay_candidates:
        route_types.append("relay")
        if not preferred_route:
            preferred_route = "relay"

    info["provider_key_ready"] = provider_key_ready
    info["provider_base"] = provider_base
    info["relay_candidates"] = relay_candidates
    info["route_types"] = route_types
    info["preferred_route"] = preferred_route
    if provider_base:
        info["gateway_host"] = _gateway_host_from_base(provider_base)
        info["gateway_health"] = _route_runtime_health(provider, provider_base, normalized, runtime_events)
    info["ready"] = bool(route_types)
    return info


def _describe_route(info: Dict[str, Any]) -> str:
    preferred_route = str(info.get("preferred_route") or "").strip()
    provider = str(info.get("provider") or "").strip() or "provider"
    provider_base = str(info.get("provider_base") or "").strip()
    relay_candidates = info.get("relay_candidates") if isinstance(info.get("relay_candidates"), list) else []
    gateway_host = str(info.get("gateway_host") or "").strip()

    if preferred_route == "compatible_gateway":
        parsed = urlparse(provider_base)
        host = gateway_host or str(parsed.netloc or parsed.path or provider_base or provider or "gateway").strip()
        return f"compatible gateway ({host})"
    if preferred_route == "direct_provider":
        return f"direct {provider} provider"
    if preferred_route == "relay_pool":
        return "relay pool"
    if preferred_route == "relay":
        if relay_candidates:
            return f"relay route ({relay_candidates[0]})"
        return "relay route"
    return "unavailable"


def _local_service_status(url: str) -> Dict[str, Any]:
    value = str(url or "").strip()
    if not value:
        return {"configured": False, "reachable": None, "host": "", "port": None}
    parsed = urlparse(value)
    host = str(parsed.hostname or "").strip()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if host not in {"127.0.0.1", "localhost"}:
        return {"configured": True, "reachable": None, "host": host, "port": port}
    return {
        "configured": True,
        "reachable": _is_port_open(host, port),
        "host": host,
        "port": port,
    }


def _runtime_vendor_status(backend_dir: Path) -> Dict[str, Any]:
    missing = [rel for rel in RUNTIME_VENDOR_FILES if not (backend_dir / rel).exists()]
    return {
        "path": str(backend_dir / "runtime_vendor"),
        "missing": missing,
        "ok": not missing,
    }


def build_release_doctor_report(
    *,
    settings_data: Optional[Dict[str, Any]] = None,
    playwright_status: Optional[Dict[str, Any]] = None,
    current_backend_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    config = deepcopy(settings_data) if isinstance(settings_data, dict) else load_settings()
    playwright = dict(playwright_status or {})
    paths = _detect_project_paths(current_backend_dir=current_backend_dir)
    backend_dir = Path(paths["backend_dir"] or Path(__file__).resolve().parent)
    source_root = paths["source_root"]
    local_app = paths["local_app"]
    dist_app = paths["dist_app"]
    desktop_app = paths["desktop_app"]

    provider_keys = _provider_key_presence(config)
    provider_bases = _provider_base_config(config)
    runtime_events = _recent_runtime_route_events()
    relay_mgr = get_relay_manager()
    relay_entries = relay_mgr.list()
    enabled_relays = [entry for entry in relay_entries if bool(entry.get("enabled", True))]
    tls_warning_relays = [entry.get("name", "") for entry in enabled_relays if bool(entry.get("tls_warning"))]

    node_model_preferences = (
        config.get("node_model_preferences", {})
        if isinstance(config.get("node_model_preferences"), dict)
        else {}
    )
    uncovered_nodes: List[str] = []
    resolved_node_models: Dict[str, List[str]] = {}
    for node_type, chain in node_model_preferences.items():
        candidates = [str(item or "").strip() for item in (chain or []) if str(item or "").strip()]
        viable = [model for model in candidates if _model_ready(model, provider_keys)]
        resolved_node_models[str(node_type)] = viable
        if candidates and not viable:
            uncovered_nodes.append(str(node_type))

    default_model = str(config.get("default_model", "") or "").strip()
    default_model_ready = _model_ready(default_model, provider_keys)
    builder_requested_chain = [
        str(item or "").strip()
        for item in (
            node_model_preferences.get("builder", [])
            if isinstance(node_model_preferences.get("builder"), list)
            else ([node_model_preferences.get("builder")] if str(node_model_preferences.get("builder") or "").strip() else [])
        )
        if str(item or "").strip()
    ]
    if not builder_requested_chain and default_model:
        builder_requested_chain = [default_model]
    builder_route_chain = [
        _model_route_info(
            model_name,
            provider_keys,
            provider_bases,
            relay_mgr=relay_mgr,
            runtime_events=runtime_events,
        )
        for model_name in builder_requested_chain
    ]
    builder_viable_chain = [
        str(item.get("model") or "").strip()
        for item in builder_route_chain
        if bool(item.get("ready"))
    ]
    builder_primary_route = next(
        (item for item in builder_route_chain if bool(item.get("ready"))),
        builder_route_chain[0] if builder_route_chain else {},
    )
    gpt54_referenced = any(
        "gpt-5.4" in [str(item or "").strip() for item in (chain or [])]
        for chain in node_model_preferences.values()
    ) or default_model == "gpt-5.4"
    gpt54_route = _model_route_info(
        "gpt-5.4",
        provider_keys,
        provider_bases,
        relay_mgr=relay_mgr,
        runtime_events=runtime_events,
    )
    gpt54_ready = bool(gpt54_route.get("ready"))

    image_config = config.get("image_generation", {}) if isinstance(config.get("image_generation"), dict) else {}
    image_available = is_image_generation_available(config)
    image_workflow = str(image_config.get("workflow_template", "") or "").strip()
    image_workflow_exists = Path(image_workflow).expanduser().exists() if image_workflow else False
    image_service = _local_service_status(str(image_config.get("comfyui_url", "") or "").strip())

    output_dir = Path(str(config.get("output_dir", "") or "") or "/tmp/evermind_output").expanduser()
    output_writable, output_error = _probe_path_writable(output_dir)
    browser_use_enabled = bool(config.get("qa_enable_browser_use"))
    browser_use_python = Path(str(config.get("browser_use_python", "") or "").strip()).expanduser() if str(config.get("browser_use_python", "") or "").strip() else None

    current_frontend_bundle = _app_frontend_bundle(paths["current_app"])
    dist_frontend_bundle = _app_frontend_bundle(dist_app)
    desktop_frontend_bundle = _app_frontend_bundle(desktop_app)
    local_app_frontend_bundle = _app_frontend_bundle(local_app)
    desktop_sync_source = _resolve_desktop_sync_source(local_app, dist_app)

    local_app_sync = _compare_file_sets(source_root / "backend" if source_root else None, _app_backend_dir(local_app), CRITICAL_BACKEND_FILES)
    desktop_sync = _compare_file_sets(_app_backend_dir(desktop_sync_source), _app_backend_dir(desktop_app), CRITICAL_BACKEND_FILES)
    runtime_vendor = _runtime_vendor_status(backend_dir)

    checks: Dict[str, Dict[str, Any]] = {}
    issues: List[Dict[str, Any]] = []

    def record_pass(check_id: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        checks[check_id] = {"status": "pass", "message": message, "details": details or {}}

    def record_issue(
        check_id: str,
        *,
        severity: str,
        code: str,
        message: str,
        hint: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        status = "warn" if severity == "warning" else "fail"
        checks[check_id] = {"status": status, "message": message, "details": details or {}}
        issues.append(_issue(severity=severity, code=code, message=message, hint=hint, details=details))

    if SETTINGS_FILE.exists() and not SETTINGS_HASH_FILE.exists():
        record_issue(
            "settings_integrity",
            severity="warning",
            code="settings-hash-missing",
            message="Settings hash file is missing; config integrity cannot be verified.",
            hint="Re-save settings once to refresh the integrity hash.",
            details={"settings_file": str(SETTINGS_FILE), "hash_file": str(SETTINGS_HASH_FILE)},
        )
    else:
        record_pass("settings_integrity", "Settings integrity metadata is present.")

    if output_writable:
        record_pass("output_dir", "Output directory is writable.", {"output_dir": str(output_dir)})
    else:
        record_issue(
            "output_dir",
            severity="fatal",
            code="output-dir-not-writable",
            message="Output directory is not writable.",
            hint="Fix the output path or permissions before packaging a release.",
            details={"output_dir": str(output_dir), "error": output_error},
        )

    if default_model and default_model_ready:
        record_pass("default_model", "Default model is routable.", {"model": default_model})
    else:
        record_issue(
            "default_model",
            severity="fatal",
            code="default-model-unavailable",
            message=f"Default model '{default_model or 'unset'}' is not routable with the current keys/relays.",
            hint="Configure the matching provider key or add a relay endpoint for the default model.",
            details={"model": default_model, "provider_keys": provider_keys},
        )

    if uncovered_nodes:
        record_issue(
            "node_model_coverage",
            severity="fatal",
            code="node-model-coverage",
            message=f"Some node types have no viable configured model: {', '.join(uncovered_nodes)}.",
            hint="Add a fallback model for each node type or configure the missing provider keys.",
            details={"uncovered_nodes": uncovered_nodes, "resolved_node_models": resolved_node_models},
        )
    else:
        record_pass("node_model_coverage", "Every configured node type has at least one viable model.")

    if gpt54_referenced and not gpt54_ready:
        record_issue(
            "gpt54_route",
            severity="warning",
            code="gpt-5.4-route-unavailable",
            message="gpt-5.4 is referenced by the current routing preferences, but no direct or relay route is ready.",
            hint="Configure an OpenAI-compatible route for gpt-5.4 or move it behind a working fallback model.",
            details={"provider_keys": provider_keys, "openai_api_base": provider_bases.get("openai", "")},
        )
    else:
        gpt54_runtime_warning = _gateway_runtime_warning(gpt54_route, label="gpt-5.4")
        if gpt54_runtime_warning:
            record_issue(
                "gpt54_route",
                severity=str(gpt54_runtime_warning.get("severity") or "warning"),
                code=str(gpt54_runtime_warning.get("code") or "gpt-5.4-gateway-unhealthy"),
                message=str(gpt54_runtime_warning.get("message") or "gpt-5.4 runtime route is unhealthy."),
                hint=str(gpt54_runtime_warning.get("hint") or ""),
                details=gpt54_runtime_warning.get("details"),
            )
        else:
            gpt54_message = f"gpt-5.4 routing is ready via {_describe_route(gpt54_route)}."
            if gpt54_route.get("preferred_route") == "compatible_gateway" and not gpt54_route.get("relay_candidates"):
                gpt54_message += " No relay endpoint currently matches this model."
            elif gpt54_route.get("relay_candidates"):
                gpt54_message += " Relay fallback is available."
            record_pass("gpt54_route", gpt54_message, gpt54_route)

    if builder_primary_route:
        if builder_primary_route.get("ready"):
            builder_runtime_warning = _gateway_runtime_warning(
                builder_primary_route,
                label=f"Builder primary model '{builder_primary_route.get('model')}'",
            )
            if builder_runtime_warning:
                record_issue(
                    "builder_primary_route",
                    severity=str(builder_runtime_warning.get("severity") or "warning"),
                    code="builder-primary-gateway-unhealthy",
                    message=str(builder_runtime_warning.get("message") or "Builder primary runtime route is unhealthy."),
                    hint=str(builder_runtime_warning.get("hint") or ""),
                    details=builder_runtime_warning.get("details"),
                )
            else:
                builder_route_message = (
                    f"Builder primary route is '{builder_primary_route.get('model')}' via "
                    f"{_describe_route(builder_primary_route)}."
                )
                if (
                    builder_primary_route.get("preferred_route") == "compatible_gateway"
                    and not builder_primary_route.get("relay_candidates")
                ):
                    builder_route_message += " No matching relay endpoint is configured for the primary builder model."
                elif builder_primary_route.get("relay_candidates"):
                    builder_route_message += " Relay fallback is available for the primary builder model."
                record_pass("builder_primary_route", builder_route_message, builder_primary_route)
        else:
            record_issue(
                "builder_primary_route",
                severity="fatal",
                code="builder-primary-route-unavailable",
                message="Builder has no routable primary model.",
                hint="Configure a working builder model or set a default model with a valid route.",
                details={"builder_requested_chain": builder_requested_chain, "builder_route_chain": builder_route_chain},
            )
    else:
        record_pass("builder_primary_route", "Builder route is inherited from the default model.")

    if (
        builder_primary_route
        and builder_primary_route.get("ready")
        and builder_primary_route.get("preferred_route") == "compatible_gateway"
        and not builder_primary_route.get("relay_candidates")
        and len(builder_viable_chain) < 2
    ):
        record_issue(
            "builder_fallback_chain",
            severity="warning",
            code="builder-gateway-fallback-thin",
            message=(
                "Builder currently depends on a single compatible-gateway route without a second model fallback."
            ),
            hint="Add a second builder model such as kimi-coding or configure a matching relay endpoint for gpt-5.4.",
            details={
                "builder_requested_chain": builder_requested_chain,
                "builder_viable_chain": builder_viable_chain,
                "builder_primary_route": builder_primary_route,
            },
        )
    else:
        record_pass(
            "builder_fallback_chain",
            "Builder fallback coverage looks healthy.",
            {
                "builder_requested_chain": builder_requested_chain,
                "builder_viable_chain": builder_viable_chain,
                "builder_primary_route": builder_primary_route,
            },
        )

    if image_available:
        if image_workflow and image_workflow_exists:
            record_pass(
                "image_pipeline",
                "Image pipeline is configured.",
                {
                    "comfyui_url": str(image_config.get("comfyui_url", "") or "").strip(),
                    "workflow_template": image_workflow,
                    "service": image_service,
                },
            )
        else:
            record_issue(
                "image_pipeline",
                severity="warning",
                code="image-workflow-missing",
                message="Image pipeline is enabled but the workflow template file is missing.",
                hint="Fix the ComfyUI workflow template path before relying on image generation in release builds.",
                details={"workflow_template": image_workflow, "service": image_service},
            )
    else:
        missing_parts: List[str] = []
        if not str(image_config.get("comfyui_url", "") or "").strip():
            missing_parts.append("comfyui_url")
        if not image_workflow:
            missing_parts.append("workflow_template")
        record_issue(
            "image_pipeline",
            severity="warning",
            code="image-pipeline-plan-only",
            message="Image generation backend is not fully configured; asset nodes can appear, but real image output will fall back to plan-only behavior.",
            hint="Configure both ComfyUI URL and workflow template if the release depends on generated image assets.",
            details={"missing": missing_parts, "service": image_service},
        )
    if image_service.get("configured") and image_service.get("reachable") is False:
        issues.append(_issue(
            severity="warning",
            code="image-backend-unreachable",
            message="Configured local image backend is not reachable.",
            hint="Start ComfyUI or update the configured URL.",
            details=image_service,
        ))

    if bool(playwright.get("available")):
        record_pass("playwright_runtime", "Playwright runtime is available.")
    else:
        record_issue(
            "playwright_runtime",
            severity="warning",
            code="playwright-unavailable",
            message="Playwright runtime is unavailable; deep browser validation will be degraded.",
            hint=str(playwright.get("reason") or "Install/fix Playwright before release validation."),
            details=playwright,
        )

    if browser_use_enabled:
        if browser_use_python and browser_use_python.exists():
            record_pass("browser_use_python", "Configured browser-use Python interpreter exists.", {"path": str(browser_use_python)})
        else:
            record_issue(
                "browser_use_python",
                severity="warning",
                code="browser-use-python-missing",
                message="Browser-use is enabled but the configured Python interpreter is missing.",
                hint="Fix browser_use_python or disable browser-use before release.",
                details={"path": str(browser_use_python) if browser_use_python else ""},
            )
    else:
        record_pass("browser_use_python", "Browser-use is disabled.")

    if runtime_vendor["ok"]:
        record_pass("runtime_vendor", "Bundled runtime vendor assets are present.", runtime_vendor)
    else:
        record_issue(
            "runtime_vendor",
            severity="fatal",
            code="runtime-vendor-missing",
            message="Required runtime vendor assets are missing from backend/runtime_vendor.",
            hint="Restore the missing bundled vendor files before packaging.",
            details=runtime_vendor,
        )

    if not source_root:
        record_pass(
            "local_app_sync",
            "Source workspace is not available in the current packaged runtime; local mirror drift check skipped.",
            local_app_sync,
        )
    elif local_app_sync["status"] == "pass":
        record_pass("local_app_sync", "Project-local Evermind.app backend mirror matches source.", local_app_sync)
    elif local_app and local_app.exists():
        record_issue(
            "local_app_sync",
            severity="fatal",
            code="local-app-backend-drift",
            message="Project-local Evermind.app backend mirror drifted from source.",
            hint="Run `npm --prefix electron run sync:local-app`.",
            details=local_app_sync,
        )
    else:
        record_issue(
            "local_app_sync",
            severity="warning",
            code="local-app-missing",
            message="Project-local Evermind.app is missing, so local packaged parity cannot be verified.",
            hint="Build/sync the local app bundle before release.",
            details=local_app_sync,
        )

    if current_frontend_bundle:
        if current_frontend_bundle.exists():
            record_pass("current_frontend_bundle", "Current runtime frontend bundle is present.", {"path": str(current_frontend_bundle)})
        else:
            record_issue(
                "current_frontend_bundle",
                severity="fatal",
                code="current-frontend-bundle-missing",
                message="Current runtime frontend bundle is missing.",
                hint="Rebuild or repackage the app so the standalone frontend bundle ships with the runtime.",
                details={"path": _path_text(current_frontend_bundle)},
            )
    else:
        record_pass("current_frontend_bundle", "Current runtime is source-backed; standalone frontend bundle check skipped.")

    if dist_frontend_bundle and dist_frontend_bundle.exists():
        record_pass("dist_frontend_bundle", "Dist app frontend bundle is present.", {"path": str(dist_frontend_bundle)})
    elif not dist_app:
        record_pass(
            "dist_frontend_bundle",
            "Packaged dist app path is not available from the current runtime; dist bundle drift check skipped.",
            {"path": _path_text(dist_frontend_bundle)},
        )
    else:
        record_issue(
            "dist_frontend_bundle",
            severity="fatal",
            code="dist-frontend-bundle-missing",
            message="Packaged dist app frontend bundle is missing.",
            hint="Run `npm --prefix electron run pack` to regenerate the packaged frontend bundle.",
            details={"path": _path_text(dist_frontend_bundle)},
        )

    if desktop_frontend_bundle and desktop_frontend_bundle.exists():
        record_pass("desktop_frontend_bundle", "Desktop app frontend bundle is present.", {"path": str(desktop_frontend_bundle)})
    else:
        record_issue(
            "desktop_frontend_bundle",
            severity="warning",
            code="desktop-frontend-bundle-missing",
            message="Desktop Evermind.app frontend bundle is missing.",
            hint="Run `npm --prefix electron run sync:desktop` after packaging.",
            details={"path": _path_text(desktop_frontend_bundle)},
        )

    if not desktop_sync_source:
        record_pass(
            "desktop_sync",
            "No usable packaged source app is available from the current runtime; desktop parity check skipped.",
            desktop_sync,
        )
    elif desktop_sync["status"] == "pass":
        record_pass(
            "desktop_sync",
            "Desktop Evermind.app backend resources match the freshest packaged source app.",
            {
                **desktop_sync,
                "source_app": _path_text(desktop_sync_source),
            },
        )
    elif desktop_sync_source.exists() and desktop_app and desktop_app.exists():
        record_issue(
            "desktop_sync",
            severity="fatal",
            code="desktop-app-backend-drift",
            message="Desktop Evermind.app drifted from the freshest packaged source app.",
            hint="Run `npm --prefix electron run sync:desktop`.",
            details={
                **desktop_sync,
                "source_app": _path_text(desktop_sync_source),
            },
        )
    else:
        record_issue(
            "desktop_sync",
            severity="warning",
            code="desktop-sync-not-verifiable",
            message="Desktop app parity could not be verified because the source app or Desktop app is missing.",
            hint="Package the app, then sync it to Desktop before release.",
            details={
                **desktop_sync,
                "source_app": _path_text(desktop_sync_source),
            },
        )

    if enabled_relays:
        relay_message = f"{len(enabled_relays)} relay endpoint(s) enabled."
        if tls_warning_relays:
            record_issue(
                "relay_config",
                severity="warning",
                code="relay-non-https",
                message="Some relay endpoints use non-HTTPS URLs.",
                hint="Switch relay endpoints to HTTPS before release.",
                details={"relays": tls_warning_relays},
            )
        else:
            record_pass("relay_config", relay_message, {"relay_count": len(enabled_relays)})
    else:
        record_pass("relay_config", "No relay endpoints configured.")

    fatal_count = sum(1 for issue in issues if issue["severity"] == "fatal")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
    pass_count = sum(1 for item in checks.values() if item.get("status") == "pass")
    status = "fail" if fatal_count else ("warn" if warning_count else "ok")

    return {
        "status": status,
        "ready": fatal_count == 0,
        "checked_at": int(time.time()),
        "summary": {
            "fatal": fatal_count,
            "warning": warning_count,
            "passed": pass_count,
            "total_checks": len(checks),
        },
        "issues": issues,
        "checks": checks,
        "paths": {
            "backend_dir": str(backend_dir),
            "source_root": _path_text(source_root),
            "current_app": _path_text(paths["current_app"]),
            "local_app": _path_text(local_app),
            "dist_app": _path_text(dist_app),
            "desktop_app": _path_text(desktop_app),
            "settings_file": str(SETTINGS_FILE),
        },
        "models": {
            "default_model": default_model,
            "default_model_ready": default_model_ready,
            "gpt_5_4_ready": gpt54_ready,
            "gpt_5_4_referenced": gpt54_referenced,
            "gpt_5_4_route": gpt54_route,
            "provider_keys": provider_keys,
            "provider_bases": provider_bases,
            "uncovered_nodes": uncovered_nodes,
            "resolved_node_models": resolved_node_models,
            "builder_requested_chain": builder_requested_chain,
            "builder_viable_chain": builder_viable_chain,
            "builder_primary_route": builder_primary_route,
        },
        "artifacts": {
            "runtime_vendor": runtime_vendor,
            "local_app_sync": local_app_sync,
            "desktop_sync": desktop_sync,
            "desktop_sync_source": _path_text(desktop_sync_source),
            "current_frontend_bundle": {
                "path": _path_text(current_frontend_bundle),
                "exists": bool(current_frontend_bundle and current_frontend_bundle.exists()),
            },
            "local_app_frontend_bundle": {
                "path": _path_text(local_app_frontend_bundle),
                "exists": bool(local_app_frontend_bundle and local_app_frontend_bundle.exists()),
            },
            "dist_frontend_bundle": {
                "path": _path_text(dist_frontend_bundle),
                "exists": bool(dist_frontend_bundle and dist_frontend_bundle.exists()),
            },
            "desktop_frontend_bundle": {
                "path": _path_text(desktop_frontend_bundle),
                "exists": bool(desktop_frontend_bundle and desktop_frontend_bundle.exists()),
            },
        },
        "config": {
            "output_dir": str(output_dir),
            "browser_use_enabled": browser_use_enabled,
            "browser_use_python": str(browser_use_python) if browser_use_python else "",
            "relay_count": len(relay_entries),
            "enabled_relay_count": len(enabled_relays),
            "observed_relay_count": runtime_events.get("relay_count"),
            "image_generation_available": image_available,
            "image_generation": {
                "comfyui_url": str(image_config.get("comfyui_url", "") or "").strip(),
                "workflow_template": image_workflow,
                "workflow_exists": image_workflow_exists,
                "service": image_service,
            },
        },
        "runtime_log": runtime_events,
    }


def format_release_doctor_report(report: Dict[str, Any]) -> str:
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    lines = [
        f"Release Doctor: {str(report.get('status', 'unknown')).upper()}",
        (
            f"fatal={int(summary.get('fatal', 0) or 0)} "
            f"warning={int(summary.get('warning', 0) or 0)} "
            f"passed={int(summary.get('passed', 0) or 0)}/"
            f"{int(summary.get('total_checks', 0) or 0)}"
        ),
    ]

    models = report.get("models", {}) if isinstance(report.get("models"), dict) else {}
    lines.append(
        "Models: "
        f"default={models.get('default_model', '-') or '-'} "
        f"default_ready={bool(models.get('default_model_ready'))} "
        f"gpt5.4_ready={bool(models.get('gpt_5_4_ready'))}"
    )
    builder_primary = models.get("builder_primary_route", {}) if isinstance(models.get("builder_primary_route"), dict) else {}
    if builder_primary:
        lines.append(
            "Builder: "
            f"primary={builder_primary.get('model', '-') or '-'} "
            f"route={_describe_route(builder_primary)} "
            f"fallbacks={len(models.get('builder_viable_chain', []) or [])}"
        )
    gpt54_health = models.get("gpt_5_4_route", {}) if isinstance(models.get("gpt_5_4_route"), dict) else {}
    gateway_health = gpt54_health.get("gateway_health") if isinstance(gpt54_health.get("gateway_health"), dict) else {}
    if gateway_health:
        lines.append(
            "Gateway: "
            f"host={gateway_health.get('host', '-') or '-'} "
            f"status={gateway_health.get('status', '-') or '-'} "
            f"remaining={int(gateway_health.get('remaining_sec', 0) or 0)}s"
        )

    config = report.get("config", {}) if isinstance(report.get("config"), dict) else {}
    lines.append(
        "Config: "
        f"relays={int(config.get('enabled_relay_count', 0) or 0)} "
        f"observed_relays={config.get('observed_relay_count', '-') if config.get('observed_relay_count') is not None else '-'} "
        f"image_generation={bool(config.get('image_generation_available'))} "
        f"browser_use={bool(config.get('browser_use_enabled'))}"
    )

    issues = report.get("issues", []) if isinstance(report.get("issues"), list) else []
    if issues:
        lines.append("Issues:")
        for issue in issues[:12]:
            severity = str(issue.get("severity", "warning")).upper()
            code = str(issue.get("code", "") or "").strip()
            message = str(issue.get("message", "") or "").strip()
            hint = str(issue.get("hint", "") or "").strip()
            line = f"- [{severity}] {code}: {message}" if code else f"- [{severity}] {message}"
            if hint:
                line += f" Hint: {hint}"
            lines.append(line)
    else:
        lines.append("Issues: none")
    return "\n".join(lines)
