"""
Evermind Backend — WebSocket Server
FastAPI + WebSocket server that bridges the frontend UI with the execution engine.
"""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# Add current dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from plugins.base import (
    PluginRegistry,
    get_default_plugins_for_node,
    get_effective_default_plugins,
    is_builder_browser_enabled,
)
from plugins.implementations import register_all as register_plugins
from ai_bridge import AIBridge
from config_utils import coerce_bool, coerce_int
from executor import NodeExecutor
from orchestrator import Orchestrator
from workflow_templates import get_template, list_templates, template_nodes
from task_store import (
    get_task_store, get_report_store,
    get_run_store, get_node_execution_store, get_artifact_store,
    MAX_NODE_RETRY_COUNT,
)
from preview_validation import (
    build_preview_url_for_file,
    diagnostics_snapshot,
    validate_preview,
)

# ─────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("evermind.server")

LOG_DIR = Path.home() / ".evermind" / "logs"
LOG_FILE = LOG_DIR / "evermind-backend.log"


def _ensure_file_logging():
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        for handler in root.handlers:
            if isinstance(handler, logging.FileHandler):
                base = getattr(handler, "baseFilename", "")
                if base and Path(base).resolve() == LOG_FILE.resolve():
                    return
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        root.addHandler(file_handler)
    except Exception as exc:
        logger.warning(f"Failed to initialize file logging: {exc}")


_ensure_file_logging()

# Register all plugins
register_plugins()

APP_VERSION = "2.1.0"
PROCESS_STARTED_AT = int(time.time())
RUNTIME_ID = f"{os.getpid()}-{PROCESS_STARTED_AT}"

app = FastAPI(title="Evermind Backend", version=APP_VERSION)


def _to_epoch_ms(value: Any, default: int | None = None) -> int:
    fallback = int(time.time() * 1000) if default is None else default
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return fallback
    if ts <= 0:
        return fallback
    if ts < 10_000_000_000:
        ts *= 1000
    return int(ts)


def _to_epoch_seconds(value: Any) -> float:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0.0
    if ts <= 0:
        return 0.0
    if ts >= 10_000_000_000:
        ts /= 1000
    return ts


