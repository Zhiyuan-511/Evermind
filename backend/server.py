"""
Evermind Backend — WebSocket Server
FastAPI + WebSocket server that bridges the frontend UI with the execution engine.
"""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, Set

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# Add current dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from plugins.base import PluginRegistry, NODE_DEFAULT_PLUGINS
from plugins.implementations import register_all as register_plugins
from ai_bridge import AIBridge
from executor import NodeExecutor
from orchestrator import Orchestrator

# ─────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("evermind.server")

# Register all plugins
register_plugins()

app = FastAPI(title="Evermind Backend", version="2.1.0")

# ─────────────────────────────────────────────
# Security — CORS restricted to local origins only
# ─────────────────────────────────────────────
_ALLOWED_ORIGINS = [
    "http://localhost",
    "http://127.0.0.1",
    "https://localhost",
    "https://127.0.0.1",
]
# Expand with common dev ports
for _port in (3000, 3001, 5173, 8000, 8080, 8765):
    for _origin in ("http://localhost", "http://127.0.0.1"):
        _ALLOWED_ORIGINS.append(f"{_origin}:{_port}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ─────────────────────────────────────────────
# Security — Response headers middleware
# ─────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response

app.add_middleware(SecurityHeadersMiddleware)


# ─────────────────────────────────────────────
# Security — Request body size limit (5 MB)
# ─────────────────────────────────────────────
MAX_REQUEST_BODY_BYTES = 5 * 1024 * 1024  # 5 MB


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error": "Request body too large", "max_bytes": MAX_REQUEST_BODY_BYTES},
            )
        return await call_next(request)

app.add_middleware(RequestSizeLimitMiddleware)


# ─────────────────────────────────────────────
# Security — Sanitize sensitive data from strings
# ─────────────────────────────────────────────
_API_KEY_RE = re.compile(r"(?:sk|key|token|api[_-]?key|Bearer)[-_\s]?[a-zA-Z0-9._\-]{8,}", re.IGNORECASE)


def _sanitize_error(msg: str) -> str:
    """Remove potential API keys / tokens from error messages before logging or returning."""
    return _API_KEY_RE.sub("[REDACTED]", msg) if msg else msg


# Global state
MAX_WS_CONNECTIONS = 10
connected_clients: Set[WebSocket] = set()
_active_tasks: Dict[int, list] = {}  # client_id → [asyncio.Task, ...]


# Path to the original frontend HTML (parent directory)
FRONTEND_HTML = Path(__file__).parent.parent / "evermind_godmode_final.html"


@app.get("/")
async def root():
    """Serve the original Evermind frontend with all features."""
    if FRONTEND_HTML.exists():
        return FileResponse(str(FRONTEND_HTML), media_type="text/html")
    return {"error": "Frontend HTML not found", "expected_path": str(FRONTEND_HTML)}


@app.get("/api/status")
async def api_status():
    return {"status": "ok", "service": "Evermind Backend", "version": "2.1.0"}


@app.get("/api/models")
async def list_models():
    """List all available AI models."""
    bridge = AIBridge()
    return {"models": bridge.get_available_models()}


@app.get("/api/plugins")
async def list_plugins():
    """List all available plugins with their metadata."""
    plugins = PluginRegistry.get_all()
    return {
        "plugins": [
            {
                "name": p.name,
                "display_name": p.display_name,
                "description": p.description,
                "icon": p.icon,
                "security_level": p.security_level.value,
                "parameters": p._get_parameters_schema()
            }
            for p in plugins.values()
        ]
    }


from proxy_relay import get_relay_manager
from privacy import get_masker, update_masker_settings, BUILTIN_PATTERNS


@app.get("/api/plugins/defaults")
async def plugin_defaults():
    """Get default plugin assignments for each node type."""
    return {"defaults": NODE_DEFAULT_PLUGINS}


