"""
Evermind Backend — WebSocket Server
FastAPI + WebSocket server that bridges the frontend UI with the execution engine.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Set

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

app = FastAPI(title="Evermind Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state
connected_clients: Set[WebSocket] = set()


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
    return {"status": "ok", "service": "Evermind Backend", "version": "2.0.0"}


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
    mgr = get_relay_manager()
    ep = mgr.add(
        name=data.get("name", "Unnamed Relay"),
        base_url=data.get("base_url", ""),
        api_key=data.get("api_key", ""),
        models=data.get("models", []),
        headers=data.get("headers", {}),
    )
    return {"success": True, "endpoint": ep.to_dict()}


@app.get("/api/relay/list")
async def relay_list():
    """List all configured relay endpoints."""
    mgr = get_relay_manager()
    return {"relays": mgr.list(), "total": len(mgr.list())}


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
    return {"success": success}


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
    model = data.get("model", "gpt-5.4")

    bridge = AIBridge()
    enabled_plugins = node.get("plugins", NODE_DEFAULT_PLUGINS.get(node.get("type", ""), []))
    plugins = [PluginRegistry.get(p) for p in enabled_plugins if PluginRegistry.get(p)]

    result = await bridge.execute(
        node=node, plugins=plugins, input_data=input_text, model=model,
        privacy_settings=data.get("privacy_settings"),
    )
    return result


# ─────────────────────────────────────────────
# WebSocket Handler
# ─────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.add(ws)
    client_id = id(ws)
    logger.info(f"Client {client_id} connected. Total: {len(connected_clients)}")

    # Build config from env
    config = {
        "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
        "deepseek_api_key": os.getenv("DEEPSEEK_API_KEY", ""),
        "kimi_api_key": os.getenv("KIMI_API_KEY", ""),
        "qwen_api_key": os.getenv("QWEN_API_KEY", ""),
        "workspace": os.getenv("WORKSPACE", str(Path.home() / "Desktop")),
        "output_dir": os.getenv("OUTPUT_DIR", "/tmp/evermind_output"),
        "max_timeout": int(os.getenv("SHELL_TIMEOUT", "30")),
        "allowed_dirs": os.getenv("ALLOWED_DIRS", "/tmp").split(","),
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
        "version": "2.0.0"
    })

    try:
        while True:
            # Receive message from frontend
            raw = await ws.receive_text()
            msg = json.loads(raw)
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
                    val = new_config.get(config_key, "")
                    if val:
                        config[config_key] = val
                        os.environ[env_key] = val  # LiteLLM reads from env
                if new_config.get("workspace"):
                    config["workspace"] = new_config["workspace"]
                # Apply privacy settings
                if new_config.get("privacy"):
                    from privacy import update_masker_settings
                    update_masker_settings(new_config["privacy"])
                ai_bridge.config = config
                ai_bridge._setup_litellm()  # Re-init LiteLLM with new keys
                logger.info(f"Config updated: {[k for k, v in new_config.items() if v and 'key' in k]}")
                await ws.send_json({"type": "config_updated"})

            elif msg_type == "execute_workflow":
                # Full workflow execution
                nodes = msg.get("nodes", [])
                edges = msg.get("edges", [])
                asyncio.create_task(executor.execute_workflow(nodes, edges))

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
                router = next((n for n in nodes if n["type"] == "router"), None)
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
                asyncio.create_task(orchestrator.run(goal, model))

            elif msg_type == "stop":
                executor.stop()
                orchestrator.stop()
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
        logger.error(f"Client {client_id} error: {e}")
    finally:
        connected_clients.discard(ws)
        executor.stop()
        logger.info(f"Client {client_id} cleaned up. Total: {len(connected_clients)}")


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8765"))
    debug = os.getenv("DEBUG", "true").lower() == "true"

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