def _normalize_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        items = value
    elif value in (None, ""):
        items = []
    else:
        items = [value]
    return [str(item) for item in items if str(item).strip()]


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _task_to_api(task: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(task or {})
    result: Dict[str, Any] = {
        "id": str(payload.get("id") or ""),
        "title": str(payload.get("title") or ""),
        "description": str(payload.get("description") or ""),
        "status": str(payload.get("status") or "backlog"),
        "mode": str(payload.get("mode") or "standard"),
        "owner": str(payload.get("owner") or ""),
        "progress": max(0, min(100, coerce_int(payload.get("progress"), 0))),
        "priority": str(payload.get("priority") or "medium"),
        "createdAt": _to_epoch_ms(payload.get("created_at", payload.get("createdAt"))),
        "updatedAt": _to_epoch_ms(payload.get("updated_at", payload.get("updatedAt"))),
        "version": max(0, coerce_int(payload.get("version"), 0)),
        "runIds": _normalize_string_list(payload.get("run_ids", payload.get("runIds"))),
        "relatedFiles": _normalize_string_list(payload.get("related_files", payload.get("relatedFiles"))),
        "latestSummary": str(payload.get("latest_summary", payload.get("latestSummary")) or ""),
        "latestRisk": str(payload.get("latest_risk", payload.get("latestRisk")) or ""),
        "reviewVerdict": str(payload.get("review_verdict", payload.get("reviewVerdict")) or ""),
        "reviewIssues": _normalize_string_list(payload.get("review_issues", payload.get("reviewIssues"))),
        "selfcheckItems": payload.get("selfcheck_items", payload.get("selfcheckItems")) or [],
    }
    if isinstance(payload.get("reports"), list):
        result["reports"] = [_report_to_api(item) for item in payload["reports"] if isinstance(item, dict)]
    return result


def _subtask_to_api(subtask: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(subtask or {})
    result = dict(payload)
    if "output_preview" in result:
        result["outputPreview"] = result.pop("output_preview")
    if "files_created" in result:
        result["filesCreated"] = result.pop("files_created")
    if "work_summary" in result:
        result["workSummary"] = result.pop("work_summary")
    if "duration_seconds" in result:
        result["durationSeconds"] = result.pop("duration_seconds")
    if "started_at" in result:
        result["startedAt"] = _to_epoch_ms(result.pop("started_at"), default=0)
    elif "startedAt" in result:
        result["startedAt"] = _to_epoch_ms(result["startedAt"], default=0)
    if "ended_at" in result:
        result["endedAt"] = _to_epoch_ms(result.pop("ended_at"), default=0)
    elif "endedAt" in result:
        result["endedAt"] = _to_epoch_ms(result["endedAt"], default=0)
    return result


def _report_to_api(report: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(report or {})
    return {
        "id": str(payload.get("id") or ""),
        "taskId": str(payload.get("task_id", payload.get("taskId")) or ""),
        "createdAt": _to_epoch_ms(payload.get("created_at", payload.get("createdAt"))),
        "goal": str(payload.get("goal") or ""),
        "difficulty": str(payload.get("difficulty") or "standard"),
        "success": bool(payload.get("success")),
        "totalSubtasks": max(0, coerce_int(payload.get("total_subtasks", payload.get("totalSubtasks")), 0)),
        "completed": max(0, coerce_int(payload.get("completed"), 0)),
        "failed": max(0, coerce_int(payload.get("failed"), 0)),
        "totalRetries": max(0, coerce_int(payload.get("total_retries", payload.get("totalRetries")), 0)),
        "durationSeconds": max(0.0, _coerce_float(payload.get("duration_seconds", payload.get("durationSeconds")), 0.0)),
        "previewUrl": str(payload.get("preview_url", payload.get("previewUrl")) or ""),
        "subtasks": [
            _subtask_to_api(item)
            for item in (payload.get("subtasks") or [])
            if isinstance(item, dict)
        ],
    }


def _task_from_api(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(data or {})
    result = dict(payload)

    if "createdAt" in payload and "created_at" not in result:
        result["created_at"] = _to_epoch_seconds(payload["createdAt"])
    if "updatedAt" in payload and "updated_at" not in result:
        result["updated_at"] = _to_epoch_seconds(payload["updatedAt"])
    if "runIds" in payload and "run_ids" not in result:
        result["run_ids"] = payload["runIds"]
    if "relatedFiles" in payload and "related_files" not in result:
        result["related_files"] = payload["relatedFiles"]
    if "latestSummary" in payload and "latest_summary" not in result:
        result["latest_summary"] = payload["latestSummary"]
    if "latestRisk" in payload and "latest_risk" not in result:
        result["latest_risk"] = payload["latestRisk"]
    if "reviewVerdict" in payload and "review_verdict" not in result:
        result["review_verdict"] = payload["reviewVerdict"]
    if "reviewIssues" in payload and "review_issues" not in result:
        result["review_issues"] = payload["reviewIssues"]
    if "selfcheckItems" in payload and "selfcheck_items" not in result:
        result["selfcheck_items"] = payload["selfcheckItems"]
    return result


def _subtask_from_api(subtask: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(subtask or {})
    result = dict(payload)
    if "outputPreview" in payload and "output_preview" not in result:
        result["output_preview"] = payload["outputPreview"]
    if "filesCreated" in payload and "files_created" not in result:
        result["files_created"] = payload["filesCreated"]
    if "workSummary" in payload and "work_summary" not in result:
        result["work_summary"] = payload["workSummary"]
    if "durationSeconds" in payload and "duration_seconds" not in result:
        result["duration_seconds"] = payload["durationSeconds"]
    if "startedAt" in payload and "started_at" not in result:
        result["started_at"] = _to_epoch_seconds(payload["startedAt"])
    if "endedAt" in payload and "ended_at" not in result:
        result["ended_at"] = _to_epoch_seconds(payload["endedAt"])
    return result


def _report_from_api(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(data or {})
    result = dict(payload)

    if "taskId" in payload and "task_id" not in result:
        result["task_id"] = payload["taskId"]
    if "createdAt" in payload and "created_at" not in result:
        result["created_at"] = _to_epoch_seconds(payload["createdAt"])
    if "totalSubtasks" in payload and "total_subtasks" not in result:
        result["total_subtasks"] = payload["totalSubtasks"]
    if "totalRetries" in payload and "total_retries" not in result:
        result["total_retries"] = payload["totalRetries"]
    if "durationSeconds" in payload and "duration_seconds" not in result:
        result["duration_seconds"] = payload["durationSeconds"]
    if "previewUrl" in payload and "preview_url" not in result:
        result["preview_url"] = payload["previewUrl"]
    if isinstance(payload.get("subtasks"), list):
        result["subtasks"] = [_subtask_from_api(item) for item in payload["subtasks"] if isinstance(item, dict)]
    return result

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
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ─────────────────────────────────────────────
# Security — Response headers middleware
# ─────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # /preview/ routes are embedded in our own app's iframe.
        # The frontend (port 3000) and backend (port 8765) are different
        # origins, so SAMEORIGIN would still block the iframe.
        # We skip X-Frame-Options entirely for preview routes only.
        path = request.url.path
        if path.startswith("/preview"):
            # Allow iframe embedding & brief caching for faster preview loads
            response.headers["Cache-Control"] = "public, max-age=5"
        else:
            response.headers["X-Frame-Options"] = "DENY"
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

# Output directory for generated files (shared with orchestrator)
# MUST match the path in orchestrator.py and agent prompts
_FIXED_OUTPUT = "/tmp/evermind_output"
OUTPUT_DIR = Path(os.getenv("EVERMIND_OUTPUT_DIR", _FIXED_OUTPUT))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
# Also ensure /tmp/evermind_output exists (macOS may resolve /tmp differently)
Path(_FIXED_OUTPUT).mkdir(parents=True, exist_ok=True)

# Path to the original frontend HTML (parent directory)
FRONTEND_HTML = Path(__file__).parent.parent / "evermind_godmode_final.html"


async def _broadcast_ws_event(data: Dict[str, Any], *, exclude_ws: Optional[WebSocket] = None):
    """Broadcast an event to connected WS clients, optionally excluding the sender."""
    stale_clients = []
    for client_ws in list(connected_clients):
        if exclude_ws is not None and client_ws is exclude_ws:
            continue
        try:
            await client_ws.send_json(data)
        except Exception:
            stale_clients.append(client_ws)
    for client_ws in stale_clients:
        connected_clients.discard(client_ws)


def _sync_run_active_nodes(
    run_id: str,
    *,
    add: Any = None,
    remove: Any = None,
    current_node_execution_id: Optional[str] = None,
    clear: bool = False,
) -> Optional[Dict[str, Any]]:
    """Keep run.current_node_execution_id and active_node_execution_ids consistent."""
    if not run_id:
        return None

    rs = get_run_store()
    run = rs.get_run(run_id)
    if not run:
        return None

    existing_active = _normalize_string_list(run.get("active_node_execution_ids", []))
    active = [] if clear else list(existing_active)

    for node_id in _normalize_string_list(add):
        if node_id not in active:
            active.append(node_id)

    remove_set = set(_normalize_string_list(remove))
    if remove_set:
        active = [node_id for node_id in active if node_id not in remove_set]

    existing_current = str(run.get("current_node_execution_id", "") or "")
    next_current = existing_current if current_node_execution_id is None else str(current_node_execution_id or "")
    if next_current and next_current not in active:
        next_current = active[-1] if active else ""
    elif not next_current and current_node_execution_id is None and active:
        next_current = active[-1]

    if active == existing_active and next_current == existing_current:
        return run

    return rs.update_run(run_id, {
        "current_node_execution_id": next_current,
        "active_node_execution_ids": active,
    })


def _transition_node_if_needed(node_id: str, new_status: str) -> bool:
    if not node_id or not new_status:
        return False
    nes = get_node_execution_store()
    node = nes.get_node_execution(node_id)
    if not node:
        return False
    run_id = str(node.get("run_id", "") or "")
    if node.get("status") == new_status:
        if run_id:
            if new_status == "running":
                _sync_run_active_nodes(run_id, add=[node_id], current_node_execution_id=node_id)
            else:
                _sync_run_active_nodes(run_id, remove=[node_id])
        return True
    result = nes.transition_node(node_id, new_status)
    success = bool(result.get("success"))
    if success and run_id:
        if new_status == "running":
            _sync_run_active_nodes(run_id, add=[node_id], current_node_execution_id=node_id)
        else:
            _sync_run_active_nodes(run_id, remove=[node_id])
    return success


def _transition_run_if_needed(run_id: str, new_status: str) -> bool:
    if not run_id or not new_status:
        return False
    rs = get_run_store()
    run = rs.get_run(run_id)
    if not run:
        return False
    if run.get("status") == new_status:
        if new_status != "running":
            _sync_run_active_nodes(run_id, clear=True, current_node_execution_id="")
        return True
    result = rs.transition_run(run_id, new_status)
    success = bool(result.get("success"))
    if success and new_status != "running":
        _sync_run_active_nodes(run_id, clear=True, current_node_execution_id="")
    return success


def _is_openclaw_run(run_id: str) -> bool:
    """Check whether a run is dispatched to OpenClaw."""
    run = get_run_store().get_run(run_id)
    return bool(run and run.get("runtime") == "openclaw")


def _auto_chain_next_node(run_id: str) -> "list[str] | str | None":
    """P3-B: DAG-aware node chaining.

    Resolves depends_on_keys to determine which queued nodes are ready.

    Returns:
        list[str] of NE IDs whose dependencies are all satisfied (parallel dispatch), or
        "__ALL_DONE__" if every node is terminal (passed/skipped), or
        None if nothing to do (still running / blocked).
    """
    nes = get_node_execution_store().list_node_executions(run_id=run_id)
    if not nes:
        return None

    DONE_STATUSES = {"passed", "skipped"}
    TERMINAL_STATUSES = {"passed", "skipped", "failed", "cancelled"}

    # Build key→status map for dependency resolution
    key_status: Dict[str, str] = {}
    for ne in nes:
        k = ne.get("node_key", "")
        if k:
            key_status[k] = ne.get("status", "queued")

    ready: list[str] = []
    for ne in nes:
        if ne.get("status") != "queued":
            continue
        deps = ne.get("depends_on_keys") or []
        # All dependencies must be done (passed/skipped)
        if all(key_status.get(d) in DONE_STATUSES for d in deps):
            ready.append(ne["id"])

    if ready:
        return ready

    # No queued nodes ready — check if all are terminal
    statuses = {ne.get("status") for ne in nes}
    if statuses <= TERMINAL_STATUSES:
        if statuses <= DONE_STATUSES:
            return "__ALL_DONE__"
        return None  # Some failed — don't auto-complete

    # Some still running — wait
    return None


def _cancel_run_cascade(run_id: str) -> Dict[str, Any]:
    """Cancel a run, cascade to non-terminal nodes, and project the task if present."""
    if not run_id:
        return {"cancelled": False, "cancelled_nodes": 0, "run": None, "task": None}

    cancelled = _transition_run_if_needed(run_id, "cancelled")
    if not cancelled:
        run_snapshot = get_run_store().get_run(run_id)
        task_snapshot = None
        if run_snapshot and run_snapshot.get("task_id"):
            task_snapshot = get_task_store().get_task(run_snapshot["task_id"])
        return {
            "cancelled": False,
            "cancelled_nodes": 0,
            "run": run_snapshot,
            "task": task_snapshot,
        }

    cancelled_nodes = get_node_execution_store().cancel_run_nodes(run_id)
    run_snapshot = get_run_store().get_run(run_id)
    task_snapshot = None
    task_id = run_snapshot.get("task_id", "") if run_snapshot else ""
    if task_id:
        task_snapshot = get_task_store().project_task_from_run(
            task_id,
            run_status="cancelled",
            run_id=run_id,
        ) or get_task_store().get_task(task_id)

    return {
        "cancelled": True,
        "cancelled_nodes": cancelled_nodes,
        "run": run_snapshot,
        "task": task_snapshot,
    }


def _build_cancel_payload(run_id: str, *, cancelled_nodes: int = 0, reason: str = "") -> Dict[str, Any]:
    run_snapshot = get_run_store().get_run(run_id) or {}
    task_snapshot = None
    task_id = str(run_snapshot.get("task_id", "") or "")
    if task_id:
        task_snapshot = get_task_store().get_task(task_id)

    payload: Dict[str, Any] = {
        "runId": run_id,
        "taskId": task_id,
        "runStatus": run_snapshot.get("status", ""),
        "taskStatus": task_snapshot.get("status", "") if task_snapshot else "",
        "activeNodeExecutionIds": run_snapshot.get("active_node_execution_ids", []),
        "cancelledNodes": cancelled_nodes,
        "_runVersion": run_snapshot.get("version", 0),
        "_taskVersion": task_snapshot.get("version", 0) if task_snapshot else 0,
    }
    if reason:
        payload["reason"] = reason
    return payload


def _build_dispatch_payload(
    run_id: str,
    node_execution_id: str,
    *,
    auto_chained: bool = False,
    launch_triggered: bool = False,
    reconnect_redispatch: bool = False,
) -> Dict[str, Any]:
    run_snapshot = get_run_store().get_run(run_id) or {}
    ne_snapshot = get_node_execution_store().get_node_execution(node_execution_id) or {}
    task_snapshot = None
    task_id = str(run_snapshot.get("task_id", "") or "")
    if task_id:
        task_snapshot = get_task_store().get_task(task_id)

    payload: Dict[str, Any] = {
        "runId": run_id,
        "taskId": task_id,
        "taskStatus": task_snapshot.get("status", "") if task_snapshot else "",
        "runStatus": run_snapshot.get("status", ""),
        "runtime": run_snapshot.get("runtime", ""),
        "workflowTemplateId": run_snapshot.get("workflow_template_id", ""),
        "activeNodeExecutionIds": run_snapshot.get("active_node_execution_ids", []),
        "nodeExecutionId": node_execution_id,
        "nodeKey": ne_snapshot.get("node_key", ""),
        "nodeLabel": ne_snapshot.get("node_label", ""),
        "_neVersion": ne_snapshot.get("version", 0),
        "_runVersion": run_snapshot.get("version", 0),
        "_taskVersion": task_snapshot.get("version", 0) if task_snapshot else 0,
    }
    if auto_chained:
        payload["autoChained"] = True
    if launch_triggered:
        payload["launchTriggered"] = True
    if reconnect_redispatch:
        payload["reconnectRedispatch"] = True
    return payload

def _resume_run_state(run_id: str) -> bool:
    if not run_id:
        return False
    rs = get_run_store()
    run = rs.get_run(run_id)
    if not run:
        return False
    current = str(run.get("status") or "")
    if current == "running":
        return True
    if current in ("failed", "cancelled"):
        if not _transition_run_if_needed(run_id, "queued"):
            return False
    return _transition_run_if_needed(run_id, "running")


def _resolve_task_id(task_id: Any = "", run_id: Any = "", node_execution_id: Any = "") -> str:
    resolved = str(task_id or "").strip()
    if resolved:
        return resolved

    resolved_run_id = str(run_id or "").strip()
    if not resolved_run_id and node_execution_id:
        node = get_node_execution_store().get_node_execution(str(node_execution_id).strip())
        if node:
            resolved_run_id = str(node.get("run_id") or "").strip()

    if resolved_run_id:
        run = get_run_store().get_run(resolved_run_id)
        if run:
            return str(run.get("task_id") or "").strip()

    return ""


def _save_connector_artifact(
    *,
    run_id: str,
    node_execution_id: str,
    artifact_type: str,
    title: str,
    content: str = "",
    path: str = "",
    metadata: Dict[str, Any] | None = None,
    artifact_id: str = "",
) -> Optional[Dict[str, Any]]:
    node_execution = get_node_execution_store().get_node_execution(node_execution_id) if node_execution_id else None
    resolved_run_id = str(run_id or (node_execution or {}).get("run_id") or "").strip()
    if not resolved_run_id and not node_execution_id:
        return None
    artifact = get_artifact_store().save_artifact({
        "id": artifact_id,
        "run_id": resolved_run_id,
        "node_execution_id": node_execution_id,
        "artifact_type": artifact_type,
        "title": title,
        "path": path,
        "content": content,
        "metadata": metadata or {},
    })
    if node_execution_id and artifact.get("id"):
        get_node_execution_store().update_node_execution(node_execution_id, {"artifact_ids": [artifact["id"]]})
    return artifact


# ─────────────────────────────────────────────
# Static Preview Server — serve generated output files
# Mount the FIXED path /tmp/evermind_output to avoid macOS temp dir resolution issues
# ─────────────────────────────────────────────
app.mount("/preview", StaticFiles(directory=_FIXED_OUTPUT, html=True), name="preview")


@app.get("/api/preview/list")
async def preview_list():
    """List generated artifacts, including task folders and output root files."""
    tasks = []
    if OUTPUT_DIR.exists():
        # task_x directories
        for task_dir in OUTPUT_DIR.iterdir():
            if not (task_dir.is_dir() and task_dir.name.startswith("task_")):
                continue
            files = []
            html_file = None
            latest_mtime = 0.0
            for f in sorted(task_dir.iterdir(), key=lambda p: p.name):
                if not f.is_file():
                    continue
                stat = f.stat()
                files.append({"name": f.name, "size": stat.st_size})
                latest_mtime = max(latest_mtime, stat.st_mtime)
                if f.suffix.lower() in (".html", ".htm") and html_file is None:
                    html_file = f.name
            if not files:
                continue
            tasks.append({
                "task_id": task_dir.name,
                "files": files,
                "html_file": html_file,
                "preview_url": f"/preview/{task_dir.name}/{html_file}" if html_file else None,
                "_mtime": latest_mtime,
            })

        # root-level artifacts (builder may write directly to /tmp/evermind_output/index.html)
        root_files = []
        root_html = None
        root_mtime = 0.0
        for f in sorted(OUTPUT_DIR.iterdir(), key=lambda p: p.name):
            if not f.is_file():
                continue
            stat = f.stat()
            root_files.append({"name": f.name, "size": stat.st_size})
            root_mtime = max(root_mtime, stat.st_mtime)
            if f.suffix.lower() in (".html", ".htm") and root_html is None:
                root_html = f.name
        if root_files:
            tasks.append({
                "task_id": "root",
                "files": root_files,
                "html_file": root_html,
                "preview_url": f"/preview/{root_html}" if root_html else None,
                "_mtime": root_mtime,
            })

        tasks.sort(key=lambda item: item.get("_mtime", 0.0), reverse=True)
        for item in tasks:
            item.pop("_mtime", None)

    return {"tasks": tasks, "output_dir": str(OUTPUT_DIR)}


@app.get("/api/preview/{task_id}")
async def preview_task(task_id: str):
    """Get preview info for a specific task, including the URL to its main HTML file."""
    task_dir = OUTPUT_DIR / task_id
    if not task_dir.exists() or not task_dir.is_dir():
        return JSONResponse(status_code=404, content={"error": f"Task {task_id} not found"})
    files = []
    html_file = None
    for f in sorted(task_dir.iterdir()):
        if f.is_file():
            files.append({"name": f.name, "size": f.stat().st_size})
            if f.suffix in (".html", ".htm") and html_file is None:
                html_file = f.name
    base_url = f"http://127.0.0.1:{os.getenv('PORT', '8765')}"
    preview_url = f"{base_url}/preview/{task_id}/{html_file}" if html_file else None
    return {
        "task_id": task_id,
        "files": files,
        "html_file": html_file,
        "preview_url": preview_url,
        "full_preview_url": preview_url,
    }


@app.post("/api/preview/validate")
async def preview_validate(data: Dict = Body(...)):
    """
    Validate preview artifact quality and optional browser smoke checks.
    Input:
      - preview_url: full URL (preferred)
      - task_id: optional fallback if preview_url is not provided
      - run_smoke: bool (optional, default false)
    """
    data = data or {}
    preview_url = str(data.get("preview_url") or "").strip()
    task_id = str(data.get("task_id") or "").strip()
    run_smoke = coerce_bool(data.get("run_smoke", False), default=False)

    if not preview_url and task_id:
        task_dir = OUTPUT_DIR / task_id
        if not task_dir.exists() or not task_dir.is_dir():
            return JSONResponse(status_code=404, content={"ok": False, "error": f"Task {task_id} not found"})
        html_files = sorted([f for f in task_dir.iterdir() if f.is_file() and f.suffix in (".html", ".htm")])
        if not html_files:
            return JSONResponse(status_code=404, content={"ok": False, "error": f"No HTML files found for {task_id}"})
        preview_url = build_preview_url_for_file(html_files[0], output_dir=OUTPUT_DIR)

    if not preview_url:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Provide preview_url or task_id"},
        )

    result = await validate_preview(preview_url, run_smoke=run_smoke)
    return result


@app.get("/api/export-pdf")
async def export_pdf():
    """Generate PDF from the latest preview HTML using Playwright."""
    # Find latest HTML file
    html_file = None
    best_mtime = 0.0
    if OUTPUT_DIR.exists():
        for f in OUTPUT_DIR.rglob("*.html"):
            try:
                mt = f.stat().st_mtime
                if mt > best_mtime:
                    best_mtime = mt
                    html_file = f
            except Exception:
                continue

    if not html_file or not html_file.exists():
        return JSONResponse(status_code=404, content={"error": "No HTML file found to export"})

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        # Fallback: serve the HTML file itself if Playwright not available
        return FileResponse(
            str(html_file),
            media_type="text/html",
            filename=html_file.stem + ".html",
            headers={"Content-Disposition": f'attachment; filename="{html_file.stem}.html"'},
        )

    pdf_path = Path("/tmp") / f"evermind_export_{int(time.time())}.pdf"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 720})
            # Load the HTML file directly (file:// protocol)
            await page.goto(f"file://{html_file.resolve()}", wait_until="networkidle", timeout=30000)
            # Wait a bit for animations/fonts to settle
            await page.wait_for_timeout(1500)
            # Generate PDF in landscape for slides/presentations
            await page.pdf(
                path=str(pdf_path),
                format="A4",
                landscape=True,
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
            await browser.close()

        if pdf_path.exists():
            return FileResponse(
                str(pdf_path),
                media_type="application/pdf",
                filename=f"{html_file.stem}.pdf",
                headers={"Content-Disposition": f'attachment; filename="{html_file.stem}.pdf"'},
            )
        return JSONResponse(status_code=500, content={"error": "PDF generation failed"})
    except Exception as e:
        logger.warning(f"PDF export failed: {e}")
        # Fallback: just download the HTML
        return FileResponse(
            str(html_file),
            media_type="text/html",
            filename=html_file.stem + ".html",
            headers={"Content-Disposition": f'attachment; filename="{html_file.stem}.html"'},
        )


@app.get("/api/diagnostics")
async def diagnostics():
    """One-stop diagnostics for app runtime, keys, ports, and latest preview info."""
    snap = await diagnostics_snapshot()
    snap["runtime"]["version"] = APP_VERSION
    snap["runtime"]["runtime_id"] = RUNTIME_ID
    snap["runtime"]["pid"] = os.getpid()
    snap["runtime"]["process_started_at"] = PROCESS_STARTED_AT
    snap["runtime"]["clients_connected"] = len(connected_clients)
    snap["runtime"]["active_tasks"] = sum(len(tasks) for tasks in _active_tasks.values())
    snap["runtime"]["log_file"] = str(LOG_FILE)
    snap["runtime"]["browser_headful"] = coerce_bool(os.getenv("EVERMIND_BROWSER_HEADFUL", "0"), default=False)
    snap["runtime"]["reviewer_tester_force_headful"] = coerce_bool(
        os.getenv("EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL", "1"),
        default=True,
    )
    return snap


@app.get("/")
async def root():
    """Serve the original Evermind frontend with all features."""
    if FRONTEND_HTML.exists():
        return FileResponse(str(FRONTEND_HTML), media_type="text/html")
    return {"error": "Frontend HTML not found", "expected_path": str(FRONTEND_HTML)}


@app.get("/api/status")
async def api_status():
    # Find latest HTML artifact for preview fallback
    latest_artifact = None
    latest_artifact_mtime = None
    try:
        html_files = list(OUTPUT_DIR.rglob("*.html"))
        if html_files:
            # Prefer task-scoped artifacts over root-level leftovers.
            task_scoped = []
            for f in html_files:
                try:
                    rel_parts = f.relative_to(OUTPUT_DIR).parts
                except Exception:
                    rel_parts = ()
                if rel_parts and str(rel_parts[0]).startswith("task_"):
                    task_scoped.append(f)
            candidates = task_scoped or html_files
            newest = max(candidates, key=lambda f: f.stat().st_mtime)
            latest_artifact = str(newest.relative_to(OUTPUT_DIR))
            latest_artifact_mtime = newest.stat().st_mtime
    except Exception:
        pass
    return {
        "status": "ok",
        "service": "Evermind Backend",
        "version": APP_VERSION,
        "runtime_id": RUNTIME_ID,
        "pid": os.getpid(),
        "process_started_at": PROCESS_STARTED_AT,
        "latest_artifact": latest_artifact,
        "latest_artifact_mtime": latest_artifact_mtime,
    }


@app.get("/api/logs")
async def api_logs(tail: int = 300):
    tail_n = coerce_int(tail, 300, minimum=20, maximum=2000)
    lines = []
    try:
        if LOG_FILE.exists():
            with LOG_FILE.open("r", encoding="utf-8", errors="ignore") as f:
                lines = [line.rstrip("\n") for line in deque(f, maxlen=tail_n)]
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"log read failed: {exc}", "log_file": str(LOG_FILE)},
        )
    return {"ok": True, "log_file": str(LOG_FILE), "tail": tail_n, "lines": lines}


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
    runtime_config = {
        "builder_enable_browser": is_builder_browser_enabled(),
    }
    return {
        "defaults": get_effective_default_plugins(config=runtime_config),
        "builder_enable_browser": runtime_config["builder_enable_browser"],
    }


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
# Task Board API Endpoints (任务看板 API)
# ─────────────────────────────────────────────
@app.get("/api/tasks")
async def list_tasks():
    """List all tasks."""
    store = get_task_store()
    return {"tasks": [_task_to_api(task) for task in store.list_tasks()]}


@app.post("/api/tasks")
async def create_task(data: Dict = Body(...)):
    """Create a new task."""
    if not data or not data.get("title"):
        return JSONResponse(status_code=400, content={"error": "title is required"})
    store = get_task_store()
    task = store.create_task(_task_from_api(data))
    return {"success": True, "task": _task_to_api(task)}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """Get a single task with linked report info."""
    store = get_task_store()
    task = store.get_task(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": f"Task {task_id} not found"})
    # Attach linked reports summary
    report_store = get_report_store()
    linked_reports = report_store.list_reports(task_id=task_id)
    task["reports"] = linked_reports
    return {"task": _task_to_api(task)}


@app.put("/api/tasks/{task_id}")
async def update_task(task_id: str, data: Dict = Body(...)):
    """Update task fields."""
    store = get_task_store()
    updated = store.update_task(task_id, _task_from_api(data or {}))
    if not updated:
        return JSONResponse(status_code=404, content={"error": f"Task {task_id} not found"})
    return {"success": True, "task": _task_to_api(updated)}


@app.post("/api/tasks/{task_id}/transition")
async def transition_task(task_id: str, data: Dict = Body(...)):
    """Transition task to a new status."""
    new_status = (data or {}).get("status", "")
    if not new_status:
        return JSONResponse(status_code=400, content={"error": "status is required"})
    store = get_task_store()
    result = store.transition_task(task_id, new_status)
    if not result.get("success"):
        status_code = 404 if "not found" in str(result.get("error", "")).lower() else 400
        return JSONResponse(status_code=status_code, content=result)
    if isinstance(result.get("task"), dict):
        result["task"] = _task_to_api(result["task"])
    return result


@app.get("/api/reports")
async def list_reports(task_id: str = "", taskId: str = ""):
    """List all run reports, optionally filtered by task_id."""
    store = get_report_store()
    resolved_task_id = task_id or taskId or None
    return {"reports": [_report_to_api(report) for report in store.list_reports(task_id=resolved_task_id)]}


@app.post("/api/reports")
async def save_report(data: Dict = Body(...)):
    """Save a run report to persistent storage."""
    if not data:
        return JSONResponse(status_code=400, content={"error": "No data provided"})
    normalized = _report_from_api(data)
    store = get_report_store()
    report = store.save_report(normalized)
    # Auto-link to task if task_id is provided
    task_id = normalized.get("task_id", "")
    if task_id:
        task_store = get_task_store()
        files = []
        for st in normalized.get("subtasks", []):
            files.extend(st.get("files_created", []) or st.get("filesCreated", []) or [])
        task_store.link_run(
            task_id, report["id"],
            summary=normalized.get("goal", ""),
            files=files,
        )
    return {"success": True, "report": _report_to_api(report)}


@app.get("/api/reports/{report_id}")
async def get_report(report_id: str):
    """Get a single run report."""
    store = get_report_store()
    report = store.get_report(report_id)
    if not report:
        return JSONResponse(status_code=404, content={"error": f"Report {report_id} not found"})
    return {"report": _report_to_api(report)}


# ─────────────────────────────────────────────
# V1 Run Lifecycle Endpoints
# ─────────────────────────────────────────────
@app.get("/api/runs")
async def list_runs(task_id: str = None, taskId: str = None):
    """List runs, optionally filtered by task_id."""
    tid = task_id or taskId
    return {"runs": get_run_store().list_runs(task_id=tid)}


@app.post("/api/runs")
async def create_run(data: Dict = Body(...)):
    """Create a new Run for a Task."""
    payload = dict(data or {})
    task_id = str(payload.get("task_id") or payload.get("taskId") or "").strip()
    if not task_id:
        return JSONResponse(status_code=400, content={"error": "task_id is required"})
    ts = get_task_store()
    if not ts.get_task(task_id):
        return JSONResponse(status_code=404, content={"error": f"Task {task_id} not found"})
    requested_id = str(payload.get("id") or "").strip()
    if requested_id and get_run_store().get_run(requested_id):
        return JSONResponse(status_code=409, content={"error": f"Run {requested_id} already exists"})
    payload["task_id"] = task_id
    run = get_run_store().create_run(payload)
    ts.update_task(task_id, {"run_ids": [run["id"]]})
    # Auto-transition task to 'executing' if it's in a pre-execution state
    task = ts.get_task(task_id)
    if task and task.get("status") in ("backlog", "planned"):
        ts.transition_task(task_id, "executing")
    return {"run": run}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    run = get_run_store().get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
    return {"run": run}


@app.put("/api/runs/{run_id}")
async def update_run(run_id: str, data: Dict = Body(...)):
    updated = get_run_store().update_run(run_id, data or {})
    if not updated:
        return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
    return {"run": updated}


@app.post("/api/runs/{run_id}/transition")
async def transition_run(run_id: str, data: Dict = Body(...)):
    new_status = (data or {}).get("status", "")
    run = get_run_store().get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"success": False, "error": f"Run {run_id} not found"})
    if new_status not in {"queued", "running", "waiting_review", "waiting_selfcheck", "failed", "done", "cancelled"}:
        return JSONResponse(status_code=400, content={"success": False, "error": f"Invalid run status: {new_status}"})
    if not _transition_run_if_needed(run_id, new_status):
        allowed = get_run_store().get_run(run_id)
        current = allowed.get("status", "") if allowed else ""
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": f"Cannot transition run from '{current}' to '{new_status}'",
        })
    return {"success": True, "run": get_run_store().get_run(run_id)}