@app.get("/api/health")
async def health():
    relay_mgr = get_relay_manager()
    masker = get_masker()
    return {
        "status": "healthy",
        "plugins_loaded": len(PluginRegistry.get_all()),
        "clients_connected": len(connected_clients),
        "relay_endpoints": len(relay_mgr.list()),
        "privacy_enabled": masker.enabled,
        "privacy_patterns": len(masker._patterns),
    }


# ─────────────────────────────────────────────
# Relay / Proxy API Endpoints (中转 API)
# ─────────────────────────────────────────────
@app.post("/api/relay/add")
async def relay_add(data: Dict = Body(...)):
    """Register a new relay endpoint."""
    if not data:
        return {"error": "No data provided"}
    base_url = (data.get("base_url") or "").strip()
    if not base_url:
        return {"error": "base_url is required"}
    if not base_url.startswith(("http://", "https://")):
        return {"error": "base_url must start with http:// or https://"}
    mgr = get_relay_manager()
    ep = mgr.add(
        name=data.get("name", "Unnamed Relay"),
        base_url=base_url,
        api_key=data.get("api_key", ""),
        models=data.get("models", []),
        headers=data.get("headers", {}),
    )
    settings = load_settings()
    _persist_relays(settings)
    return {"success": True, "endpoint": ep.to_dict(), "relay_count": len(mgr.list())}


@app.get("/api/relay/list")
async def relay_list():
    """List all configured relay endpoints."""
    mgr = get_relay_manager()
    relays = mgr.list()
    return {"relays": relays, "total": len(relays)}


@app.post("/api/relay/test/{endpoint_id}")
async def relay_test(endpoint_id: str):
    """Test connectivity to a relay endpoint."""
    mgr = get_relay_manager()
    result = await mgr.test(endpoint_id)
    return result


@app.delete("/api/relay/{endpoint_id}")
async def relay_remove(endpoint_id: str):
    """Remove a relay endpoint."""
    mgr = get_relay_manager()
    success = mgr.remove(endpoint_id)
    if success:
        settings = load_settings()
        _persist_relays(settings)
    return {"success": success, "relay_count": len(mgr.list())}


# ─────────────────────────────────────────────
# Privacy / Desensitization Endpoints (脱敏处理)
# ─────────────────────────────────────────────
@app.get("/api/privacy/patterns")
async def privacy_patterns():
    """Get available masking patterns."""
    masker = get_masker()
    return {
        "enabled": masker.enabled,
        "patterns": masker.get_patterns_info(),
        "builtin_count": len(BUILTIN_PATTERNS),
    }


@app.post("/api/privacy/test")
async def privacy_test(data: Dict = Body(...)):
    """Test masking on sample text."""
    if not data or "text" not in data:
        return {"error": "Provide 'text' field"}
    masker = get_masker()
    return masker.test_mask(data["text"])


@app.post("/api/privacy/settings")
async def privacy_update(data: Dict = Body(...)):
    """Update privacy/masking settings."""
    if not data:
        return {"error": "No settings provided"}
    masker = update_masker_settings(data)
    return {
        "success": True,
        "enabled": masker.enabled,
        "patterns_count": len(masker._patterns),
    }


# ─────────────────────────────────────────────
# Execute Endpoint (single node test)
# ─────────────────────────────────────────────
@app.post("/api/execute")
async def execute_node(data: Dict = Body(...)):
    """Execute a single node via REST API (for testing)."""
    if not data:
        return {"error": "No data provided"}

    node = data.get("node", {"type": "builder", "name": "Test"})
    input_text = data.get("input", "")
    model = data.get("model") or node.get("data", {}).get("model") or node.get("model", "gpt-5.4")

    workspace = os.getenv("WORKSPACE", str(Path.home() / "Desktop"))
    output_dir = os.getenv("OUTPUT_DIR", "/tmp/evermind_output")
    allowed_dirs_env = os.getenv("ALLOWED_DIRS", "")
    allowed_dirs = [p for p in allowed_dirs_env.split(",") if p] if allowed_dirs_env else [workspace, output_dir, "/tmp"]

    bridge = AIBridge(config={
        "workspace": workspace,
        "output_dir": output_dir,
        "allowed_dirs": allowed_dirs,
        "max_timeout": int(os.getenv("SHELL_TIMEOUT", "30")),
    })
    node_type = node.get("data", {}).get("nodeType", node.get("type", ""))
    enabled_plugins = node.get("plugins") or node.get("data", {}).get("plugins") or NODE_DEFAULT_PLUGINS.get(node_type, [])
    plugins = [PluginRegistry.get(p) for p in enabled_plugins if PluginRegistry.get(p)]

    result = await bridge.execute(
        node=node, plugins=plugins, input_data=input_text, model=model,
        privacy_settings=data.get("privacy_settings"),
    )
    return result