@app.get("/api/workflow-templates")
async def api_list_workflow_templates():
    """P2-A: List available workflow templates."""
    return {"templates": list_templates()}


@app.post("/api/runs/launch")
async def launch_run(data: Dict = Body(...)):
    """P2-B: One-shot run launch — create run + NEs from template + start.

    Body: { task_id, template_id?, runtime?, timeout_seconds? }
    """
    task_id = str(data.get("task_id") or data.get("taskId") or "").strip()
    if not task_id:
        return JSONResponse(status_code=400, content={"error": "task_id is required"})

    task = get_task_store().get_task(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": f"Task {task_id} not found"})

    template_id = str(data.get("template_id") or data.get("templateId") or "standard").strip()
    requested_runtime = str(data.get("runtime") or "openclaw").strip()
    timeout_seconds = int(data.get("timeout_seconds") or data.get("timeoutSeconds") or 0)

    tpl = get_template(template_id)
    if not tpl:
        return JSONResponse(status_code=400, content={"error": f"Unknown template: {template_id}"})

    nodes_def = tpl["nodes"]

    # 1. Create run
    run_data = {
        "task_id": task_id,
        "runtime": requested_runtime,
        "workflow_template_id": template_id,
        "trigger_source": str(data.get("trigger_source") or "ui"),
    }
    if timeout_seconds > 0:
        run_data["timeout_seconds"] = timeout_seconds
    run = get_run_store().create_run(run_data)
    run_id = run["id"]
    runtime = str(run.get("runtime") or requested_runtime or "local").strip()

    # 2. Link run to task
    ts = get_task_store()
    ts.link_run(task_id, run_id)

    # 3. Create NodeExecutions from template
    nes_store = get_node_execution_store()
    created_nes = []
    for node_def in nodes_def:
        ne = nes_store.create_node_execution({
            "run_id": run_id,
            "node_key": node_def["key"],
            "node_label": node_def["label"],
            "depends_on_keys": node_def.get("depends_on", []),
        })
        created_nes.append(ne)

    # 4. Link NE ids to run
    ne_ids = [ne["id"] for ne in created_nes]
    get_run_store().update_run(run_id, {"node_execution_ids": ne_ids})

    # 5. Transition run to running
    get_run_store().transition_run(run_id, "running")

    # 6. Transition task to executing if needed
    if task.get("status") in ("backlog", "planned"):
        ts.transition_task(task_id, "executing")

    # 7. Determine nodes to dispatch (DAG roots)
    dispatched_ids: list[str] = []
    if runtime == "openclaw" and created_nes:
        ready = _auto_chain_next_node(run_id)
        if isinstance(ready, list):
            for ne_id in ready:
                _transition_node_if_needed(ne_id, "running")
                dispatched_ids.append(ne_id)
            if dispatched_ids:
                get_run_store().update_run(run_id, {
                    "current_node_execution_id": dispatched_ids[0],
                    "active_node_execution_ids": dispatched_ids,
                })
                for ne_id in dispatched_ids:
                    await _broadcast_ws_event({
                        "type": "evermind_dispatch_node",
                        "timestamp": int(time.time() * 1000),
                        "payload": _build_dispatch_payload(
                            run_id,
                            ne_id,
                            launch_triggered=True,
                        ),
                    })

    # 8. Get updated snapshots
    run_snapshot = get_run_store().get_run(run_id)
    task_snapshot = ts.get_task(task_id)
    created_nes = get_node_execution_store().list_node_executions(run_id=run_id)

    return {
        "success": True,
        "run": run_snapshot,
        "task": task_snapshot,
        "nodeExecutions": created_nes,
        "firstDispatchNodeId": dispatched_ids[0] if dispatched_ids else None,
        "dispatchedNodeIds": dispatched_ids,
        "templateId": template_id,
    }


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    run_snapshot = get_run_store().get_run(run_id)
    if not run_snapshot:
        return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})

    result = _cancel_run_cascade(run_id)
    if not result.get("cancelled"):
        return JSONResponse(status_code=400, content={
            "error": f"Run {run_id} could not be cancelled from status '{run_snapshot.get('status', '')}'",
            "run": result.get("run"),
        })

    payload = _build_cancel_payload(
        run_id,
        cancelled_nodes=int(result.get("cancelled_nodes", 0) or 0),
        reason="manual",
    )
    await _broadcast_ws_event({"type": "evermind_cancel_run", "payload": payload})
    return {
        "success": True,
        "run": result.get("run"),
        "task": result.get("task"),
        "cancelledNodes": result.get("cancelled_nodes", 0),
    }


@app.get("/api/runs/{run_id}/stale-nodes")
async def get_stale_nodes(run_id: str, stale_threshold_s: int = 60):
    """P1-2C: Return running nodes that haven't received an update in >threshold seconds."""
    run = get_run_store().get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
    now = time.time()
    threshold = max(10, min(stale_threshold_s, 600))
    nes = get_node_execution_store().list_node_executions(run_id=run_id)
    stale = []
    for ne in nes:
        if ne.get("status") != "running":
            continue
        last_update = float(ne.get("updated_at", 0) or ne.get("started_at", 0) or 0)
        if last_update > 0 and (now - last_update) > threshold:
            stale.append({
                "id": ne["id"],
                "node_key": ne.get("node_key", ""),
                "elapsed_since_update": round(now - last_update, 1),
            })
    return {"staleNodes": stale, "runId": run_id, "runtime": run.get("runtime", "local")}


# ─────────────────────────────────────────────
# V1 NodeExecution Endpoints
# ─────────────────────────────────────────────
@app.get("/api/node-executions")
async def list_node_executions(run_id: str = None, runId: str = None):
    rid = run_id or runId
    return {"nodeExecutions": get_node_execution_store().list_node_executions(run_id=rid)}


@app.post("/api/node-executions")
async def create_node_execution(data: Dict = Body(...)):
    payload = dict(data or {})
    run_id = str(payload.get("run_id") or payload.get("runId") or "").strip()
    node_key = str(payload.get("node_key") or payload.get("nodeKey") or "").strip()
    if not run_id:
        return JSONResponse(status_code=400, content={"error": "run_id is required"})
    if not node_key:
        return JSONResponse(status_code=400, content={"error": "node_key is required"})
    if not get_run_store().get_run(run_id):
        return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
    requested_id = str(payload.get("id") or "").strip()
    if requested_id and get_node_execution_store().get_node_execution(requested_id):
        return JSONResponse(status_code=409, content={"error": f"NodeExecution {requested_id} already exists"})
    payload["run_id"] = run_id
    payload["node_key"] = node_key
    ne = get_node_execution_store().create_node_execution(payload)
    get_run_store().update_run(ne["run_id"], {
        "node_execution_ids": [ne["id"]],
        "current_node_execution_id": ne["id"],
    })
    return {"nodeExecution": ne}


@app.get("/api/node-executions/{node_id}")
async def get_node_execution(node_id: str):
    ne = get_node_execution_store().get_node_execution(node_id)
    if not ne:
        return JSONResponse(status_code=404, content={"error": f"NodeExecution {node_id} not found"})
    return {"nodeExecution": ne}


@app.put("/api/node-executions/{node_id}")
async def update_node_execution(node_id: str, data: Dict = Body(...)):
    updated = get_node_execution_store().update_node_execution(node_id, data or {})
    if not updated:
        return JSONResponse(status_code=404, content={"error": f"NodeExecution {node_id} not found"})
    return {"nodeExecution": updated}


@app.post("/api/node-executions/{node_id}/transition")
async def transition_node_execution(node_id: str, data: Dict = Body(...)):
    new_status = (data or {}).get("status", "")
    ne = get_node_execution_store().get_node_execution(node_id)
    if not ne:
        return JSONResponse(status_code=404, content={"success": False, "error": f"NodeExecution {node_id} not found"})
    if not _transition_node_if_needed(node_id, new_status):
        current = (get_node_execution_store().get_node_execution(node_id) or {}).get("status", "")
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": f"Cannot transition node from '{current}' to '{new_status}'",
        })
    return {"success": True, "node_execution": get_node_execution_store().get_node_execution(node_id)}


@app.post("/api/node-executions/{node_id}/retry")
async def retry_node_execution(node_id: str):
    """Retry a failed/blocked node: creates a new NodeExecution for the same node_key in the same run."""
    nes = get_node_execution_store()
    old_ne = nes.get_node_execution(node_id)
    if not old_ne:
        return JSONResponse(status_code=404, content={"error": f"NodeExecution {node_id} not found"})
    if old_ne.get("status") not in ("failed", "blocked"):
        return JSONResponse(status_code=400, content={
            "error": f"Can only retry failed or blocked nodes. Current status: {old_ne['status']}"
        })
    retry_count = int(old_ne.get("retry_count", 0) or 0)
    if retry_count >= MAX_NODE_RETRY_COUNT:
        return JSONResponse(status_code=400, content={
            "error": f"Retry limit reached ({MAX_NODE_RETRY_COUNT}) for node {node_id}"
        })
    # Create a new NodeExecution inheriting the same run/node/model
    new_ne_data = {
        "run_id": old_ne["run_id"],
        "node_key": old_ne["node_key"],
        "node_label": old_ne.get("node_label", ""),
        "retried_from_id": old_ne["id"],
        "assigned_model": old_ne.get("assigned_model", ""),
        "assigned_provider": old_ne.get("assigned_provider", ""),
        "retry_count": retry_count + 1,
    }
    new_ne = nes.create_node_execution(new_ne_data)
    # Auto-link to run
    get_run_store().update_run(old_ne["run_id"], {
        "node_execution_ids": [new_ne["id"]],
        "current_node_execution_id": new_ne["id"],
    })
    return {"nodeExecution": new_ne, "retriedFrom": node_id}