# ─────────────────────────────────────────────
# Settings Persistence Endpoints
# ─────────────────────────────────────────────
from settings import load_settings, save_settings, apply_api_keys, validate_api_key, get_usage_tracker, deep_merge_dicts

def _merge_settings(base: Dict, patch: Dict) -> Dict:
    """Deep merge for partial settings updates from the frontend."""
    return deep_merge_dicts(base, patch or {})


def _persist_relays(settings: Dict):
    settings["relay_endpoints"] = get_relay_manager().export()
    save_settings(settings)


# Auto-load saved settings on startup
_saved_settings = load_settings()
_applied = apply_api_keys(_saved_settings)
get_relay_manager().load(_saved_settings.get("relay_endpoints", []))
logger.info(f"Auto-loaded settings: {_applied} API keys applied, {len(get_relay_manager().list())} relays restored")


@app.get("/api/settings")
async def get_settings():
    """Get current saved settings (keys are masked)."""
    settings = load_settings()
    # Mask API keys for security
    masked_keys = {}
    for k, v in settings.get("api_keys", {}).items():
        if v:
            masked_keys[k] = v[:6] + "..." + v[-4:] if len(v) > 10 else "***"
        else:
            masked_keys[k] = ""
    return {
        "api_keys": masked_keys,
        "workspace": settings.get("workspace", ""),
        "default_model": settings.get("default_model", "gpt-5.4"),
        "privacy_enabled": settings.get("privacy", {}).get("enabled", True),
        "relay_endpoints": get_relay_manager().list(),
        "relay_count": len(get_relay_manager().list()),
        "has_keys": {k: bool(v) for k, v in settings.get("api_keys", {}).items()},
    }


@app.post("/api/settings/save")
async def save_user_settings(data: Dict = Body(...)):
    """Save settings to disk and apply API keys."""
    merged = _merge_settings(load_settings(), data or {})
    if "relay_endpoints" not in (data or {}):
        merged["relay_endpoints"] = get_relay_manager().export()

    success = save_settings(merged)
    if success:
        count = apply_api_keys(merged)
        get_relay_manager().load(merged.get("relay_endpoints", []))
        return {"success": True, "keys_applied": count, "relay_count": len(get_relay_manager().list())}
    return {"success": False, "error": "Failed to save"}


@app.post("/api/settings/validate")
async def validate_keys(data: Dict = Body(...)):
    """Validate API keys by making minimal LiteLLM requests."""
    results = {}
    keys = data.get("api_keys", {})
    for provider, key in keys.items():
        if key:
            result = validate_api_key(provider, key)
            results[provider] = result
    return {"results": results}


@app.get("/api/usage")
async def get_usage():
    """Get token usage stats for the current session."""
    tracker = get_usage_tracker()
    return tracker.get_usage()


# ─────────────────────────────────────────────
# WebSocket Handler
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# Graceful Shutdown
# ─────────────────────────────────────────────
@app.on_event("shutdown")
async def shutdown_event():
    """Gracefully close all WebSocket connections and cancel tasks on server shutdown."""
    logger.info("Server shutting down — closing all connections...")
    for client_id, tasks in _active_tasks.items():
        for task in tasks:
            if not task.done():
                task.cancel()
    _active_tasks.clear()
    for ws in list(connected_clients):
        try:
            await ws.close(code=1001, reason="Server shutting down")
        except Exception:
            pass
    connected_clients.clear()
    logger.info("Shutdown complete.")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # ── Connection limit guard ──
    if len(connected_clients) >= MAX_WS_CONNECTIONS:
        await ws.close(code=1013, reason="Maximum connections reached")
        logger.warning(f"WebSocket rejected: connection limit ({MAX_WS_CONNECTIONS}) reached")
        return

    await ws.accept()
    connected_clients.add(ws)
    client_id = id(ws)
    _active_tasks[client_id] = []
    logger.info(f"Client {client_id} connected. Total: {len(connected_clients)}")

    # Build config from env
    workspace = os.getenv("WORKSPACE", str(Path.home() / "Desktop"))
    output_dir = os.getenv("OUTPUT_DIR", "/tmp/evermind_output")
    allowed_dirs_env = os.getenv("ALLOWED_DIRS", "")
    allowed_dirs = [p for p in allowed_dirs_env.split(",") if p] if allowed_dirs_env else [workspace, output_dir, "/tmp"]
    config = {
        "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
        "deepseek_api_key": os.getenv("DEEPSEEK_API_KEY", ""),
        "kimi_api_key": os.getenv("KIMI_API_KEY", ""),
        "qwen_api_key": os.getenv("QWEN_API_KEY", ""),
        "workspace": workspace,
        "output_dir": output_dir,
        "max_timeout": int(os.getenv("SHELL_TIMEOUT", "30")),
        "allowed_dirs": allowed_dirs,
    }

    # Create executor for this client
    ai_bridge = AIBridge(config=config)

    async def send_event(data: Dict):
        """Send real-time event to this client."""
        try:
            await ws.send_json(data)
        except Exception:
            pass

    executor = NodeExecutor(ai_bridge=ai_bridge, on_event=send_event)
    orchestrator = Orchestrator(ai_bridge=ai_bridge, executor=executor, on_event=send_event)

    # Send initial handshake
    await ws.send_json({
        "type": "connected",
        "plugins": list(PluginRegistry.get_all().keys()),
        "defaults": NODE_DEFAULT_PLUGINS,
        "models": ai_bridge.get_available_models(),
        "version": "2.1.0"
    })

    try:
        while True:
            # Receive message from frontend
            raw = await ws.receive_text()

            # ── Guard: message size limit (10 MB) ──
            if len(raw) > 10 * 1024 * 1024:
                await ws.send_json({"type": "error", "error": "Message too large"})
                continue

            # ── Guard: JSON parse safety ──
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as je:
                logger.warning(f"Client {client_id}: invalid JSON — {je}")
                await ws.send_json({"type": "error", "error": "Invalid JSON message"})
                continue

            msg_type = msg.get("type", "")
            logger.info(f"Client {client_id} → {msg_type}")

            if msg_type == "ping":
                await ws.send_json({"type": "pong"})

            elif msg_type == "update_config":
                # Update API keys from frontend settings
                new_config = msg.get("config", {})
                key_map = {
                    "openai_api_key": "OPENAI_API_KEY",
                    "anthropic_api_key": "ANTHROPIC_API_KEY",
                    "gemini_api_key": "GEMINI_API_KEY",
                    "deepseek_api_key": "DEEPSEEK_API_KEY",
                    "kimi_api_key": "KIMI_API_KEY",
                    "qwen_api_key": "QWEN_API_KEY",
                }
                for config_key, env_key in key_map.items():
                    if config_key in new_config:
                        val = new_config.get(config_key, "")
                        config[config_key] = val
                        if val:
                            os.environ[env_key] = val  # LiteLLM reads from env
                        else:
                            os.environ.pop(env_key, None)
                if "workspace" in new_config and new_config.get("workspace"):
                    config["workspace"] = new_config["workspace"]
                if "allowed_dirs" in new_config and isinstance(new_config.get("allowed_dirs"), list):
                    config["allowed_dirs"] = new_config["allowed_dirs"]
                if "max_timeout" in new_config:
                    config["max_timeout"] = int(new_config.get("max_timeout") or config.get("max_timeout", 30))
                # Apply privacy settings
                if new_config.get("privacy"):
                    from privacy import update_masker_settings
                    update_masker_settings(new_config["privacy"])
                ai_bridge.config = config
                ai_bridge._setup_litellm()  # Re-init LiteLLM with new keys
                # Log only count of updated keys — never log key names or values
                key_count = sum(1 for k, v in new_config.items() if v and 'key' in k.lower())
                logger.info(f"Config updated: {key_count} API key(s) refreshed")
                await ws.send_json({"type": "config_updated"})

            elif msg_type == "execute_workflow":
                # Full workflow execution
                nodes = msg.get("nodes", [])
                edges = msg.get("edges", [])
                task = asyncio.create_task(executor.execute_workflow(nodes, edges))
                _active_tasks[client_id].append(task)

            elif msg_type == "execute_node":
                # Single node execution (test / step)
                node = msg.get("node", {})
                input_data = msg.get("input", "")
                result = await executor.execute_single(node, input_data)
                await ws.send_json({
                    "type": "node_result",
                    "node_id": node.get("id"),
                    "result": result
                })

            elif msg_type == "send_task":
                # Task from chat panel → find router → execute
                task_text = msg.get("task", "")
                nodes = msg.get("nodes", [])
                router = next((n for n in nodes if n.get("type") == "router" or n.get("data", {}).get("nodeType") == "router"), None)
                if router:
                    router["_direct_input"] = task_text
                    result = await executor.execute_single(router, task_text)
                    await ws.send_json({
                        "type": "task_result",
                        "router_id": router.get("id"),
                        "result": result
                    })
                else:
                    await ws.send_json({
                        "type": "task_error",
                        "error": "No router node found"
                    })

            elif msg_type == "run_goal":
                # 🧠 Autonomous mode: user sends a goal, system does everything
                goal = msg.get("goal", "")
                model = msg.get("model", "gpt-5.4")
                task = asyncio.create_task(orchestrator.run(goal, model))
                _active_tasks[client_id].append(task)

            elif msg_type == "stop":
                executor.stop()
                orchestrator.stop()
                # Cancel tracked async tasks
                for t in _active_tasks.get(client_id, []):
                    if not t.done():
                        t.cancel()
                _active_tasks[client_id] = []
                await ws.send_json({"type": "workflow_stopped"})

            elif msg_type == "test_plugin":
                # Direct plugin test
                plugin_name = msg.get("plugin", "")
                params = msg.get("params", {})
                plugin = PluginRegistry.get(plugin_name)
                if plugin:
                    result = await plugin.execute(params, context=config)
                    await ws.send_json({
                        "type": "plugin_result",
                        "plugin": plugin_name,
                        "result": result.to_dict()
                    })
                else:
                    await ws.send_json({
                        "type": "plugin_error",
                        "error": f"Plugin '{plugin_name}' not found"
                    })

    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected")
    except Exception as e:
        logger.error(f"Client {client_id} error: {_sanitize_error(str(e))}")
    finally:
        connected_clients.discard(ws)
        # Cancel any remaining tracked tasks
        for t in _active_tasks.pop(client_id, []):
            if not t.done():
                t.cancel()
        executor.stop()
        logger.info(f"Client {client_id} cleaned up. Total: {len(connected_clients)}")


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "127.0.0.1")  # Default: local-only for security
    port = int(os.getenv("PORT", "8765"))
    debug = os.getenv("DEBUG", "false").lower() == "true"  # Default: debug off

    print(f"""
╔══════════════════════════════════════════╗
║     🧠 Evermind Backend Server          ║
║     Frontend: http://{host}:{port}/       ║
║     WebSocket: ws://{host}:{port}/ws      ║
║     REST API:  http://{host}:{port}/api   ║
║     Plugins:   {len(PluginRegistry.get_all())} loaded                ║
╚══════════════════════════════════════════╝
    """)

    uvicorn.run("server:app", host=host, port=port, reload=debug)