# ─────────────────────────────────────────────
# V1 Artifact Endpoints
# ─────────────────────────────────────────────
@app.get("/api/artifacts")
async def list_artifacts(run_id: str = None, runId: str = None, node_execution_id: str = None, nodeExecutionId: str = None):
    rid = run_id or runId
    neid = node_execution_id or nodeExecutionId
    return {"artifacts": get_artifact_store().list_artifacts(run_id=rid, node_execution_id=neid)}


@app.post("/api/artifacts")
async def save_artifact(data: Dict = Body(...)):
    payload = dict(data or {})
    run_id = str(payload.get("run_id") or payload.get("runId") or "").strip()
    node_execution_id = str(payload.get("node_execution_id") or payload.get("nodeExecutionId") or "").strip()
    if not run_id and not node_execution_id:
        return JSONResponse(status_code=400, content={"error": "run_id or node_execution_id is required"})

    node_execution = None
    if node_execution_id:
        node_execution = get_node_execution_store().get_node_execution(node_execution_id)
        if not node_execution:
            return JSONResponse(status_code=404, content={"error": f"NodeExecution {node_execution_id} not found"})
        expected_run_id = str(node_execution.get("run_id") or "").strip()
        if run_id and expected_run_id and run_id != expected_run_id:
            return JSONResponse(
                status_code=400,
                content={"error": f"node_execution_id {node_execution_id} does not belong to run {run_id}"},
            )
        if not run_id:
            run_id = expected_run_id

    if not run_id:
        return JSONResponse(status_code=400, content={"error": "Unable to resolve run_id for artifact"})
    if not get_run_store().get_run(run_id):
        return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})

    requested_id = str(payload.get("id") or "").strip()
    if requested_id and get_artifact_store().get_artifact(requested_id):
        return JSONResponse(status_code=409, content={"error": f"Artifact {requested_id} already exists"})

    payload["run_id"] = run_id
    if node_execution_id:
        payload["node_execution_id"] = node_execution_id
    artifact = get_artifact_store().save_artifact(payload)
    if artifact.get("node_execution_id"):
        get_node_execution_store().update_node_execution(
            artifact["node_execution_id"], {"artifact_ids": [artifact["id"]]}
        )
    return {"artifact": artifact}


@app.get("/api/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str):
    a = get_artifact_store().get_artifact(artifact_id)
    if not a:
        return JSONResponse(status_code=404, content={"error": f"Artifact {artifact_id} not found"})
    return {"artifact": a}


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
        "max_timeout": coerce_int(os.getenv("SHELL_TIMEOUT", "30"), 30, minimum=5, maximum=600),
        "builder_enable_browser": is_builder_browser_enabled(),
        "tester_run_smoke": coerce_bool(os.getenv("EVERMIND_TESTER_RUN_SMOKE", "1"), default=True),
        "browser_headful": coerce_bool(os.getenv("EVERMIND_BROWSER_HEADFUL", "0"), default=False),
        "reviewer_tester_force_headful": coerce_bool(
            os.getenv("EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL", "1"),
            default=True,
        ),
        "max_retries": coerce_int(os.getenv("EVERMIND_MAX_RETRIES", "3"), 3, minimum=1, maximum=8),
    })
    if "builder_enable_browser" in data:
        bridge.config["builder_enable_browser"] = coerce_bool(data.get("builder_enable_browser"), default=False)
    if "tester_run_smoke" in data:
        bridge.config["tester_run_smoke"] = coerce_bool(data.get("tester_run_smoke"), default=True)
    if "browser_headful" in data:
        bridge.config["browser_headful"] = coerce_bool(data.get("browser_headful"), default=False)
    if "reviewer_tester_force_headful" in data:
        bridge.config["reviewer_tester_force_headful"] = coerce_bool(
            data.get("reviewer_tester_force_headful"),
            default=True,
        )
    if "max_retries" in data:
        bridge.config["max_retries"] = coerce_int(data.get("max_retries"), 3, minimum=1, maximum=8)
    node_type = node.get("data", {}).get("nodeType", node.get("type", ""))
    enabled_plugins = (
        node.get("plugins")
        or node.get("data", {}).get("plugins")
        or get_default_plugins_for_node(node_type, config=bridge.config)
    )
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
if _saved_settings.get("builder", {}).get("enable_browser_search", False):
    os.environ["EVERMIND_BUILDER_ENABLE_BROWSER"] = "1"
else:
    os.environ["EVERMIND_BUILDER_ENABLE_BROWSER"] = "0"
os.environ["EVERMIND_TESTER_RUN_SMOKE"] = "1" if coerce_bool(_saved_settings.get("tester_run_smoke", True), default=True) else "0"
os.environ["EVERMIND_BROWSER_HEADFUL"] = "1" if coerce_bool(_saved_settings.get("browser_headful", False), default=False) else "0"
os.environ["EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL"] = "1" if coerce_bool(
    _saved_settings.get("reviewer_tester_force_headful", True),
    default=True,
) else "0"
os.environ["EVERMIND_MAX_RETRIES"] = str(coerce_int(_saved_settings.get("max_retries", 3), 3, minimum=1, maximum=8))
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
        "builder_enable_browser": coerce_bool(settings.get("builder", {}).get("enable_browser_search", False), default=False),
        "tester_run_smoke": coerce_bool(settings.get("tester_run_smoke", True), default=True),
        "browser_headful": coerce_bool(settings.get("browser_headful", False), default=False),
        "reviewer_tester_force_headful": coerce_bool(settings.get("reviewer_tester_force_headful", True), default=True),
        "max_retries": coerce_int(settings.get("max_retries", 3), 3, minimum=1, maximum=8),
        "relay_endpoints": get_relay_manager().list(),
        "relay_count": len(get_relay_manager().list()),
        "has_keys": {k: bool(v) for k, v in settings.get("api_keys", {}).items()},
    }


@app.post("/api/settings/save")
async def save_user_settings(data: Dict = Body(...)):
    """Save settings to disk and apply API keys."""
    patch = dict(data or {})
    if "tester_run_smoke" in patch:
        patch["tester_run_smoke"] = coerce_bool(patch.get("tester_run_smoke"), default=True)
    if "browser_headful" in patch:
        patch["browser_headful"] = coerce_bool(patch.get("browser_headful"), default=False)
    if "reviewer_tester_force_headful" in patch:
        patch["reviewer_tester_force_headful"] = coerce_bool(
            patch.get("reviewer_tester_force_headful"),
            default=True,
        )
    if "max_retries" in patch:
        patch["max_retries"] = coerce_int(patch.get("max_retries"), 3, minimum=1, maximum=8)
    if "builder_enable_browser" in patch:
        builder_browser = coerce_bool(patch["builder_enable_browser"], default=False)
        patch.setdefault("builder", {})
        if isinstance(patch["builder"], dict):
            patch["builder"]["enable_browser_search"] = builder_browser
        patch.pop("builder_enable_browser", None)
    merged = _merge_settings(load_settings(), patch)
    if "relay_endpoints" not in (data or {}):
        merged["relay_endpoints"] = get_relay_manager().export()

    success = save_settings(merged)
    if success:
        count = apply_api_keys(merged)
        get_relay_manager().load(merged.get("relay_endpoints", []))
        if merged.get("builder", {}).get("enable_browser_search", False):
            os.environ["EVERMIND_BUILDER_ENABLE_BROWSER"] = "1"
        else:
            os.environ["EVERMIND_BUILDER_ENABLE_BROWSER"] = "0"
        os.environ["EVERMIND_TESTER_RUN_SMOKE"] = "1" if coerce_bool(merged.get("tester_run_smoke", True), default=True) else "0"
        os.environ["EVERMIND_BROWSER_HEADFUL"] = "1" if coerce_bool(merged.get("browser_headful", False), default=False) else "0"
        os.environ["EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL"] = "1" if coerce_bool(
            merged.get("reviewer_tester_force_headful", True),
            default=True,
        ) else "0"
        os.environ["EVERMIND_MAX_RETRIES"] = str(coerce_int(merged.get("max_retries", 3), 3, minimum=1, maximum=8))

        # Return which models are now available based on configured keys
        from ai_bridge import MODEL_REGISTRY
        provider_env_map = {
            "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
            "google": "GEMINI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
            "kimi": "KIMI_API_KEY", "qwen": "QWEN_API_KEY",
        }
        configured_providers = [
            p for p, env in provider_env_map.items() if os.environ.get(env)
        ]
        available_models = {}
        for model_id, info in MODEL_REGISTRY.items():
            provider = info.get("provider", "")
            if provider in configured_providers or provider == "ollama":
                if provider not in available_models:
                    available_models[provider] = []
                available_models[provider].append(model_id)

        return {
            "success": True,
            "keys_applied": count,
            "relay_count": len(get_relay_manager().list()),
            "configured_providers": configured_providers,
            "available_models": available_models,
        }
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
# P1-2C: Timeout Watchdog
# ─────────────────────────────────────────────
DEFAULT_NODE_TIMEOUT_S = 600    # 10 minutes
DEFAULT_RUN_TIMEOUT_S = 3600    # 1 hour
WATCHDOG_INTERVAL_S = 30        # check every 30 seconds
_watchdog_task: Optional[asyncio.Task] = None


async def _timeout_watchdog():
    """Background task that auto-fails stuck nodes and auto-cancels timed-out runs."""
    while True:
        try:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            now = time.time()

            # 1. Check for stuck nodes (running longer than timeout)
            nes = get_node_execution_store()
            for ne_dict in nes.list_node_executions():
                if ne_dict.get("status") != "running":
                    continue
                started = float(ne_dict.get("started_at", 0) or 0)
                if started <= 0:
                    continue
                ne_timeout = int(ne_dict.get("timeout_seconds", 0) or 0)
                if ne_timeout <= 0:
                    ne_timeout = DEFAULT_NODE_TIMEOUT_S
                elapsed = now - started
                if elapsed > ne_timeout:
                    ne_id = ne_dict["id"]
                    logger.warning(f"[Watchdog] Node {ne_id} timed out after {elapsed:.0f}s (limit={ne_timeout}s)")
                    _transition_node_if_needed(ne_id, "failed")
                    nes.update_node_execution(ne_id, {
                        "error_message": f"Timed out after {int(elapsed)}s (limit: {ne_timeout}s)",
                    })
                    ne_latest = nes.get_node_execution(ne_id) or {}
                    run_latest = get_run_store().get_run(str(ne_dict.get("run_id") or "")) or {}
                    await _broadcast_ws_event({
                        "type": "openclaw_node_update",
                        "payload": {
                            "runId": ne_latest.get("run_id", ne_dict.get("run_id", "")),
                            "nodeExecutionId": ne_id,
                            "nodeKey": ne_latest.get("node_key", ne_dict.get("node_key", "")),
                            "nodeLabel": ne_latest.get("node_label", ne_dict.get("node_label", "")),
                            "status": "failed",
                            "errorMessage": ne_latest.get("error_message", ""),
                            "tokensUsed": ne_latest.get("tokens_used", 0),
                            "cost": ne_latest.get("cost", 0.0),
                            "startedAt": ne_latest.get("started_at", 0),
                            "endedAt": ne_latest.get("ended_at", 0),
                            "timestamp": int(now * 1000),
                            "_neVersion": ne_latest.get("version", 0),
                            "_runVersion": run_latest.get("version", 0),
                        },
                    })

            # 2. Check for timed-out runs
            rs = get_run_store()
            for run_dict in rs.list_runs():
                if run_dict.get("status") != "running":
                    continue
                # Use the run's own timeout or default
                run_started = float(run_dict.get("started_at", 0) or 0)
                if run_started <= 0:
                    run_started = float(run_dict.get("created_at", 0) or 0)
                if run_started <= 0:
                    continue
                run_timeout = int(run_dict.get("timeout_seconds", 0) or 0)
                if run_timeout <= 0:
                    run_timeout = DEFAULT_RUN_TIMEOUT_S
                elapsed = now - run_started
                if elapsed > run_timeout:
                    run_id = run_dict["id"]
                    logger.warning(f"[Watchdog] Run {run_id} timed out after {elapsed:.0f}s (limit={run_timeout}s)")
                    cancel_result = _cancel_run_cascade(run_id)
                    if cancel_result.get("cancelled"):
                        await _broadcast_ws_event({
                            "type": "evermind_cancel_run",
                            "payload": _build_cancel_payload(
                                run_id,
                                cancelled_nodes=int(cancel_result.get("cancelled_nodes", 0) or 0),
                                reason="timeout",
                            ),
                        })

        except asyncio.CancelledError:
            logger.info("[Watchdog] Timeout watchdog cancelled")
            break
        except Exception as e:
            logger.warning(f"[Watchdog] Error in timeout watchdog: {e}")


@asynccontextmanager
async def lifespan(application):
    """FastAPI lifespan: start/stop background tasks."""
    global _watchdog_task
    _watchdog_task = asyncio.create_task(_timeout_watchdog())
    logger.info("[Watchdog] Timeout watchdog started")
    yield
    # ── Shutdown ──
    logger.info("Server shutting down — closing all connections...")
    if _watchdog_task and not _watchdog_task.done():
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass
        _watchdog_task = None
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


app.router.lifespan_context = lifespan


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
        "max_timeout": coerce_int(os.getenv("SHELL_TIMEOUT", "30"), 30, minimum=5, maximum=600),
        "allowed_dirs": allowed_dirs,
        "builder_enable_browser": is_builder_browser_enabled(),
        "tester_run_smoke": coerce_bool(os.getenv("EVERMIND_TESTER_RUN_SMOKE", "1"), default=True),
        "browser_headful": coerce_bool(os.getenv("EVERMIND_BROWSER_HEADFUL", "0"), default=False),
        "reviewer_tester_force_headful": coerce_bool(
            os.getenv("EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL", "1"),
            default=True,
        ),
        "max_retries": coerce_int(os.getenv("EVERMIND_MAX_RETRIES", "3"), 3, minimum=1, maximum=8),
    }

    # Create executor for this client
    ai_bridge = AIBridge(config=config)

    async def send_event(data: Dict):
        """Send real-time event to this client."""
        try:
            await ws.send_json(data)
        except Exception:
            pass

    async def broadcast_connector_event(data: Dict[str, Any]):
        """Broadcast connector events to all other connected websocket clients."""
        await _broadcast_ws_event(data, exclude_ws=ws)

    executor = NodeExecutor(ai_bridge=ai_bridge, on_event=send_event)
    orchestrator = Orchestrator(ai_bridge=ai_bridge, executor=executor, on_event=send_event)

    # Send initial handshake
    await ws.send_json({
        "type": "connected",
        "plugins": list(PluginRegistry.get_all().keys()),
        "defaults": get_effective_default_plugins(config=config),
        "models": ai_bridge.get_available_models(),
        "version": APP_VERSION,
        "runtime_id": RUNTIME_ID,
        "pid": os.getpid(),
        "process_started_at": PROCESS_STARTED_AT,
        "builder_enable_browser": coerce_bool(config.get("builder_enable_browser"), default=False),
        "tester_run_smoke": coerce_bool(config.get("tester_run_smoke", True), default=True),
        "browser_headful": coerce_bool(config.get("browser_headful", False), default=False),
        "reviewer_tester_force_headful": coerce_bool(config.get("reviewer_tester_force_headful", True), default=True),
        "max_retries": coerce_int(config.get("max_retries", 3), 3, minimum=1, maximum=8),
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
                    config["max_timeout"] = coerce_int(
                        new_config.get("max_timeout"),
                        coerce_int(config.get("max_timeout", 30), 30, minimum=5, maximum=600),
                        minimum=5,
                        maximum=600,
                    )
                if "builder_enable_browser" in new_config:
                    config["builder_enable_browser"] = coerce_bool(new_config.get("builder_enable_browser"), default=False)
                if "tester_run_smoke" in new_config:
                    config["tester_run_smoke"] = coerce_bool(new_config.get("tester_run_smoke"), default=True)
                    os.environ["EVERMIND_TESTER_RUN_SMOKE"] = "1" if config["tester_run_smoke"] else "0"
                if "browser_headful" in new_config:
                    config["browser_headful"] = coerce_bool(new_config.get("browser_headful"), default=False)
                    os.environ["EVERMIND_BROWSER_HEADFUL"] = "1" if config["browser_headful"] else "0"
                if "reviewer_tester_force_headful" in new_config:
                    config["reviewer_tester_force_headful"] = coerce_bool(
                        new_config.get("reviewer_tester_force_headful"),
                        default=True,
                    )
                    os.environ["EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL"] = "1" if config["reviewer_tester_force_headful"] else "0"
                if "max_retries" in new_config:
                    config["max_retries"] = coerce_int(new_config.get("max_retries"), 3, minimum=1, maximum=8)
                    os.environ["EVERMIND_MAX_RETRIES"] = str(coerce_int(config["max_retries"], 3, minimum=1, maximum=8))
                if isinstance(new_config.get("builder"), dict) and "enable_browser_search" in new_config.get("builder", {}):
                    config["builder_enable_browser"] = coerce_bool(new_config["builder"].get("enable_browser_search"), default=False)
                # Apply privacy settings
                if new_config.get("privacy"):
                    from privacy import update_masker_settings
                    update_masker_settings(new_config["privacy"])
                ai_bridge.config = config
                ai_bridge._setup_litellm()  # Re-init LiteLLM with new keys
                # Log only count of updated keys — never log key names or values
                key_count = sum(1 for k, v in new_config.items() if v and 'key' in k.lower())
                logger.info(f"Config updated: {key_count} API key(s) refreshed")
                # Tell frontend which providers now have keys configured
                providers_with_keys = [
                    name for name, env in key_map.items()
                    if os.getenv(env)
                ]
                await ws.send_json({
                    "type": "config_updated",
                    "keys_applied": key_count,
                    "providers": providers_with_keys,
                    "builder_enable_browser": coerce_bool(config.get("builder_enable_browser"), default=False),
                    "tester_run_smoke": coerce_bool(config.get("tester_run_smoke", True), default=True),
                    "browser_headful": coerce_bool(config.get("browser_headful", False), default=False),
                    "reviewer_tester_force_headful": coerce_bool(
                        config.get("reviewer_tester_force_headful", True),
                        default=True,
                    ),
                    "max_retries": coerce_int(config.get("max_retries", 3), 3, minimum=1, maximum=8),
                })

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
                # Extract recent chat history for context continuity
                chat_history = msg.get("chat_history", [])
                if not isinstance(chat_history, list):
                    chat_history = []
                # Sanitize: keep only role + content, limit to 10 messages
                safe_history = []
                allowed_roles = {"user", "agent"}
                for h in chat_history[-10:]:
                    if isinstance(h, dict) and h.get("role") and h.get("content"):
                        role = str(h["role"]).strip().lower()[:10]
                        if role not in allowed_roles:
                            continue
                        safe_history.append({
                            "role": role,
                            "content": str(h["content"])[:500],
                        })

                # ── Auto-detect model if default has no key ──
                if model == "gpt-5.4" and not os.environ.get("OPENAI_API_KEY"):
                    # Find first model with a configured API key
                    fallback_order = [
                        ("kimi-coding", "KIMI_API_KEY"),
                        ("kimi-k2.5", "KIMI_API_KEY"),
                        ("deepseek-v3", "DEEPSEEK_API_KEY"),
                        ("gemini-2.5-pro", "GEMINI_API_KEY"),
                        ("claude-4-sonnet", "ANTHROPIC_API_KEY"),
                        ("qwen-max", "QWEN_API_KEY"),
                    ]
                    for fallback_model, env_key in fallback_order:
                        if os.environ.get(env_key):
                            model = fallback_model
                            logger.info(f"Auto-selected model: {model} (OpenAI key not configured)")
                            await send_event({
                                "type": "system_info",
                                "message": f"🔄 自动选择模型: {model}（未配置 OpenAI Key）"
                            })
                            break

                task = asyncio.create_task(orchestrator.run(
                    goal, model,
                    conversation_history=safe_history,
                    difficulty=str(msg.get("difficulty", "standard")).strip().lower(),
                ))
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

            # ═══════════════════════════════════════════════════
            # OpenClaw V1 Connector Protocol — WS Message Handlers
            # ═══════════════════════════════════════════════════

            elif msg_type == "evermind_dispatch_node":
                # Evermind → OpenClaw: dispatch a node for execution
                from connector_idempotency import connector_idempotency
                idem_key = msg.get("idempotencyKey", "")
                cached = connector_idempotency.check(idem_key) if idem_key else None
                if cached is not None:
                    await ws.send_json(cached)
                else:
                    payload = msg.get("payload", {})
                    ne_id = payload.get("nodeExecutionId", "")
                    run_id = payload.get("runId", "")
                    node_key = payload.get("nodeKey", "")
                    logger.info(f"[OpenClaw] Dispatching node {ne_id} (key={node_key}) for run {run_id}")
                    # Transition node to running in canonical state
                    dispatched = False
                    try:
                        if ne_id:
                            dispatched = _transition_node_if_needed(ne_id, "running")
                    except Exception as e:
                        logger.warning(f"[OpenClaw] Node transition failed: {e}")
                    resp = {
                        "type": "evermind_dispatch_node_ack",
                        "requestId": msg.get("requestId", ""),
                        "payload": {
                            "runId": run_id,
                            "nodeExecutionId": ne_id,
                            "dispatched": dispatched,
                        }
                    }
                    if idem_key:
                        connector_idempotency.record(idem_key, resp)
                    await ws.send_json(resp)
                    if run_id and ne_id:
                        payload = {
                            **payload,
                            **_build_dispatch_payload(
                                run_id,
                                ne_id,
                                auto_chained=bool(payload.get("autoChained")),
                                launch_triggered=bool(payload.get("launchTriggered")),
                                reconnect_redispatch=bool(payload.get("reconnectRedispatch")),
                            ),
                        }
                    await broadcast_connector_event({
                        "type": "evermind_dispatch_node",
                        "requestId": msg.get("requestId", ""),
                        "idempotencyKey": idem_key,
                        "timestamp": msg.get("timestamp", 0),
                        "payload": payload,
                    })

            elif msg_type == "evermind_cancel_run":
                # Evermind → OpenClaw: cancel a run
                from connector_idempotency import connector_idempotency
                idem_key = msg.get("idempotencyKey", "")
                cached = connector_idempotency.check(idem_key) if idem_key else None
                if cached is not None:
                    await ws.send_json(cached)
                else:
                    payload = msg.get("payload", {})
                    run_id = payload.get("runId", "")
                    logger.info(f"[OpenClaw] Cancelling run {run_id}")
                    cancelled = False
                    cancelled_nodes = 0
                    cancel_payload: Dict[str, Any] = {"runId": run_id, "activeNodeExecutionIds": []}
                    try:
                        cancel_result = _cancel_run_cascade(run_id)
                        cancelled = bool(cancel_result.get("cancelled"))
                        cancelled_nodes = int(cancel_result.get("cancelled_nodes", 0) or 0)
                        cancel_payload = _build_cancel_payload(
                            run_id,
                            cancelled_nodes=cancelled_nodes,
                            reason="manual",
                        )
                        if cancelled:
                            logger.info(f"[OpenClaw] Cascaded cancel to {cancelled_nodes} nodes for run {run_id}")
                    except Exception as e:
                        logger.warning(f"[OpenClaw] Run cancel transition failed: {e}")
                    resp = {
                        "type": "evermind_cancel_run_ack",
                        "requestId": msg.get("requestId", ""),
                        "payload": {
                            "cancelled": cancelled,
                            **cancel_payload,
                        },
                    }
                    if idem_key:
                        connector_idempotency.record(idem_key, resp)
                    await ws.send_json(resp)
                    await broadcast_connector_event({
                        "type": "evermind_cancel_run",
                        "payload": cancel_payload,
                    })

            elif msg_type == "evermind_resume_run":
                # Evermind → OpenClaw: resume a run
                from connector_idempotency import connector_idempotency
                idem_key = msg.get("idempotencyKey", "")
                cached = connector_idempotency.check(idem_key) if idem_key else None
                if cached is not None:
                    await ws.send_json(cached)
                else:
                    payload = msg.get("payload", {})
                    run_id = payload.get("runId", "")
                    logger.info(f"[OpenClaw] Resuming run {run_id}")
                    resumed = False
                    try:
                        resumed = _resume_run_state(run_id)
                    except Exception as e:
                        logger.warning(f"[OpenClaw] Run resume transition failed: {e}")
                    resp = {"type": "evermind_resume_run_ack", "requestId": msg.get("requestId", ""), "payload": {"runId": run_id, "resumed": resumed}}
                    if idem_key:
                        connector_idempotency.record(idem_key, resp)
                    await ws.send_json(resp)
                    await broadcast_connector_event({"type": "evermind_resume_run", "payload": payload})

            elif msg_type == "evermind_rerun_node":
                # Evermind → OpenClaw: rerun a failed node
                from connector_idempotency import connector_idempotency
                idem_key = msg.get("idempotencyKey", "")
                cached = connector_idempotency.check(idem_key) if idem_key else None
                if cached is not None:
                    await ws.send_json(cached)
                else:
                    payload = msg.get("payload", {})
                    ne_id = payload.get("nodeExecutionId", "")
                    logger.info(f"[OpenClaw] Rerunning node {ne_id}")
                    rerunning = bool(ne_id and get_node_execution_store().get_node_execution(ne_id))
                    resp = {"type": "evermind_rerun_node_ack", "requestId": msg.get("requestId", ""), "payload": {"nodeExecutionId": ne_id, "rerunning": rerunning}}
                    if idem_key:
                        connector_idempotency.record(idem_key, resp)
                    await ws.send_json(resp)
                    await broadcast_connector_event({"type": "evermind_rerun_node", "payload": payload})

            elif msg_type == "openclaw_node_ack":
                # OpenClaw → Evermind: acknowledge node dispatch
                from connector_idempotency import connector_idempotency
                idem_key = msg.get("idempotencyKey", "")
                if idem_key and connector_idempotency.has_key(idem_key):
                    continue
                payload = msg.get("payload", {})
                ne_id = payload.get("nodeExecutionId", "")
                accepted = payload.get("accepted", False)
                logger.info(f"[OpenClaw] Node ack: {ne_id} accepted={accepted}")
                if idem_key:
                    connector_idempotency.record(idem_key, {"acked": True})
                await broadcast_connector_event({"type": "openclaw_node_ack", "payload": payload})

            elif msg_type == "openclaw_node_progress":
                # P3-C: OpenClaw → Evermind: real-time progress stream (partial output, tool calls, %)
                payload = msg.get("payload", {})
                ne_id = str(payload.get("nodeExecutionId", "")).strip()
                run_id = str(payload.get("runId", "")).strip()
                if not ne_id:
                    continue
                payload["timestamp"] = payload.get("timestamp") or int(time.time() * 1000)
                # Optionally update partial NE fields
                ne_store = get_node_execution_store()
                ne_snapshot = ne_store.get_node_execution(ne_id)
                if not ne_snapshot:
                    logger.warning(f"[OpenClaw] Progress update ignored for missing nodeExecutionId={ne_id}")
                    continue
                canonical_run_id = str(ne_snapshot.get("run_id", "")).strip()
                if canonical_run_id:
                    if run_id and run_id != canonical_run_id:
                        logger.warning(
                            f"[OpenClaw] Progress runId mismatch for {ne_id}: payload={run_id} canonical={canonical_run_id}",
                        )
                    run_id = canonical_run_id
                    payload["runId"] = canonical_run_id

                update_data: Dict[str, Any] = {}
                if payload.get("partialOutput"):
                    update_data["output_summary"] = str(payload["partialOutput"])
                if update_data:
                    ne_store.update_node_execution(ne_id, update_data)
                    ne_latest = ne_store.get_node_execution(ne_id)
                    if ne_latest:
                        payload["_neVersion"] = ne_latest.get("version", 0)
                if run_id:
                    run_latest = get_run_store().get_run(run_id)
                    if run_latest:
                        payload["_runVersion"] = run_latest.get("version", 0)
                await broadcast_connector_event({"type": "openclaw_node_progress", "payload": payload})

            elif msg_type == "openclaw_node_update":
                # OpenClaw → Evermind: node status/progress update
                from connector_idempotency import connector_idempotency
                idem_key = msg.get("idempotencyKey", "")
                if idem_key and connector_idempotency.has_key(idem_key):
                    continue
                payload = msg.get("payload", {})
                ne_id = payload.get("nodeExecutionId", "")
                status = payload.get("status", "")
                progress = payload.get("progress", 0)
                ts = _to_epoch_seconds(payload.get("timestamp", 0))
                logger.info(f"[OpenClaw] Node update: {ne_id} status={status} progress={progress}")
                # Persist to canonical state
                if ne_id and status:
                    try:
                        nes = get_node_execution_store()
                        ne = nes.get_node_execution(ne_id)
                        if ne:
                            existing_updated = float(ne.get("updated_at", 0) or 0)
                            if ts and existing_updated and ts + 1 < existing_updated:
                                logger.warning(f"[OpenClaw] Rejecting stale update for {ne_id}: ts={ts} < updated_at={existing_updated}")
                            else:
                                allowed_statuses = {"running", "passed", "failed", "blocked", "waiting_approval"}
                                if status in allowed_statuses:
                                    _transition_node_if_needed(ne_id, status)
                                    # Update token/cost if provided
                                    update_data: Dict[str, Any] = {}
                                    if "tokensUsed" in payload:
                                        update_data["tokens_used"] = payload["tokensUsed"]
                                    if "cost" in payload:
                                        update_data["cost"] = payload["cost"]
                                    if "costDelta" in payload:
                                        update_data["cost"] = _coerce_float(ne.get("cost"), 0.0) + _coerce_float(payload["costDelta"], 0.0)
                                    if "partialOutputSummary" in payload:
                                        update_data["output_summary"] = payload["partialOutputSummary"]
                                    if update_data:
                                        nes.update_node_execution(ne_id, update_data)
                                else:
                                    logger.warning(f"[OpenClaw] Rejected status '{status}' — not in allowed set")
                    except Exception as e:
                        logger.warning(f"[OpenClaw] Node update persist failed: {e}")
                if idem_key:
                    connector_idempotency.record(idem_key, {"updated": True})
                # P0-3: Inject current versions into broadcast for frontend
                if ne_id:
                    ne_latest = get_node_execution_store().get_node_execution(ne_id)
                    if ne_latest:
                        payload["_neVersion"] = ne_latest.get("version", 0)
                if payload.get("runId"):
                    run_latest = get_run_store().get_run(str(payload["runId"]))
                    if run_latest:
                        payload["_runVersion"] = run_latest.get("version", 0)
                        payload["activeNodeExecutionIds"] = run_latest.get("active_node_execution_ids", [])
                await broadcast_connector_event({"type": "openclaw_node_update", "payload": payload})

                # ── P1-2B: Auto-chain next node / auto-complete run ──
                run_id_for_chain = str(payload.get("runId", "")).strip()
                if status in ("passed", "skipped") and run_id_for_chain and _is_openclaw_run(run_id_for_chain):
                    try:
                        chain_result = _auto_chain_next_node(run_id_for_chain)
                        if isinstance(chain_result, list) and chain_result:
                            # Dispatch all ready nodes (parallel DAG resolution)
                            for chain_ne_id in chain_result:
                                next_ne = get_node_execution_store().get_node_execution(chain_ne_id)
                                next_node_key = next_ne.get("node_key", "") if next_ne else ""
                                logger.info(f"[OpenClaw] Auto-chaining: dispatching node {chain_ne_id} (key={next_node_key}) for run {run_id_for_chain}")
                                _transition_node_if_needed(chain_ne_id, "running")
                                # Build active list: existing running NEs + newly dispatched
                                run_snap = get_run_store().get_run(run_id_for_chain)
                                active = list(run_snap.get("active_node_execution_ids", [])) if run_snap else []
                                if chain_ne_id not in active:
                                    active.append(chain_ne_id)
                                # Remove the just-completed NE from active list
                                completed_ne_id = str(payload.get("nodeExecutionId", ""))
                                active = [x for x in active if x != completed_ne_id]
                                run_after_dispatch = get_run_store().update_run(run_id_for_chain, {
                                    "current_node_execution_id": chain_ne_id,
                                    "active_node_execution_ids": active,
                                })
                                await broadcast_connector_event({
                                    "type": "evermind_dispatch_node",
                                    "payload": {
                                        **_build_dispatch_payload(
                                            run_id_for_chain,
                                            chain_ne_id,
                                            auto_chained=True,
                                        ),
                                        "nodeKey": next_node_key,
                                        "_runVersion": run_after_dispatch.get("version", 0) if run_after_dispatch else 0,
                                    },
                                })
                        elif chain_result == "__ALL_DONE__":
                            # All nodes terminal — auto-complete the run
                            logger.info(f"[OpenClaw] All nodes terminal for run {run_id_for_chain} — auto-completing")
                            _transition_run_if_needed(run_id_for_chain, "done")
                            run_final = get_run_store().get_run(run_id_for_chain)
                            task_id_for_chain = run_final.get("task_id", "") if run_final else ""
                            # Project run completion to task
                            task_final = None
                            if task_id_for_chain:
                                ts = get_task_store()
                                task_final = ts.project_task_from_run(
                                    task_id_for_chain,
                                    run_status="done",
                                    run_id=run_id_for_chain,
                                    summary=run_final.get("summary", "") if run_final else "",
                                    remaining_risks=run_final.get("risks", []) if run_final else [],
                                )
                            complete_payload = {
                                "runId": run_id_for_chain,
                                "taskId": task_id_for_chain,
                                "finalResult": "success",
                                "autoCompleted": True,
                                "summary": run_final.get("summary", "") if run_final else "",
                                "risks": run_final.get("risks", []) if run_final else [],
                                "totalTokens": run_final.get("total_tokens", 0) if run_final else 0,
                                "totalCost": run_final.get("total_cost", 0) if run_final else 0,
                                "timestamp": int(time.time() * 1000),
                            }
                            if run_final:
                                complete_payload["_runVersion"] = run_final.get("version", 0)
                            if task_final:
                                complete_payload["_taskVersion"] = task_final.get("version", 0)
                            await broadcast_connector_event({"type": "openclaw_run_complete", "payload": complete_payload})
                    except Exception as e:
                        logger.warning(f"[OpenClaw] Auto-chain failed for run {run_id_for_chain}: {e}")

            elif msg_type == "openclaw_attach_artifact":
                # OpenClaw → Evermind: attach artifact to a node/run
                from connector_idempotency import connector_idempotency
                idem_key = msg.get("idempotencyKey", "")
                if idem_key and connector_idempotency.has_key(idem_key):
                    continue
                payload = msg.get("payload", {})
                artifact_data = payload.get("artifact", {})
                ne_id = payload.get("nodeExecutionId", "")
                run_id = payload.get("runId", "")
                logger.info(f"[OpenClaw] Attach artifact: {artifact_data.get('id', '?')} type={artifact_data.get('type', '?')} to ne={ne_id}")
                # Persist artifact via canonical stores and link it back to the node execution.
                try:
                    _save_connector_artifact(
                        artifact_id=str(artifact_data.get("id", f"artifact_{int(time.time()*1000)}")),
                        run_id=str(run_id or ""),
                        node_execution_id=str(ne_id or ""),
                        artifact_type=str(artifact_data.get("type", "raw_log") or "raw_log"),
                        title=str(artifact_data.get("title", "") or ""),
                        path=str(artifact_data.get("path", "") or ""),
                        content=str(artifact_data.get("content", "") or ""),
                        metadata=artifact_data.get("metadata") if isinstance(artifact_data.get("metadata"), dict) else {},
                    )
                except Exception as e:
                    logger.warning(f"[OpenClaw] Artifact persist failed: {e}")
                if idem_key:
                    connector_idempotency.record(idem_key, {"attached": True})
                await broadcast_connector_event({"type": "openclaw_attach_artifact", "payload": payload})

            elif msg_type == "openclaw_submit_review":
                # OpenClaw → Evermind: submit structured review
                from connector_idempotency import connector_idempotency
                idem_key = msg.get("idempotencyKey", "")
                if idem_key and connector_idempotency.has_key(idem_key):
                    continue
                payload = msg.get("payload", {})
                ne_id = payload.get("nodeExecutionId", "")
                decision = str(payload.get("decision", "") or "").strip().lower()
                issues = payload.get("issues", [])
                remaining_risks = payload.get("remainingRisks", [])
                run_id = payload.get("runId", "")
                task_id = _resolve_task_id(payload.get("taskId", ""), run_id, ne_id)
                if task_id:
                    payload["taskId"] = task_id
                logger.info(f"[OpenClaw] Submit review: ne={ne_id} decision={decision}")
                # Persist review and transition node
                try:
                    if ne_id:
                        _transition_node_if_needed(ne_id, "passed")
                        get_node_execution_store().update_node_execution(ne_id, {
                            "output_summary": f"Review: {decision} — {'; '.join(issues[:3])}",
                        })
                        _save_connector_artifact(
                            artifact_id=f"artifact_review_{int(time.time()*1000)}",
                            run_id=str(run_id or ""),
                            node_execution_id=str(ne_id or ""),
                            artifact_type="review_result",
                            title="Review Decision",
                            content=json.dumps({
                                "decision": decision,
                                "issues": issues,
                                "remaining_risks": remaining_risks,
                                "next_action": payload.get("nextAction", ""),
                            }),
                            metadata={"decision": decision, "issue_count": len(issues)},
                        )
                    # If run needs human approval, mark it
                    if run_id and decision in ("needs_fix", "reject", "blocked"):
                        _transition_run_if_needed(run_id, "waiting_review")
                    # P0-2: Unified task projection — review verdict
                    if task_id:
                        get_task_store().project_task_from_run(
                            task_id,
                            run_status="waiting_review" if decision in ("needs_fix", "reject", "blocked") else None,
                            review_verdict=decision,
                            review_issues=issues if isinstance(issues, list) else [],
                            remaining_risks=remaining_risks if isinstance(remaining_risks, list) else [],
                            run_id=run_id,
                        )
                except Exception as e:
                    logger.warning(f"[OpenClaw] Review persist failed: {e}")
                if idem_key:
                    connector_idempotency.record(idem_key, {"reviewed": True})
                # P0-3: Inject current versions into broadcast
                if run_id:
                    run_latest = get_run_store().get_run(run_id)
                    if run_latest:
                        payload["_runVersion"] = run_latest.get("version", 0)
                if task_id:
                    task_latest = get_task_store().get_task(task_id)
                    if task_latest:
                        payload["_taskVersion"] = task_latest.get("version", 0)
                await broadcast_connector_event({"type": "openclaw_submit_review", "payload": payload})

            elif msg_type == "openclaw_submit_validation":
                # OpenClaw → Evermind: submit structured validation/selfcheck
                from connector_idempotency import connector_idempotency
                idem_key = msg.get("idempotencyKey", "")
                if idem_key and connector_idempotency.has_key(idem_key):
                    continue
                payload = msg.get("payload", {})
                ne_id = payload.get("nodeExecutionId", "")
                summary_status = str(payload.get("summaryStatus", "") or "").strip().lower()
                checklist = payload.get("checklist", [])
                val_summary = str(payload.get("summary", "") or "")
                run_id = payload.get("runId", "")
                task_id = _resolve_task_id(payload.get("taskId", ""), run_id, ne_id)
                if task_id:
                    payload["taskId"] = task_id
                logger.info(f"[OpenClaw] Submit validation: ne={ne_id} status={summary_status}")
                try:
                    if ne_id:
                        _transition_node_if_needed(ne_id, "passed")
                        get_node_execution_store().update_node_execution(ne_id, {
                            "output_summary": val_summary,
                        })
                        _save_connector_artifact(
                            artifact_id=f"artifact_validation_{int(time.time()*1000)}",
                            run_id=str(run_id or ""),
                            node_execution_id=str(ne_id or ""),
                            artifact_type="report",
                            title="Validation Result",
                            content=json.dumps({
                                "summary_status": summary_status,
                                "summary": val_summary,
                                "checklist": checklist,
                            }),
                            metadata={"summary_status": summary_status, "checklist_count": len(checklist or [])},
                        )
                    if run_id and summary_status in ("failed", "blocked"):
                        _transition_run_if_needed(run_id, "waiting_selfcheck")
                    # P0-2: Unified task projection — selfcheck items
                    if task_id:
                        get_task_store().project_task_from_run(
                            task_id,
                            run_status="waiting_selfcheck" if summary_status in ("failed", "blocked") else None,
                            selfcheck_items=checklist if isinstance(checklist, list) else [],
                            summary=val_summary,
                            run_id=run_id,
                        )
                except Exception as e:
                    logger.warning(f"[OpenClaw] Validation persist failed: {e}")
                if idem_key:
                    connector_idempotency.record(idem_key, {"validated": True})
                # P0-3: Inject current versions into broadcast
                if run_id:
                    run_latest = get_run_store().get_run(run_id)
                    if run_latest:
                        payload["_runVersion"] = run_latest.get("version", 0)
                if task_id:
                    task_latest = get_task_store().get_task(task_id)
                    if task_latest:
                        payload["_taskVersion"] = task_latest.get("version", 0)
                await broadcast_connector_event({"type": "openclaw_submit_validation", "payload": payload})

            elif msg_type == "openclaw_run_complete":
                # OpenClaw → Evermind: report final run completion
                from connector_idempotency import connector_idempotency
                idem_key = msg.get("idempotencyKey", "")
                if idem_key and connector_idempotency.has_key(idem_key):
                    continue
                payload = msg.get("payload", {})
                run_id = payload.get("runId", "")
                final_result = str(payload.get("finalResult", "") or "").strip().lower()
                run_success = payload.get("success") is True or final_result in ("success", "done")
                run_summary = str(payload.get("summary", "") or "")
                run_risks = payload.get("risks", [])
                task_id = _resolve_task_id(payload.get("taskId", ""), run_id)
                if task_id:
                    payload["taskId"] = task_id
                logger.info(f"[OpenClaw] Run complete: run={run_id} task={task_id} result={final_result}")
                try:
                    if run_id:
                        target_status = "done" if run_success else "failed"
                        _transition_run_if_needed(run_id, target_status)
                        get_run_store().update_run(run_id, {
                            "summary": run_summary,
                            "risks": run_risks,
                            "total_tokens": payload.get("totalTokens", 0),
                            "total_cost": payload.get("totalCost", 0),
                        })
                    # P0-2: Unified task projection — run completion
                    if task_id:
                        run_target = "done" if run_success else "failed"
                        get_task_store().project_task_from_run(
                            task_id,
                            run_status=run_target,
                            summary=run_summary,
                            remaining_risks=run_risks if isinstance(run_risks, list) else [],
                            run_id=run_id,
                        )
                except Exception as e:
                    logger.warning(f"[OpenClaw] Run complete persist failed: {e}")
                if idem_key:
                    connector_idempotency.record(idem_key, {"completed": True})
                # P0-3: Inject current versions into broadcast
                if run_id:
                    run_latest = get_run_store().get_run(run_id)
                    if run_latest:
                        payload["_runVersion"] = run_latest.get("version", 0)
                if task_id:
                    task_latest = get_task_store().get_task(task_id)
                    if task_latest:
                        payload["_taskVersion"] = task_latest.get("version", 0)
                await broadcast_connector_event({"type": "openclaw_run_complete", "payload": payload})

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

    print(f"""
╔══════════════════════════════════════════╗
║     🧠 Evermind Backend Server          ║
║     Frontend: http://{host}:{port}/       ║
║     WebSocket: ws://{host}:{port}/ws      ║
║     REST API:  http://{host}:{port}/api   ║
║     Plugins:   {len(PluginRegistry.get_all())} loaded                ║
╚══════════════════════════════════════════╝
    """)

    # Use app object directly (not string "server:app") to avoid double
    # module initialization. reload=False ensures no reloader subprocess.
    uvicorn.run(app, host=host, port=port, log_level="info")
