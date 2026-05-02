"""
Evermind Backend — WebSocket Server
FastAPI + WebSocket server that bridges the frontend UI with the execution engine.
"""

import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
try:
    import fcntl
except ImportError:
    fcntl = None
import importlib
import json
import logging
import mimetypes
import os
import re
import signal
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Keep the packaged app bundle immutable at runtime.
sys.dont_write_bytecode = True

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
    is_image_generation_available,
    resolve_enabled_plugins_for_node,
)
from plugins.implementations import register_all as register_plugins
from ai_bridge import AIBridge, MODEL_REGISTRY, PROVIDER_ENV_KEY_MAP
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
    is_bootstrap_html_artifact,
    is_partial_html_artifact,
    latest_preview_artifact,
    resolve_preview_file,
    validate_preview,
)
from runtime_paths import ensure_output_dir_alias, resolve_output_dir, resolve_state_dir
from node_roles import normalize_node_role
from agent_skills import list_skill_catalog, install_skill_from_github, remove_installed_skill

_SESSION_CONTINUATION_HINT_RE = re.compile(
    r"(继续|接着|延续|沿用|基于上次|在这个基础上|在上个版本上|上一轮|刚才那个|同一个项目|继续优化|"
    r"continue|keep iterating|same project|same site|based on the previous|iterate on the current)",
    re.IGNORECASE,
)
_SESSION_ITERATIVE_EDIT_HINT_RE = re.compile(
    r"(修改|改一下|再改|再优化|微调|调整|完善|打磨|修一下|修复|修正|修补|继续做|继续完善|"
    r"modify|revise|refine|iterate|tweak|polish)",
    re.IGNORECASE,
)
_SESSION_DEICTIC_PROJECT_HINT_RE = re.compile(
    r"(这个|这次|当前|现有|刚才|上次|上一版|上一轮|前一个|同一个|该项目|该网站|该游戏|这个游戏|这个网站|当前项目|当前网站|当前游戏|"
    r"this|current|existing|previous|same|that one|the site|the game|the project)",
    re.IGNORECASE,
)
_SESSION_REFERENTIAL_ISSUE_HINT_RE = re.compile(
    r"(写得还可以|还有一点问题|这些问题|这些地方|上面的问题|当前的问题|existing issues|remaining issues|those issues|fix the issues)",
    re.IGNORECASE,
)
_SESSION_NEW_PROJECT_HINT_RE = re.compile(
    r"(全新|新建|重新做一个|重新创建|从零开始|另外做一个|另一个|新项目|新网站|新游戏|"
    r"brand new|new project|new site|new game|from scratch|create a new|build a new)",
    re.IGNORECASE,
)

# ─────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("evermind.server")

STATE_DIR = resolve_state_dir()
LOG_DIR = STATE_DIR / "logs"
LOG_FILE = LOG_DIR / "evermind-backend.log"
BACKEND_LOCK_FILE = STATE_DIR / "backend.lock"


def _ensure_file_logging():
    """v7.3.9 audit-fix CRITICAL — replace plain FileHandler with rotating
    handler. Prior code had no rotation, so heavy use produced a 5–10 GB log
    in 24h. Now: 50 MB max + 5 backups (250 MB total ceiling)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        for handler in root.handlers:
            base = getattr(handler, "baseFilename", "")
            if base and Path(base).resolve() == LOG_FILE.resolve():
                return  # Already attached
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=50 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
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
DESKTOP_BUILD_ID = str(os.getenv("EVERMIND_DESKTOP_BUILD_ID", "") or "").strip()
_backend_lock_handle = None
ORPHANED_RUNNING_RUN_STALE_S = max(15, coerce_int(os.getenv("EVERMIND_ORPHANED_RUNNING_RUN_STALE_S", 45), 45))

app = FastAPI(title="Evermind Backend", version=APP_VERSION)

# v6.4.4 (maintainer): GitHub integration router
try:
    from git_routes import router as _git_router
    app.include_router(_git_router)
except Exception as _git_err:
    import logging as _lg
    _lg.getLogger("evermind.server").warning("git routes failed to load: %s", _git_err)


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


def _safe_session_segment(value: Any) -> str:
    raw = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return (raw or f"session-{int(time.time())}")[:80]


def _safe_filename(value: Any, fallback: str = "attachment.bin") -> str:
    name = Path(str(value or fallback)).name.strip()
    if not name:
        name = fallback
    safe = re.sub(r"[^a-zA-Z0-9._ -]+", "_", name).strip(" .")
    return (safe or fallback)[:160]


def _guess_attachment_kind(name: str, mime_type: str) -> str:
    normalized_mime = str(mime_type or "").strip().lower()
    suffix = Path(name).suffix.lower()
    if normalized_mime.startswith("image/") or suffix in IMAGE_ATTACHMENT_EXTENSIONS:
        return "image"
    return "file"


def _store_chat_attachment_bytes(
    *,
    session_id: str,
    name: str,
    mime_type: str,
    raw_bytes: bytes,
) -> Dict[str, Any]:
    session_dir = CHAT_UPLOADS_DIR / _safe_session_segment(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _safe_filename(name)
    stem = Path(safe_name).stem[:80] or "attachment"
    suffix = Path(safe_name).suffix[:20]
    unique_name = f"{int(time.time() * 1000)}_{os.urandom(4).hex()}_{stem}{suffix}"
    target = session_dir / unique_name
    target.write_bytes(raw_bytes)

    guessed_mime = str(mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream").strip() or "application/octet-stream"
    return {
        "id": target.stem[:120],
        "name": safe_name,
        "path": str(target),
        "mime_type": guessed_mime[:160],
        "size": len(raw_bytes),
        "kind": _guess_attachment_kind(safe_name, guessed_mime),
    }


def _chat_attachment_public_url(request: Request, file_path: Path) -> str:
    try:
        rel = file_path.resolve().relative_to(CHAT_UPLOADS_DIR.resolve())
        rel_path = "/".join(rel.parts)
    except Exception:
        rel_path = file_path.name
    return str(request.base_url).rstrip("/") + f"/uploads/{rel_path}"


def _serialize_chat_attachment(record: Dict[str, Any], request: Request) -> Dict[str, Any]:
    path_value = Path(str(record.get("path") or "")).expanduser()
    kind = str(record.get("kind") or _guess_attachment_kind(path_value.name, str(record.get("mime_type") or "")))
    return {
        "id": str(record.get("id") or path_value.stem)[:120],
        "name": str(record.get("name") or path_value.name)[:220],
        "path": str(path_value),
        "mimeType": str(record.get("mime_type") or record.get("mimeType") or "application/octet-stream")[:160],
        "size": max(0, int(record.get("size") or 0)),
        "kind": kind,
        "previewUrl": _chat_attachment_public_url(request, path_value) if kind == "image" else "",
    }


def _normalize_run_goal_attachments(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    uploads_root = CHAT_UPLOADS_DIR.resolve()
    for item in value[:MAX_CHAT_ATTACHMENTS]:
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            continue
        try:
            resolved = Path(raw_path).expanduser().resolve()
        except Exception:
            continue
        try:
            resolved.relative_to(uploads_root)
        except Exception:
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        dedupe_key = str(resolved)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        name = str(item.get("name") or resolved.name).strip()[:220] or resolved.name
        mime_type = str(item.get("mimeType") or item.get("mime_type") or mimetypes.guess_type(name)[0] or "application/octet-stream").strip()[:160]
        result.append({
            "id": str(item.get("id") or resolved.stem)[:120],
            "name": name,
            "path": str(resolved),
            "mime_type": mime_type or "application/octet-stream",
            "size": resolved.stat().st_size,
            "kind": _guess_attachment_kind(name, mime_type),
        })
    return result


def _build_attachment_context_block(attachments: List[Dict[str, Any]]) -> str:
    if not attachments:
        return ""
    lines = [
        "[ATTACHED FILES]",
        "The user attached these files for this run. Reuse them and preserve their intent when editing or generating output.",
    ]
    for attachment in attachments[:MAX_CHAT_ATTACHMENTS]:
        size = int(attachment.get("size") or 0)
        lines.append(
            f"- {attachment.get('name')} | kind={attachment.get('kind')} | type={attachment.get('mime_type')} | size={size} bytes"
        )
        lines.append(f"  stored_path: {attachment.get('path')}")
    return "\n".join(lines)


def _compose_node_input_summary(
    base_summary: Any,
    *,
    effective_goal: str = "",
    session_context_note: str = "",
    cross_session_memory_note: str = "",
    limit: int = 2000,
) -> str:
    parts: List[str] = []
    summary = str(base_summary or "").strip()
    if summary:
        parts.append(summary)
    goal_text = str(effective_goal or "").strip()
    if goal_text:
        parts.append(f"[RUN GOAL]\n{goal_text}")
    session_note = str(session_context_note or "").strip()
    if session_note:
        parts.append(f"[SESSION CONTEXT]\n{session_note}")
    memory_note = str(cross_session_memory_note or "").strip()
    if memory_note:
        parts.append(f"[RECENT RELATED RUN MEMORY]\n{memory_note}")
    combined = "\n\n".join(part for part in parts if part).strip()
    return combined[:limit]


def _goal_requests_session_continuation(goal: str, chat_history: Optional[List[Dict[str, Any]]] = None) -> bool:
    goal_text = str(goal or "").strip()
    if not goal_text:
        return False
    if _SESSION_NEW_PROJECT_HINT_RE.search(goal_text):
        return False
    try:
        from orchestrator import is_continuation_input
        if is_continuation_input(goal_text):
            return True
    except Exception:
        pass
    if _SESSION_CONTINUATION_HINT_RE.search(goal_text):
        return True
    if _SESSION_ITERATIVE_EDIT_HINT_RE.search(goal_text) and _SESSION_DEICTIC_PROJECT_HINT_RE.search(goal_text):
        return True
    if _SESSION_ITERATIVE_EDIT_HINT_RE.search(goal_text) and _SESSION_REFERENTIAL_ISSUE_HINT_RE.search(goal_text):
        return True

    history = chat_history or []
    if len(history) >= 2 and _SESSION_ITERATIVE_EDIT_HINT_RE.search(goal_text):
        last_user_turn = next(
            (
                str(item.get("content") or "").strip()
                for item in reversed(history)
                if isinstance(item, dict) and str(item.get("role") or "").strip().lower() == "user"
            ),
            "",
        )
        if last_user_turn and (
            _SESSION_CONTINUATION_HINT_RE.search(last_user_turn)
            or _SESSION_DEICTIC_PROJECT_HINT_RE.search(last_user_turn)
        ):
            return True
    return False


_CROSS_SESSION_MEMORY_MAX_AGE_S = 14 * 24 * 60 * 60
_CROSS_SESSION_MEMORY_MIN_KEY_CHARS = 24
_CROSS_SESSION_MEMORY_NOTE_LIMIT = 1200
_PROJECT_MEMORY_DIGEST_LIMIT = 3200
_PROJECT_MEMORY_NODE_LIMIT = 6
_PROJECT_MEMORY_NODE_ORDER = {
    "planner": 1,
    "analyst": 2,
    "uidesign": 3,
    "scribe": 4,
    "imagegen": 5,
    "spritesheet": 6,
    "assetimport": 7,
    "builder": 8,
    "builder1": 9,
    "builder2": 10,
    "merger": 11,
    "reviewer": 12,
    "tester": 13,
    "deployer": 14,
    "debugger": 15,
}


def _normalize_goal_memory_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return text[:2000]


def _goal_task_type(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        import task_classifier as _tc
        return str(_tc.classify(text).task_type or "").strip().lower()
    except Exception:
        return ""


def _find_related_task_for_cross_session_memory(goal: str, *, session_id: str = "") -> Optional[Dict[str, Any]]:
    normalized_goal = _normalize_goal_memory_key(goal)
    if len(normalized_goal) < _CROSS_SESSION_MEMORY_MIN_KEY_CHARS:
        return None

    goal_task_type = _goal_task_type(goal)
    now = time.time()
    candidates = get_task_store().list_tasks()
    for task in candidates:
        if not isinstance(task, dict):
            continue
        candidate_session_id = str(task.get("session_id") or "").strip()
        if session_id and candidate_session_id == session_id:
            continue
        updated_at = _to_epoch_seconds(task.get("updated_at"))
        if updated_at and updated_at < now - _CROSS_SESSION_MEMORY_MAX_AGE_S:
            continue
        summary = str(task.get("latest_summary") or "").strip()
        risk = str(task.get("latest_risk") or "").strip()
        verdict = str(task.get("review_verdict") or "").strip()
        issues = task.get("review_issues")
        has_memory = bool(summary or risk or verdict or (isinstance(issues, list) and issues))
        if not has_memory:
            continue
        candidate_seed = str(task.get("description") or task.get("title") or "").strip()
        candidate_key = _normalize_goal_memory_key(candidate_seed)
        if len(candidate_key) < _CROSS_SESSION_MEMORY_MIN_KEY_CHARS:
            continue
        candidate_task_type = _goal_task_type(candidate_seed)
        if goal_task_type and candidate_task_type and goal_task_type != candidate_task_type:
            continue
        exact_match = candidate_key == normalized_goal
        prefix_match = candidate_key.startswith(normalized_goal) or normalized_goal.startswith(candidate_key)
        if exact_match or prefix_match:
            return task
    return None


def _build_cross_session_memory_note(task: Optional[Dict[str, Any]]) -> str:
    if not isinstance(task, dict):
        return ""

    lines = [
        "Recent related run detected from a different session/client.",
        "Use these lessons to preserve continuity after relay/client/session changes, but do NOT treat them as permission to reuse old artifacts unless this run explicitly requests continuation.",
    ]
    title = str(task.get("title") or "").strip()
    summary = str(task.get("latest_summary") or "").strip()
    risk = str(task.get("latest_risk") or "").strip()
    verdict = str(task.get("review_verdict") or "").strip().lower()
    if title:
        lines.append(f"Previous task: {title}")
    if summary:
        lines.append(f"Previous summary: {summary}")
    if risk:
        lines.append(f"Known risk: {risk}")
    if verdict:
        lines.append(f"Previous review verdict: {verdict}")
    issues = task.get("review_issues")
    if isinstance(issues, list):
        normalized_issues = [str(item).strip() for item in issues if str(item).strip()]
        if normalized_issues:
            lines.append("Previous review issues: " + " | ".join(normalized_issues[:4]))
    note = "\n".join(lines).strip()
    if len(note) <= _CROSS_SESSION_MEMORY_NOTE_LIMIT:
        return note
    return note[:_CROSS_SESSION_MEMORY_NOTE_LIMIT].rstrip() + "..."


def _compact_memory_text(value: Any, *, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _latest_task_run(task: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(task, dict):
        return None
    run_ids = task.get("run_ids")
    if not isinstance(run_ids, list):
        return None
    rs = get_run_store()
    runs: List[Dict[str, Any]] = []
    for run_id in run_ids:
        run = rs.get_run(str(run_id or "").strip())
        if isinstance(run, dict):
            runs.append(run)
    if not runs:
        return None
    runs.sort(
        key=lambda item: (
            _to_epoch_seconds(item.get("updated_at"))
            or _to_epoch_seconds(item.get("ended_at"))
            or _to_epoch_seconds(item.get("created_at"))
        ),
        reverse=True,
    )
    return runs[0]


def _project_memory_artifact_hint(node: Dict[str, Any]) -> str:
    if not isinstance(node, dict):
        return ""
    artifact_store = get_artifact_store()
    candidates = [
        ("latest_review_report_artifact_id", "review report"),
        ("latest_merge_manifest_artifact_id", "merge report"),
        ("latest_deployment_receipt_artifact_id", "deployment receipt"),
        ("summary_artifact_id", "summary report"),
        ("dossier_artifact_id", "execution dossier"),
    ]
    for field_name, label in candidates:
        artifact_id = str(node.get(field_name) or "").strip()
        if not artifact_id:
            continue
        artifact = artifact_store.get_artifact(artifact_id)
        if not isinstance(artifact, dict):
            continue
        path = _compact_memory_text(artifact.get("path") or "", limit=140)
        title = _compact_memory_text(artifact.get("title") or "", limit=100)
        if path:
            return f"{label}: {path}"
        if title:
            return f"{label}: {title}"
    return ""


def _project_memory_node_line(node: Dict[str, Any]) -> str:
    if not isinstance(node, dict):
        return ""
    label = str(node.get("node_label") or node.get("node_key") or "Node").strip()
    status = str(node.get("status") or "").strip().lower() or "unknown"
    summary_items = node.get("work_summary") if isinstance(node.get("work_summary"), list) else []
    summary = "；".join(str(item).strip() for item in summary_items[:2] if str(item).strip())
    if not summary:
        summary = (
            str(node.get("output_summary") or "").strip()
            or str(node.get("current_action") or "").strip()
            or str(node.get("error_message") or "").strip()
            or str(node.get("blocking_reason") or "").strip()
        )
    summary = _compact_memory_text(summary, limit=240)
    if not summary:
        return ""
    extras: List[str] = []
    loaded_skills = node.get("loaded_skills") if isinstance(node.get("loaded_skills"), list) else []
    if loaded_skills:
        extras.append("skills=" + ", ".join(str(skill).strip() for skill in loaded_skills[:4] if str(skill).strip()))
    refs = node.get("reference_urls") if isinstance(node.get("reference_urls"), list) else []
    if refs:
        extras.append(f"refs={len([item for item in refs if str(item).strip()])}")
    artifact_hint = _project_memory_artifact_hint(node)
    if artifact_hint:
        extras.append(artifact_hint)
    suffix = f" ({'; '.join(extras)})" if extras else ""
    return f"- {label} [{status}]: {summary}{suffix}"


def _build_project_memory_digest(
    task: Optional[Dict[str, Any]],
    *,
    continuation: bool,
) -> str:
    if not isinstance(task, dict):
        return ""

    lines: List[str] = [
        "Project memory digest from the latest known state of this project.",
        (
            "This run is explicitly continuing the same project. Patch existing files and preserve the strongest implemented parts."
            if continuation
            else "This run is related to a previous project. Reuse lessons and failure history, but only reuse artifacts if the user explicitly asks for continuation."
        ),
    ]
    title = _compact_memory_text(task.get("title") or "", limit=180)
    summary = _compact_memory_text(task.get("latest_summary") or "", limit=320)
    risk = _compact_memory_text(task.get("latest_risk") or "", limit=220)
    verdict = _compact_memory_text(task.get("review_verdict") or "", limit=60).lower()
    related_files = task.get("related_files") if isinstance(task.get("related_files"), list) else []
    issues = [
        _compact_memory_text(item, limit=180)
        for item in (task.get("review_issues") if isinstance(task.get("review_issues"), list) else [])
        if str(item or "").strip()
    ]
    if title:
        lines.append(f"Previous task: {title}")
    if summary:
        lines.append(f"Latest summary: {summary}")
    if risk:
        lines.append(f"Latest risk: {risk}")
    if verdict:
        lines.append(f"Latest review verdict: {verdict}")
    if issues:
        lines.append("Latest review issues:")
        lines.extend(f"- {item}" for item in issues[:5])
    if related_files:
        file_list = [str(item).strip() for item in related_files[:8] if str(item).strip()]
        if file_list:
            lines.append("Known project files: " + ", ".join(file_list))

    latest_run = _latest_task_run(task)
    if isinstance(latest_run, dict):
        run_status = _compact_memory_text(latest_run.get("status") or "", limit=40)
        run_summary = _compact_memory_text(latest_run.get("summary") or "", limit=320)
        run_risks = latest_run.get("risks") if isinstance(latest_run.get("risks"), list) else []
        if run_status:
            lines.append(f"Latest run status: {run_status}")
        if run_summary:
            lines.append(f"Latest run summary: {run_summary}")
        if run_risks:
            normalized_risks = [_compact_memory_text(item, limit=180) for item in run_risks if str(item or "").strip()]
            if normalized_risks:
                lines.append("Latest run remaining risks:")
                lines.extend(f"- {item}" for item in normalized_risks[:4])

        run_id = str(latest_run.get("id") or "").strip()
        if run_id:
            nodes = get_node_execution_store().list_node_executions(run_id=run_id) or []
            if nodes:
                nodes = sorted(
                    nodes,
                    key=lambda item: (
                        _PROJECT_MEMORY_NODE_ORDER.get(str(item.get("node_key") or "").strip().lower(), 999),
                        -_to_epoch_seconds(item.get("updated_at")),
                    ),
                )
                node_lines: List[str] = []
                for node in nodes:
                    line = _project_memory_node_line(node)
                    if not line:
                        continue
                    node_lines.append(line)
                    if len(node_lines) >= _PROJECT_MEMORY_NODE_LIMIT:
                        break
                if node_lines:
                    lines.append("Latest node carry-forward:")
                    lines.extend(node_lines)

    digest = "\n".join(line for line in lines if str(line or "").strip()).strip()
    if len(digest) <= _PROJECT_MEMORY_DIGEST_LIMIT:
        return digest
    return digest[:_PROJECT_MEMORY_DIGEST_LIMIT].rstrip() + "..."


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_backend_lock_metadata() -> Dict[str, Any]:
    try:
        if not BACKEND_LOCK_FILE.exists():
            return {}
        raw = BACKEND_LOCK_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _acquire_backend_runtime_lock() -> Optional[str]:
    global _backend_lock_handle
    if _backend_lock_handle is not None or fcntl is None:
        return None
    if coerce_bool(os.getenv("EVERMIND_ALLOW_MULTI_BACKEND", "0"), default=False):
        logger.warning("Multi-backend lock disabled via EVERMIND_ALLOW_MULTI_BACKEND=1")
        return None

    BACKEND_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(BACKEND_LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        existing = _read_backend_lock_metadata()
        handle.close()
        owner = f"pid={existing.get('pid')}, runtime_id={existing.get('runtime_id')}, port={existing.get('port')}"
        return (
            "Another Evermind backend is already running and holding the shared runtime lock "
            f"({owner}). Reuse the existing backend instead of starting a second one."
        )

    payload = {
        "pid": os.getpid(),
        "runtime_id": RUNTIME_ID,
        "port": int(os.getenv("PORT", "8765")),
        "started_at": PROCESS_STARTED_AT,
        "output_dir": str(resolve_output_dir()),
    }
    handle.seek(0)
    handle.truncate()
    json.dump(payload, handle)
    handle.flush()
    os.fsync(handle.fileno())
    _backend_lock_handle = handle
    return None


def _release_backend_runtime_lock() -> None:
    global _backend_lock_handle
    handle = _backend_lock_handle
    _backend_lock_handle = None
    if handle is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def _aggregate_run_metrics_from_nodes(run_id: str) -> Dict[str, Any]:
    run_id = str(run_id or "").strip()
    if not run_id:
        return {"total_tokens": 0, "total_cost": 0.0}
    try:
        node_executions = get_node_execution_store().list_node_executions(run_id=run_id) or []
    except Exception as exc:
        logger.warning("Failed to aggregate node metrics for run %s: %s", run_id, exc)
        return {"total_tokens": 0, "total_cost": 0.0}
    total_tokens = sum(max(0, coerce_int(ne.get("tokens_used"), 0)) for ne in node_executions if isinstance(ne, dict))
    total_cost = sum(max(0.0, _coerce_float(ne.get("cost"), 0.0)) for ne in node_executions if isinstance(ne, dict))
    return {
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 6),
    }


def _coalesce_run_metrics(run_id: str, reported_tokens: Any = None, reported_cost: Any = None) -> Dict[str, Any]:
    aggregated = _aggregate_run_metrics_from_nodes(run_id)
    reported_total_tokens = max(0, coerce_int(reported_tokens, 0))
    reported_total_cost = max(0.0, _coerce_float(reported_cost, 0.0))
    return {
        "total_tokens": max(reported_total_tokens, aggregated["total_tokens"]),
        "total_cost": round(max(reported_total_cost, aggregated["total_cost"]), 6),
    }


def _resolve_completion_preview_url(payload: Optional[Dict[str, Any]] = None) -> str:
    if isinstance(payload, dict):
        explicit_preview_url = str(payload.get("previewUrl", payload.get("preview_url")) or "").strip()
        if explicit_preview_url:
            return explicit_preview_url
    try:
        _task_key, preview_file = latest_preview_artifact()
        if preview_file:
            return build_preview_url_for_file(preview_file)
    except Exception as exc:
        logger.warning("Failed to resolve completion preview URL: %s", exc)
    return ""


def _is_live_write_stage(stage: Any) -> bool:
    return str(stage or "").strip().lower() in {"builder_write", "artifact_write"}


def _event_live_sync_paths(event: Dict[str, Any]) -> List[Path]:
    if not isinstance(event, dict):
        return []
    event_type = str(event.get("type") or "").strip().lower()
    raw_paths: List[Any] = []
    if event_type == "subtask_progress" and _is_live_write_stage(event.get("stage")):
        raw_paths = [event.get("path")]
    elif event_type == "files_created":
        files = event.get("files")
        if isinstance(files, list):
            raw_paths = files
    resolved_paths: List[Path] = []
    for raw in raw_paths:
        path_str = str(raw or "").strip()
        if not path_str:
            continue
        try:
            resolved_paths.append(Path(path_str).expanduser().resolve())
        except Exception:
            continue
    return resolved_paths


def _relative_live_delivery_path(path: Path) -> Optional[Path]:
    try:
        resolved = path.resolve()
        rel = resolved.relative_to(OUTPUT_DIR.resolve())
    except Exception:
        return None
    if not rel.parts:
        return None
    if any(part.startswith(".") for part in rel.parts):
        return None
    if any(part in _WORKSPACE_SKIP_DIRS for part in rel.parts):
        return None
    if path.is_file() and path.suffix.lower() in (".html", ".htm"):
        if is_partial_html_artifact(path) or is_bootstrap_html_artifact(path):
            return None
    if rel.parts[0] == "_stable_previews":
        return None
    if rel.parts[0].startswith("task_"):
        # During live builder sync, task_*/index.html is still a preview fallback and
        # must not overwrite the user's delivery folder until a final preview is approved.
        if len(rel.parts) == 2 and rel.name == "index.html":
            return None
        flattened = Path(*rel.parts[1:]) if len(rel.parts) > 1 else Path()
        if str(flattened) in {"", "."}:
            return None
        return flattened
    return rel


def _sync_live_event_artifacts_to_target(
    target_dir: Path,
    *,
    event: Dict[str, Any],
    preview_url: str = "",
) -> Dict[str, Any]:
    target_dir.mkdir(parents=True, exist_ok=True)

    copied_rel_paths: List[str] = []
    for source in _event_live_sync_paths(event):
        if not source.exists() or not source.is_file():
            continue
        rel = _relative_live_delivery_path(source)
        if rel is None:
            continue
        destination = target_dir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied_rel_paths.append(rel.as_posix())

    if copied_rel_paths:
        manifest_files = set(_read_sync_manifest(target_dir))
        manifest_files.update(copied_rel_paths)
        manifest_source_root = _resolve_sync_source_root(preview_url) if preview_url else OUTPUT_DIR
        _write_sync_manifest(
            target_dir,
            files=sorted(manifest_files),
            source_root=manifest_source_root,
            preview_url=preview_url,
        )

    return {
        "success": True,
        "target_dir": str(target_dir),
        "copied_files": len(copied_rel_paths),
        "files": copied_rel_paths[:200],
        "preview_url": preview_url,
        "live": True,
    }


def _maybe_auto_sync_delivery_artifacts(event: Dict[str, Any]) -> None:
    if not isinstance(event, dict):
        return
    event_type = str(event.get("type") or "").strip().lower()
    roots = _current_workspace_roots()
    target_raw = str(roots.get("artifact_sync_dir") or "").strip()
    if not target_raw:
        return
    target_dir = Path(target_raw).expanduser().resolve()
    if not _is_safe_workspace_root(target_dir):
        logger.warning("Skipping artifact auto-sync for unsafe target: %s", target_dir)
        return
    preview_url = str(event.get("preview_url") or event.get("previewUrl") or "").strip()
    try:
        if event_type == "preview_ready":
            if not bool(event.get("final")):
                return
            sync_result = _sync_output_to_target(target_dir, preview_url=preview_url)
        elif event_type == "files_created":
            sync_result = _sync_live_event_artifacts_to_target(target_dir, event=event, preview_url=preview_url)
        elif event_type == "subtask_progress" and _is_live_write_stage(event.get("stage")):
            sync_result = _sync_live_event_artifacts_to_target(target_dir, event=event, preview_url=preview_url)
        else:
            return
        event["artifact_sync"] = {
            "target_dir": sync_result.get("target_dir"),
            "copied_files": sync_result.get("copied_files", 0),
            "files": sync_result.get("files", [])[:50],
            "live": bool(sync_result.get("live")),
        }
    except Exception as exc:
        logger.warning("Artifact auto-sync failed: %s", exc)


_ACTIVE_RUN_STATUSES = {"queued", "running", "waiting_review", "waiting_selfcheck"}
_TERMINAL_NODE_STATUSES = {"passed", "failed", "skipped", "cancelled"}


def _live_task_progress(task_payload: Dict[str, Any]) -> int:
    status = str(task_payload.get("status") or "").strip().lower()
    stored_progress = max(0, min(100, coerce_int(task_payload.get("progress"), 0)))
    if status not in {"executing", "review", "selfcheck"}:
        return stored_progress

    task_id = str(task_payload.get("id") or "").strip()
    if not task_id:
        return stored_progress

    try:
        runs = get_run_store().list_runs(task_id=task_id)
    except Exception as exc:
        logger.warning("Failed to load runs for task %s progress hydration: %s", task_id, exc)
        return stored_progress

    latest_run = None
    if runs:
        active_runs = [run for run in runs if str(run.get("status") or "").strip().lower() in _ACTIVE_RUN_STATUSES]
        pool = active_runs or runs
        latest_run = max(pool, key=lambda run: _coerce_float(run.get("updated_at"), 0.0))
    if not latest_run:
        return stored_progress

    run_id = str(latest_run.get("id") or "").strip()
    if not run_id:
        return stored_progress

    try:
        nodes = get_node_execution_store().list_node_executions(run_id=run_id)
    except Exception as exc:
        logger.warning("Failed to load node executions for run %s progress hydration: %s", run_id, exc)
        return stored_progress
    if not nodes:
        return stored_progress

    total = len(nodes)
    terminal = 0
    started = 0
    active = 0
    for node in nodes:
        node_status = str(node.get("status") or "").strip().lower()
        if node_status != "queued":
            started += 1
        if node_status in _TERMINAL_NODE_STATUSES:
            terminal += 1
        if node_status == "running":
            active += 1

    if status == "review":
        return max(stored_progress, 60)
    if status == "selfcheck":
        return max(stored_progress, 80)

    started_ratio = started / max(total, 1)
    terminal_ratio = terminal / max(total, 1)
    live_progress = 20 + round(started_ratio * 30) + round(terminal_ratio * 35)
    if active > 0:
        live_progress += 3
    return max(stored_progress, min(live_progress, 89))


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
        "sessionId": str(payload.get("session_id", payload.get("sessionId")) or ""),
    }
    result["progress"] = _live_task_progress({
        "id": result["id"],
        "status": result["status"],
        "progress": result["progress"],
    })
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
        "runId": str(payload.get("run_id", payload.get("runId")) or ""),
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
    if "sessionId" in payload and "session_id" not in result:
        result["session_id"] = payload["sessionId"]
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
    if "runId" in payload and "run_id" not in result:
        result["run_id"] = payload["runId"]
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
            # v7.1f (maintainer): NO cache for /preview — reviewer/
            # tester must always observe the LATEST patched code. Old
            # `max-age=5` allowed Playwright to serve stale JS/CSS for 5s,
            # which is exactly long enough for a fast patcher edit to be
            # invisible to the immediately-following reviewer re-observation.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        else:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

app.add_middleware(SecurityHeadersMiddleware)


# ─────────────────────────────────────────────
# Security — Request body size limit
# v7.4: was 5 MB which silently blocked uploads larger than ~3.7 MB raw
# (base64 inflates ~33%) before the 25 MB UI / 12 MB route caps could
# return a friendly error. Bumped so the route-level cap is the binding
# constraint and the 413 middleware only catches genuinely abusive bodies.
# ─────────────────────────────────────────────
MAX_REQUEST_BODY_BYTES = 50 * 1024 * 1024  # 50 MB (covers 25 MB raw + base64 inflation + JSON envelope)


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
_detached_tasks: Set[asyncio.Task] = set()  # long-lived tasks that survive sender WS disconnect
# v6.4.34 (maintainer): cross-handler references so chat_stop can
# interrupt an in-flight chat turn. Keyed by client_id → handle.
_chat_cancel_handles: Dict[int, Dict[str, bool]] = {}
_chat_queue_handles: Dict[int, Any] = {}

# ── Session Memory: pipeline↔chat context sharing ──
# Key: session_id → {pipeline_results: [...], chat_messages: [...]}
_SESSION_MEMORY: Dict[str, Dict[str, list]] = {}
_SESSION_MEMORY_MAX_PIPELINE_RESULTS = 5   # keep last N pipeline runs
_SESSION_MEMORY_MAX_CHAT_MESSAGES = 60     # keep last N chat exchanges
_SESSION_MEMORY_MAX_SESSIONS = 50          # evict oldest when exceeded
_SESSION_MEMORY_FILE = Path.home() / ".evermind" / "chat_sessions.json"
_SESSION_MEMORY_SAVE_LOCK = threading.Lock()


def _load_session_memory_from_disk() -> None:
    """Load persisted chat session memory from disk at startup."""
    global _SESSION_MEMORY
    try:
        if _SESSION_MEMORY_FILE.exists():
            raw = _SESSION_MEMORY_FILE.read_text("utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                _SESSION_MEMORY = data
                logger.info(f"Loaded {len(_SESSION_MEMORY)} chat session(s) from {_SESSION_MEMORY_FILE}")
    except Exception as e:
        logger.warning(f"Failed to load chat sessions from disk: {e}")


def _save_session_memory_to_disk() -> None:
    """Persist chat session memory to disk (debounced by caller)."""
    with _SESSION_MEMORY_SAVE_LOCK:
        try:
            _SESSION_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _SESSION_MEMORY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(_SESSION_MEMORY, ensure_ascii=False), "utf-8")
            tmp.replace(_SESSION_MEMORY_FILE)
        except Exception as e:
            logger.warning(f"Failed to save chat sessions to disk: {e}")


# Debounce disk writes — schedule a save 2s after last change
_session_memory_save_timer: Optional[threading.Timer] = None


def _schedule_session_memory_save() -> None:
    """Schedule a debounced save of session memory to disk."""
    global _session_memory_save_timer
    if _session_memory_save_timer is not None:
        _session_memory_save_timer.cancel()
    _session_memory_save_timer = threading.Timer(2.0, _save_session_memory_to_disk)
    _session_memory_save_timer.daemon = True
    _session_memory_save_timer.start()


def _get_session_memory(session_id: str) -> Dict[str, list]:
    """Get or create session memory for a given session."""
    if not session_id:
        return {"pipeline_results": [], "chat_messages": []}
    if session_id not in _SESSION_MEMORY:
        # Evict oldest session if at capacity
        if len(_SESSION_MEMORY) >= _SESSION_MEMORY_MAX_SESSIONS:
            oldest_key = next(iter(_SESSION_MEMORY))
            _SESSION_MEMORY.pop(oldest_key, None)
        _SESSION_MEMORY[session_id] = {"pipeline_results": [], "chat_messages": []}
    return _SESSION_MEMORY[session_id]


def _store_pipeline_result_in_session(session_id: str, report: Dict[str, Any]) -> None:
    """Store a pipeline execution result summary in session memory."""
    if not session_id or not report:
        return
    mem = _get_session_memory(session_id)
    summary = {
        "goal": str(report.get("goal", ""))[:300],
        "status": str(report.get("status", report.get("finalResult", ""))),
        "difficulty": str(report.get("difficulty", "")),
        "subtasks": [
            {
                "agent": st.get("agent", ""),
                "status": st.get("status", ""),
                "work_summary": st.get("work_summary", [])[:3],
            }
            for st in (report.get("subtasks", []) or [])[:8]
        ],
        "timestamp": int(time.time()),
    }
    mem["pipeline_results"].append(summary)
    # Trim to max
    if len(mem["pipeline_results"]) > _SESSION_MEMORY_MAX_PIPELINE_RESULTS:
        mem["pipeline_results"] = mem["pipeline_results"][-_SESSION_MEMORY_MAX_PIPELINE_RESULTS:]
    _schedule_session_memory_save()


def _store_chat_message_in_session(session_id: str, role: str, content: str) -> None:
    """Store a chat message in session memory for context continuity."""
    if not session_id or not content:
        return
    mem = _get_session_memory(session_id)
    mem["chat_messages"].append({
        "role": role,
        "content": content[:2000],
        "ts": int(time.time()),
    })
    if len(mem["chat_messages"]) > _SESSION_MEMORY_MAX_CHAT_MESSAGES:
        _compress_chat_messages(mem)
    _schedule_session_memory_save()


def _compress_chat_messages(mem: Dict[str, list]) -> None:
    """Compress chat history by keeping recent messages and summarising older ones."""
    msgs = mem.get("chat_messages", [])
    if len(msgs) <= _SESSION_MEMORY_MAX_CHAT_MESSAGES:
        return
    # Keep the latest 40, drop the rest
    mem["chat_messages"] = msgs[-40:]


def _detect_workspace_tech_stack(index_path: Path) -> str:
    """v6.4.30 (maintainer) — peek at index.html head to identify
    the tech stack so chat agent proposes stack-appropriate edits.
    Returns a short one-liner like 'Three.js r164 (3D game)' or empty."""
    try:
        head = index_path.read_text(encoding="utf-8", errors="ignore")[:3000].lower()
    except Exception:
        return ""
    signals = []
    if "three.js" in head or "three.min.js" in head or "new three." in head:
        signals.append("Three.js (3D/WebGL)")
    if "canvas" in head and "getcontext" in head:
        signals.append("Canvas 2D")
    if 'from "react"' in head or "react.createelement" in head or 'import react' in head:
        signals.append("React")
    if "vue.js" in head or "createapp" in head:
        signals.append("Vue")
    if "phaser" in head:
        signals.append("Phaser")
    if "pixi" in head or "pixi.js" in head:
        signals.append("Pixi.js")
    if "tailwind" in head:
        signals.append("Tailwind CSS")
    if signals:
        return " + ".join(signals)
    return "vanilla HTML/CSS/JS"


def _load_workspace_conventions() -> str:
    """v6.4.30 — auto-detect and read CLAUDE.md / AGENTS.md / .cursorrules
    from OUTPUT_DIR root (Claude Code / Cursor auto-discovery pattern).
    The chat agent should honor project-specific conventions before every
    coding action — this avoids re-negotiating style rules every turn."""
    try:
        root = OUTPUT_DIR  # type: ignore[name-defined]
        if not root.exists():
            return ""
    except Exception:
        return ""
    for name in ("CLAUDE.md", "AGENTS.md", ".cursorrules", ".evermind-rules.md"):
        path = root / name
        if path.exists() and path.is_file():
            try:
                body = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if not body.strip():
                continue
            # Cap at ~2500 chars to avoid prompt bloat; keep first section.
            body = body[:2500]
            return (
                f"\n\n## Workspace Conventions (from {name}, auto-loaded)\n"
                "Honor these project-specific rules before every coding action:\n\n"
                f"{body}\n"
            )
    return ""


def _recent_focused_files_from_session(session_id: str) -> list[str]:
    """v6.4.30 — read the last 3-5 files the chat agent read/edited in
    this session. Persisted in session memory under 'focused_files'.
    Helps with multi-turn memory: user refines a previous change without
    re-specifying the path."""
    if not session_id:
        return []
    try:
        mem = _get_session_memory(session_id)
    except Exception:
        return []
    files = mem.get("focused_files") if isinstance(mem, dict) else None
    if not isinstance(files, list):
        return []
    return [str(f) for f in files[:5] if isinstance(f, str)]


def _build_active_project_context(*, compact: bool = False, session_id: str = "") -> str:
    """v6.4.29 (maintainer) — inject "where the user's current
    project lives" so the chat agent never replies "我没看到你的游戏前端源码".
    The pipeline's most recent output is always at OUTPUT_DIR/index.html.

    v6.4.30 (maintainer) — also injects:
      - detected tech stack ("Three.js (3D/WebGL)")
      - CLAUDE.md / AGENTS.md conventions
      - recently focused files from session memory

    Two modes:
      - compact=True  → short (≤ 600 chars) for OpenAI-family system prompts
        (relay 403s above ~6KB sys prompt).
      - compact=False → full with top-level listing + full conventions.
    """
    try:
        output_root = OUTPUT_DIR  # type: ignore[name-defined]
    except Exception:
        return ""
    try:
        if not output_root.exists():
            return ""
    except Exception:
        return ""
    index_path = output_root / "index.html"
    if not index_path.exists():
        # No pipeline output yet — still tell chat where the project WILL go.
        if compact:
            return (
                "\n\n## Active Project (ground truth)\n"
                f"- workspace_root: {output_root}\n"
                "- primary artifact (not yet written): index.html\n"
                "- No pipeline output yet. If user asks to modify 'the game/project/preview',\n"
                "  first run `file_ops list` on workspace_root to see what exists.\n"
            )
        return (
            "\n\n## Active Project (ground truth — Evermind auto-injected)\n"
            f"The user's pipeline workspace is `{output_root}` but no output is written yet.\n"
            "When the user refers to 'the game / 游戏 / the project / 当前项目 / the preview',\n"
            "they mean this directory. Start by `file_ops list` on it.\n"
        )
    # index.html exists — full Active Project block.
    try:
        size = index_path.stat().st_size
        mtime = int(index_path.stat().st_mtime)
    except Exception:
        size = 0
        mtime = 0
    # v6.4.30: tech stack + recently focused files give the agent instant
    # context without a round-trip.
    tech_stack = _detect_workspace_tech_stack(index_path)
    focused = _recent_focused_files_from_session(session_id) if session_id else []
    focused_block = ""
    if focused:
        focused_list = "\n".join(f"  - {f}" for f in focused)
        focused_block = (
            f"\n### Recently focused files (this session, most recent first)\n"
            f"{focused_list}\n"
            f"If the user's next message refers to 'that file' / 'the one I was editing',\n"
            f"they probably mean the top entry above. Don't ask — read it.\n"
        )
    if compact:
        tech_line = f"- tech stack: {tech_stack}\n" if tech_stack else ""
        focused_line = ""
        if focused:
            focused_line = f"- recent focus: {focused[0]}\n"
        return (
            "\n\n## Active Project (ground truth)\n"
            f"- workspace_root: {output_root}\n"
            f"- primary artifact: {index_path} (size={size}B)\n"
            f"{tech_line}"
            f"{focused_line}"
            f"- When user says 'the game / 游戏 / the project / 当前项目 / the preview / 准星 / 子弹 / 枪械',\n"
            f"  they mean this file. IMMEDIATELY `file_ops read {index_path}` BEFORE your FIRST\n"
            f"  prose sentence. NEVER reply '我没看到你的源码' or '请告诉我文件路径' — that path IS the source.\n"
            f"- Preview URL: http://127.0.0.1:8765/preview/?t=<epoch> (cache-busting query optional)\n"
        )
    # Full mode: list top-level entries.
    top_entries: list[str] = []
    try:
        for p in sorted(output_root.iterdir())[:30]:
            try:
                if p.name.startswith(".") or p.name.startswith("_"):
                    continue
                if p.is_dir():
                    top_entries.append(f"  {p.name}/")
                else:
                    sz = p.stat().st_size
                    top_entries.append(f"  {p.name} ({sz}B)")
            except Exception:
                continue
    except Exception:
        pass
    top_listing = "\n".join(top_entries) if top_entries else "  (empty)"
    tech_line = f"  tech stack: {tech_stack}\n" if tech_stack else ""
    return (
        "\n\n## Active Project (ground truth — Evermind auto-injected)\n"
        f"The user's most recent pipeline output lives at:\n"
        f"  workspace_root: {output_root}\n"
        f"  primary artifact: {index_path} (mtime={mtime}, size={size}B)\n"
        f"{tech_line}"
        f"{focused_block}"
        f"\n### Discover-then-act contract (MANDATORY, v6.4.30)\n"
        "When the user says 'the game / the project / 游戏 / 这个预览 / 准星不能动 / 子弹没轨迹 / \n"
        "没有枪械建模' WITHOUT a path, the Active Project block above IS the answer. You are\n"
        "FORBIDDEN from replying '哪里是源码' / '我没看到你的项目' / '请告诉我文件路径'. Immediately:\n"
        f"  1. `file_ops read {index_path}` — the whole game is usually one file\n"
        f"  2. If the HTML references external modules (e.g. `<script src='./assets/sprites.js'>`),\n"
        f"     `file_ops read` those too (they are in `{output_root}/assets/`)\n"
        "  3. `file_ops search` for the user's symbol (e.g. 'crosshair', 'bullet',\n"
        "     'gun', '准星', '弹道', 'weapon', 'fire')\n"
        "  4. Propose `file_ops edit` with exact old_string → new_string\n"
        "  5. If the browser tool is available, navigate to http://127.0.0.1:8765/preview/\n"
        "     to verify visually.\n\n"
        f"### Workspace top-level entries ({output_root.name}/)\n"
        f"{top_listing}\n\n"
        f"### Preview URL\nhttp://127.0.0.1:8765/preview/  (use `browser navigate` there for visual verification)\n\n"
        "(This block is auto-generated on every chat turn from the live filesystem — trust it as truth.)\n"
        f"{_load_workspace_conventions()}"
    )


def _chat_auto_pre_read_snapshot(user_message: str, session_id: str, *, kimi_compact: bool = False) -> str:
    """v6.4.31 (maintainer) — smart auto pre-read.

    v6.4.36 (maintainer): `kimi_compact=True` caps the snippet at
    2500 chars (vs 18000 default) because Kimi k2.6-code-preview stops
    emitting standard tool_calls and degenerates into prose
    (observed: "to=file_ops.read 体育彩票天天json") once system prompt
    exceeds ~10KB with tools enabled.

    When the user's message clearly refers to the current artifact (contains
    fix/debug/修/改 keywords OR names a file extension the pipeline just
    wrote), eagerly read OUTPUT_DIR/index.html (up to 18KB) and inject it
    into the system prompt as a <current_artifact> snapshot. This saves a
    round-trip: the chat agent starts the turn already knowing the code,
    so its FIRST message can be a concrete answer instead of a tool call.

    Adapted from Aider's "added to the chat" pattern + Cursor's context
    auto-attach behaviour.
    """
    try:
        root = OUTPUT_DIR  # type: ignore[name-defined]
        if not root.exists():
            return ""
    except Exception:
        return ""
    index = root / "index.html"
    if not index.exists() or not index.is_file():
        return ""
    msg = (user_message or "").lower()
    # Trigger keywords — intent to modify/inspect the current artifact.
    triggers_en = [
        "fix", "debug", "optimize", "refactor", "improve", "add ",
        "why doesn't", "why does", "it doesn't", "not working",
        "crosshair", "bullet", "weapon", "gun", "game", "project", "preview",
    ]
    triggers_zh = [
        "修", "改", "优化", "不行", "不工作", "不能", "失效",
        "准星", "子弹", "弹道", "枪械", "武器", "游戏", "项目", "预览",
        "帮我", "看看", "检查", "增加", "加一个", "改一下",
    ]
    if not any(t in msg for t in triggers_en) and not any(t in msg for t in triggers_zh):
        return ""
    # Skip if this session already has focused files — user + agent already
    # in a coding flow, the chat agent has read something recent.
    try:
        if _recent_focused_files_from_session(session_id):
            return ""
    except Exception:
        pass
    try:
        raw = index.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    size = len(raw)
    # v6.4.36: Kimi k2.6 stops emitting OpenAI-format tool_calls once
    # system prompt exceeds ~10KB. Shrink the pre-read to 2.5KB for Kimi
    # so the model can still make a normal file_ops read call and see
    # the full file. Other providers keep the richer 18KB.
    head_cap = 2500 if kimi_compact else 18000
    snippet = raw[:head_cap]
    truncated = size > head_cap
    lines_total = raw.count("\n") + 1
    lines_shown = snippet.count("\n") + 1
    return (
        "\n\n## Auto-Attached Source (v6.4.31)\n"
        f"The user's message suggests they want you to modify the current artifact. "
        f"To save you a tool round-trip, Evermind has pre-read the primary file:\n\n"
        f"<current_artifact path=\"{index}\" size=\"{size}B\" lines=\"{lines_total}\""
        f"{' truncated=\"true\"' if truncated else ''}>\n"
        f"{snippet}\n"
        f"</current_artifact>\n"
        f"\nShowing the first {lines_shown} of {lines_total} lines. "
        f"{'Call file_ops read if you need the tail.' if truncated else 'This is the FULL file.'}\n"
        "\nYou now know what the code looks like — do NOT call file_ops read on this same path\n"
        "before responding. Either answer directly or call file_ops edit / search for follow-up.\n"
    )


def _record_focused_file_in_session(session_id: str, file_path: str) -> None:
    """v6.4.30 — update 'recently focused' list when chat agent reads or
    edits a file. Called from the chat handler's tool-execution loop.
    Keeps most recent first, dedupes, max 10."""
    if not session_id or not file_path:
        return
    try:
        mem = _get_session_memory(session_id)
    except Exception:
        return
    existing = mem.get("focused_files")
    if not isinstance(existing, list):
        existing = []
    # Move file_path to front; dedupe.
    filtered = [f for f in existing if f != file_path]
    filtered.insert(0, file_path)
    mem["focused_files"] = filtered[:10]
    _schedule_session_memory_save()


def _build_pipeline_context_for_chat(session_id: str) -> str:
    """Build a richer session-memory block for the chat assistant.

    v5.8.6: beyond listing recent runs, surface artifacts, reviewer verdicts,
    and canvas topology so the chat can reason about the user's project. This
    is what turns the chat from a stateless assistant into a project-aware
    collaborator that can answer "why did reviewer reject?" or "design a new
    workflow on top of my last run."
    """
    if not session_id:
        return ""
    mem = _SESSION_MEMORY.get(session_id)
    if not mem:
        return ""
    lines: List[str] = []
    pipeline_results = mem.get("pipeline_results") or []
    if pipeline_results:
        lines.append("\n## Recent Pipeline Results (this session)")
        for i, pr in enumerate(pipeline_results[-5:], 1):
            status = pr.get("status", "unknown")
            goal = str(pr.get("goal", ""))[:180]
            lines.append(f"\n### Run {i}: {goal}")
            lines.append(f"Status: {status}  ·  Duration: {pr.get('durationSeconds', '?')}s  ·  Retries: {pr.get('retries', 0)}")
            # Surface reviewer verdict / blocking issues when available
            reviewer_notes = str(pr.get("reviewer_notes") or "")[:400]
            if reviewer_notes:
                lines.append(f"Reviewer: {reviewer_notes}")
            for st in pr.get("subtasks", [])[:14]:
                agent = st.get("agent", "?")
                st_status = st.get("status", "?")
                summaries = st.get("work_summary", [])
                summary_text = "; ".join(str(s) for s in summaries[:2]) if summaries else ""
                files_count = len(st.get("files_created") or st.get("filesCreated") or [])
                lines.append(
                    f"- {agent}: {st_status}"
                    + (f" · {files_count} files" if files_count else "")
                    + (f" — {summary_text}" if summary_text else "")
                )
    # Canvas workflow snapshot — what nodes the user currently has on the board
    canvas_plan = mem.get("canvas_plan") or {}
    canvas_nodes = canvas_plan.get("nodes") or []
    if canvas_nodes:
        lines.append("\n## Current Canvas Workflow")
        lines.append(
            f"The user has {len(canvas_nodes)} nodes on the canvas: "
            + ", ".join(str(n.get("agent") or n.get("label") or "?") for n in canvas_nodes[:20])
        )
    # Project memory digest — multi-session project identity (if linked)
    project_digest = str(mem.get("project_memory_digest") or "").strip()
    if project_digest:
        lines.append("\n## Project Memory Digest\n" + project_digest[:600])
    return "\n".join(lines)
_openclaw_dispatch_watchdogs: Dict[str, asyncio.Task] = {}
OPENCLAW_DISPATCH_ACK_TIMEOUT_S = coerce_int(
    os.getenv("EVERMIND_OPENCLAW_ACK_TIMEOUT_SEC", "12"),
    12,
    minimum=1,
    maximum=120,
)

# Output directory for generated files (shared with orchestrator)
OUTPUT_DIR = resolve_output_dir()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# v7.3 (maintainer) — per-task workspaces for session isolation.
# Each task gets its own folder under ~/.evermind/workspaces/<task_id>/
# so files added in one task are never visible in another. Inspired by
# Claude Code worktrees (~/.claude/worktrees/<session>/) and Claude Cowork
# walled-off project workspaces.
_TASK_WORKSPACES_DIR = Path(os.path.expanduser("~/.evermind/workspaces"))


def _task_workspace_dir(task_id: str, *, create: bool = True) -> Path:
    """Return the absolute path to <task_id>'s isolated workspace folder."""
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "", str(task_id or "").strip())[:64]
    if not safe:
        raise ValueError("task_id required for workspace resolution")
    p = _TASK_WORKSPACES_DIR / safe
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def _scan_workspace_files(root: Path, *, max_entries: int = 500, max_depth: int = 6) -> List[Dict[str, Any]]:
    """List files in a task workspace. Returns [{name, rel_path, size, kind}].

    v7.3.4 audit fix MINOR-2 — bounded BFS instead of `sorted(rglob('*'))`,
    so a workspace with 100k files doesn't materialize the whole tree before
    truncating. Symlinks are not followed (st.is_dir() / iterdir behave
    correctly via pathlib's lazy resolution).
    """
    if not root.exists() or not root.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    queue: List[Tuple[Path, int]] = [(root, 0)]
    while queue and len(out) < max_entries:
        current, depth = queue.pop(0)
        if depth > max_depth:
            continue
        try:
            entries = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (PermissionError, OSError):
            continue
        for p in entries:
            if len(out) >= max_entries:
                break
            try:
                if p.name.startswith("."):
                    continue
                if p.is_symlink():
                    # Don't traverse symlinks (cycle / escape risk)
                    continue
                rel = str(p.relative_to(root))
                if p.is_dir():
                    out.append({"name": p.name, "rel_path": rel, "size": 0, "kind": "dir"})
                    if depth < max_depth:
                        queue.append((p, depth + 1))
                elif p.is_file():
                    out.append({"name": p.name, "rel_path": rel, "size": p.stat().st_size, "kind": "file"})
            except Exception:
                continue
    return out


@app.get("/api/tasks/{task_id}/workspace")
async def api_task_workspace(task_id: str):
    """List files in a task's isolated workspace."""
    try:
        root = _task_workspace_dir(task_id, create=False)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    files = _scan_workspace_files(root)
    total_size = sum(int(f.get("size", 0) or 0) for f in files if f.get("kind") == "file")
    file_count = sum(1 for f in files if f.get("kind") == "file")
    return {
        "task_id": task_id,
        "path": str(root),
        "exists": root.exists(),
        "files": files,
        "stats": {"file_count": file_count, "total_size": total_size},
    }


@app.post("/api/tasks/{task_id}/workspace/upload")
async def api_task_workspace_upload(task_id: str, payload: Dict[str, Any] = Body(default={})):
    """Upload files into a task's workspace.

    Body: {
        files: [
            {name: str (required), content: str | base64 string},
            ...
        ],
        encoding?: 'utf-8' | 'base64'  (per-file or batch),
    }
    """
    try:
        root = _task_workspace_dir(task_id)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    files = payload.get("files") if isinstance(payload.get("files"), list) else []
    if not files:
        return JSONResponse(status_code=400, content={"error": "files list required"})
    batch_encoding = str(payload.get("encoding") or "utf-8").lower()
    saved: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    from pathlib import PurePosixPath
    root_resolved = root.resolve()
    for raw in files[:128]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        # v7.3.4 audit MINOR-4: split into path components instead of substring
        # check, so `foo..bar.txt` (legitimate filename) is no longer rejected,
        # while `foo/../bar` and `..` parts are still blocked.
        if not name or name.startswith("/"):
            rejected.append({"name": name, "reason": "invalid name"})
            continue
        try:
            parts = PurePosixPath(name).parts
        except Exception:
            rejected.append({"name": name, "reason": "unparseable path"})
            continue
        if any(part == ".." or part == "" for part in parts):
            rejected.append({"name": name, "reason": "path traversal"})
            continue
        content = raw.get("content")
        if content is None:
            rejected.append({"name": name, "reason": "no content"})
            continue
        encoding = str(raw.get("encoding") or batch_encoding).lower()
        target = root / name
        # v7.3.3 audit fix MAJOR-5: defense-in-depth — even after the `..`
        # substring + leading-`/` checks, verify the resolved target stays
        # inside the workspace root. A symlink already inside root could
        # otherwise route the write outside.
        try:
            if not str(target.resolve()).startswith(str(root_resolved) + os.sep):
                rejected.append({"name": name, "reason": "resolves outside workspace"})
                continue
        except Exception as _resolve_err:
            rejected.append({"name": name, "reason": f"resolve failed: {str(_resolve_err)[:80]}"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if encoding == "base64":
                import base64
                target.write_bytes(base64.b64decode(content))
            else:
                target.write_text(str(content), encoding="utf-8")
            saved.append({"name": name, "rel_path": str(target.relative_to(root)), "size": target.stat().st_size})
        except Exception as exc:
            rejected.append({"name": name, "reason": str(exc)[:120]})
    return {"ok": True, "saved": saved, "rejected": rejected, "path": str(root)}


@app.delete("/api/tasks/{task_id}/workspace/{filename:path}")
async def api_task_workspace_delete(task_id: str, filename: str):
    """Remove a file or directory from a task's workspace."""
    try:
        root = _task_workspace_dir(task_id, create=False)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    safe_name = filename.strip().lstrip("/")
    # v7.3.9 audit-fix CRITICAL — same hardening as upload (line 1804+):
    # 1) reject path components equal to ".." instead of bare substring
    #    (so a legitimate filename like `foo..bar.txt` survives), and
    # 2) require a trailing `os.sep` when comparing resolved-target prefix
    #    against root, so a workspace `abc` does NOT permit deletion of
    #    `abc-evil/` (without sep, startsWith would match prefix `abc`).
    from pathlib import PurePosixPath
    try:
        parts = PurePosixPath(safe_name).parts
    except Exception:
        return JSONResponse(status_code=400, content={"error": "unparseable path"})
    if any(p == ".." or p == "" for p in parts):
        return JSONResponse(status_code=400, content={"error": "path traversal"})
    target = (root / safe_name).resolve()
    root_resolved = root.resolve()
    try:
        if not str(target).startswith(str(root_resolved) + os.sep):
            return JSONResponse(status_code=400, content={"error": "outside workspace"})
        if target.is_dir():
            import shutil
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        else:
            return JSONResponse(status_code=404, content={"error": "not found"})
        return {"ok": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)[:200]})

# v6.5 Phase 3: chat Plan/Act/Debug modes. Each mode scopes which tools the
# LLM may call + appends a short behavioural suffix to the system prompt.
# `tools_whitelist` of None means "all tools enabled" (act mode). Names refer
# to the chat tool `function.name` strings declared in _chat_worker.
CHAT_MODES: Dict[str, Dict[str, Any]] = {
    "plan": {
        "tools_whitelist": {"file_ops.read", "file_ops.list", "file_ops.search", "browser.observe", "browser.navigate"},
        "sys_suffix": "\n\n## PLAN MODE (v6.5)\n你现在是规划助手。只读/研究/提出方案,不改文件。用户确认后让他们切到 ACT 模式执行。",
    },
    "act": {
        "tools_whitelist": None,
        "sys_suffix": "\n\n## ACT MODE (v6.5)\n你现在执行已确认的计划。可 file_ops.write/edit 和所有工具。",
    },
    "debug": {
        "tools_whitelist": {"file_ops.read", "file_ops.search", "browser.navigate", "browser.observe", "browser.screenshot"},
        "sys_suffix": "\n\n## DEBUG MODE (v6.5)\n你现在做根因定位。先读日志/复现/假设/验证,不要盲改代码。",
    },
}


def _filter_chat_tools_by_mode(tools: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    """Keep only tool specs whose fn name (or fn.name.action) is in the whitelist."""
    cfg = CHAT_MODES.get(mode) or CHAT_MODES["act"]
    wl = cfg.get("tools_whitelist")
    if wl is None:
        return list(tools)
    out: List[Dict[str, Any]] = []
    for t in tools or []:
        fn = (t.get("function") or {}) if isinstance(t, dict) else {}
        fn_name = str(fn.get("name") or "").strip()
        if not fn_name:
            continue
        # whitelist granularity: either exact fn name ("browser") OR "fn.action".
        # we include a tool if ANY of its allowed variants matches OR if the
        # bare fn name is in the whitelist (action-level filtering happens
        # server-side when dispatching the tool call).
        allowed_bare = any(w == fn_name or w.startswith(fn_name + ".") for w in wl)
        if allowed_bare:
            out.append(t)
    return out


def _load_agents_md_and_memories(output_dir: Path) -> str:
    """v6.5 Phase 3: read <OUTPUT_DIR>/AGENTS.md + .evermind/memories/*.md.

    Returns an injectable system-prompt block, or empty string if nothing on disk.
    """
    parts: List[str] = []
    try:
        agents_md = Path(output_dir) / "AGENTS.md"
        if agents_md.is_file() and agents_md.stat().st_size < 64 * 1024:
            body = agents_md.read_text(encoding="utf8", errors="ignore").strip()
            if body:
                parts.append("## AGENTS.md (project conventions)\n" + body)
    except Exception:
        pass
    try:
        mem_dir = Path(output_dir) / ".evermind" / "memories"
        if mem_dir.is_dir():
            mem_chunks: List[str] = []
            for mp in sorted(mem_dir.glob("*.md"))[:20]:
                try:
                    if mp.stat().st_size > 16 * 1024:
                        continue
                    text = mp.read_text(encoding="utf8", errors="ignore").strip()
                    if text:
                        mem_chunks.append(f"### {mp.stem}\n{text}")
                except Exception:
                    continue
            if mem_chunks:
                parts.append("## Auto-Memory (prior sessions)\n" + "\n\n".join(mem_chunks))
    except Exception:
        pass
    return "\n\n".join(parts)


def _maybe_persist_chat_memory(
    output_dir: Path,
    user_text: str,
    assistant_text: str,
    chat_mode: str,
) -> None:
    """v6.5 Phase 3: after a chat turn completes, ask the LLM (via a tiny
    keyword heuristic first; full LLM decision can hook here later) whether
    the turn warrants persisting a memory note. Fires-and-forgets on disk.
    """
    try:
        if not assistant_text or len(assistant_text) < 200:
            return
        combined = f"{user_text}\n{assistant_text}".lower()
        trigger = any(kw in combined for kw in (
            "记住", "remember", "convention", "rule", "always", "never",
            "下次", "以后", "from now on", "根因", "root cause", "decision",
        ))
        if not trigger:
            return
        mem_dir = Path(output_dir) / ".evermind" / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)
        # naive topic extraction: first 30 chars of user text, slugified.
        topic = re.sub(r"[^a-z0-9一-龥]+", "-", user_text.strip().lower())[:40] or "note"
        ts = time.strftime("%Y-%m-%dT%H-%M-%S")
        path = mem_dir / f"{topic}-{ts}.md"
        body = (
            f"# {user_text[:80]}\n\n"
            f"- mode: {chat_mode}\n"
            f"- captured_at: {ts}\n\n"
            f"## User\n{user_text.strip()[:1200]}\n\n"
            f"## Assistant (truncated)\n{assistant_text.strip()[:2400]}\n"
        )
        path.write_text(body, encoding="utf8")
    except Exception:
        pass
# Keep the legacy /tmp alias synced to the active runtime directory so older
# prompts and preview assumptions cannot drift onto stale artifacts.
LEGACY_OUTPUT_ALIAS = ensure_output_dir_alias(OUTPUT_DIR)
CHAT_UPLOADS_DIR = STATE_DIR / "uploads"
CHAT_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
MAX_CHAT_ATTACHMENTS = max(1, coerce_int(os.getenv("EVERMIND_CHAT_ATTACHMENT_MAX_FILES", 8), 8, minimum=1, maximum=24))
MAX_CHAT_ATTACHMENT_BYTES = max(
    64 * 1024,
    coerce_int(os.getenv("EVERMIND_CHAT_ATTACHMENT_MAX_BYTES", 12 * 1024 * 1024), 12 * 1024 * 1024, minimum=64 * 1024),
)
IMAGE_ATTACHMENT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp", ".avif"}

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
        except (RuntimeError, OSError):
            # v3.1: Only evict on connection-level errors (broken pipe, disconnected).
            # Transient send failures should not permanently evict the client.
            stale_clients.append(client_ws)
        except Exception as exc:
            logger.debug("Non-fatal broadcast error: %s", str(exc)[:200])
    for client_ws in stale_clients:
        connected_clients.discard(client_ws)


def _is_benign_ws_disconnect_error(exc: Exception) -> bool:
    message = str(exc or "")
    return isinstance(exc, RuntimeError) and "WebSocket is not connected" in message


def _track_client_task(
    client_id: int,
    task: asyncio.Task,
    *,
    cancel_on_disconnect: bool = True,
) -> asyncio.Task:
    """Track a background task started on behalf of a websocket client.

    Some tasks, such as `run_goal`, must keep running even if the requester
    disconnects because a separate desktop UI websocket may still be attached.
    """
    bucket = _active_tasks.setdefault(client_id, [])
    bucket.append(task)
    if not cancel_on_disconnect:
        _detached_tasks.add(task)

    def _cleanup(done_task: asyncio.Task):
        _detached_tasks.discard(done_task)
        tracked = _active_tasks.get(client_id)
        if tracked is None:
            return
        try:
            tracked.remove(done_task)
        except ValueError:
            return
        if not tracked:
            _active_tasks.pop(client_id, None)

    task.add_done_callback(_cleanup)
    return task


def _cancel_tracked_task(task: asyncio.Task):
    _detached_tasks.discard(task)
    if not task.done():
        task.cancel()


def _cancel_openclaw_dispatch_watchdog(node_execution_id: str):
    if not node_execution_id:
        return
    task = _openclaw_dispatch_watchdogs.pop(node_execution_id, None)
    if task and not task.done():
        task.cancel()


async def _fail_openclaw_dispatch(
    *,
    run_id: str,
    node_execution_id: str,
    summary: str,
    risks: Optional[List[str]] = None,
):
    if not run_id:
        return

    _cancel_openclaw_dispatch_watchdog(node_execution_id)

    node_store = get_node_execution_store()
    run_store = get_run_store()
    task_store = get_task_store()

    node_snapshot = node_store.get_node_execution(node_execution_id) if node_execution_id else None
    if node_snapshot and node_snapshot.get("status") not in ("passed", "failed", "skipped", "cancelled"):
        node_store.update_node_execution(node_execution_id, {
            "error_message": summary,
            "output_summary": summary,
        })
        _transition_node_if_needed(node_execution_id, "failed")
        node_snapshot = node_store.get_node_execution(node_execution_id)

    normalized_risks = risks if isinstance(risks, list) and risks else [
        "OpenClaw runtime did not acknowledge the node dispatch.",
    ]
    run_store.update_run(run_id, {
        "summary": summary,
        "risks": normalized_risks,
    })
    _transition_run_if_needed(run_id, "failed")
    run_snapshot = run_store.get_run(run_id)
    task_id = str((run_snapshot or {}).get("task_id", "") or "")
    task_snapshot = None
    if task_id:
        task_snapshot = task_store.project_task_from_run(
            task_id,
            run_status="failed",
            summary=summary,
            remaining_risks=normalized_risks,
            run_id=run_id,
        ) or task_store.get_task(task_id)

    if node_snapshot:
        await _broadcast_ws_event({
            "type": "openclaw_node_update",
            "payload": {
                "runId": run_id,
                "taskId": task_id,
                "runtime": "openclaw",
                "nodeExecutionId": node_execution_id,
                "nodeKey": node_snapshot.get("node_key", ""),
                "nodeLabel": node_snapshot.get("node_label", ""),
                "status": "failed",
                "errorMessage": summary,
                "partialOutputSummary": summary,
                "activeNodeExecutionIds": (run_snapshot or {}).get("active_node_execution_ids", []),
                "timestamp": int(time.time() * 1000),
                "_neVersion": node_snapshot.get("version", 0),
                "_runVersion": (run_snapshot or {}).get("version", 0),
                "_taskVersion": task_snapshot.get("version", 0) if task_snapshot else 0,
            },
        })

    await _broadcast_ws_event({
        "type": "system_info",
        "message": summary,
    })
    await _broadcast_ws_event({
        "type": "openclaw_run_complete",
        "payload": {
            "runId": run_id,
            "taskId": task_id,
            "finalResult": "failed",
            "success": False,
            "summary": summary,
            "risks": normalized_risks,
            "runtime": "openclaw",
            "timestamp": int(time.time() * 1000),
            "_runVersion": (run_snapshot or {}).get("version", 0),
            "_taskVersion": task_snapshot.get("version", 0) if task_snapshot else 0,
        },
    })


def _start_openclaw_dispatch_watchdog(
    *,
    run_id: str,
    node_execution_id: str,
    timeout_s: Optional[int] = None,
):
    if not run_id or not node_execution_id:
        return

    if timeout_s is None:
        timeout_s = OPENCLAW_DISPATCH_ACK_TIMEOUT_S
    _cancel_openclaw_dispatch_watchdog(node_execution_id)

    async def _monitor():
        try:
            await asyncio.sleep(timeout_s)
            run_snapshot = get_run_store().get_run(run_id)
            node_snapshot = get_node_execution_store().get_node_execution(node_execution_id)
            if not run_snapshot or not node_snapshot:
                return
            if str(run_snapshot.get("status") or "").strip().lower() != "running":
                return
            if str(node_snapshot.get("status") or "").strip().lower() not in ("queued", "running"):
                return
            active_ids = _normalize_string_list(run_snapshot.get("active_node_execution_ids", []))
            current_node_id = str(run_snapshot.get("current_node_execution_id", "") or "")
            if node_execution_id not in active_ids and current_node_id != node_execution_id:
                return
            node_label = str(node_snapshot.get("node_label") or node_snapshot.get("node_key") or node_execution_id)
            peer_count = max(0, len(connected_clients) - 1)
            summary = (
                f"OpenClaw Direct Mode dispatch timed out: node '{node_label}' "
                f"received no OpenClaw ack/progress within {timeout_s}s. "
                f"Connected peer clients={peer_count}. "
                f"Connect the OpenClaw runtime client to Evermind backend or switch runtime to local."
            )
            await _fail_openclaw_dispatch(
                run_id=run_id,
                node_execution_id=node_execution_id,
                summary=summary,
                risks=[
                    "No OpenClaw runtime acknowledged the dispatched node.",
                    "Direct Mode requires a separate OpenClaw execution client on the Evermind websocket.",
                ],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[OpenClaw] Dispatch watchdog failed for node %s: %s",
                node_execution_id,
                exc,
            )
        finally:
            existing = _openclaw_dispatch_watchdogs.get(node_execution_id)
            if existing is asyncio.current_task():
                _openclaw_dispatch_watchdogs.pop(node_execution_id, None)

    _openclaw_dispatch_watchdogs[node_execution_id] = asyncio.create_task(_monitor())


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


def _reconcile_orphaned_running_run(
    run_id: str,
    *,
    now: Optional[float] = None,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Repair stale run projections when the run is still marked running but no live node
    is actually active anymore.
    """
    if not run_id:
        return None
    rs = get_run_store()
    ns = get_node_execution_store()
    run = rs.get_run(run_id)
    if not run or str(run.get("status") or "").strip().lower() != "running":
        return run

    all_nes = ns.list_node_executions(run_id=run_id) or []
    if not all_nes:
        return run

    running_ids = [str(ne.get("id") or "") for ne in all_nes if str(ne.get("status") or "").strip().lower() == "running"]
    running_ids = [node_id for node_id in running_ids if node_id]
    active_ids = _normalize_string_list(run.get("active_node_execution_ids", []))
    current_node_id = str(run.get("current_node_execution_id", "") or "")
    desired_current = running_ids[-1] if running_ids else ""

    if running_ids != active_ids or desired_current != current_node_id:
        run = rs.update_run(run_id, {
            "active_node_execution_ids": running_ids,
            "current_node_execution_id": desired_current,
        }) or rs.get_run(run_id) or run

    if running_ids:
        return run

    now_ts = float(time.time() if now is None else now)
    last_update_candidates = []
    for ne in all_nes:
        status = str(ne.get("status") or "").strip().lower()
        if status in {"passed", "skipped", "failed", "cancelled"}:
            last_update_candidates.append(float(ne.get("updated_at", 0) or 0))
            last_update_candidates.append(float(ne.get("ended_at", 0) or 0))
        elif status == "running":
            last_update_candidates.append(float(ne.get("updated_at", 0) or 0))
            last_update_candidates.append(float(ne.get("started_at", 0) or 0))
        elif status in {"blocked", "waiting_approval"}:
            last_update_candidates.append(float(ne.get("updated_at", 0) or 0))
    last_update = max(last_update_candidates) if last_update_candidates else 0.0
    if not force and last_update > 0 and (now_ts - last_update) < ORPHANED_RUNNING_RUN_STALE_S:
        return run

    statuses = [str(ne.get("status") or "").strip().lower() for ne in all_nes]
    terminal = {"passed", "skipped", "failed", "cancelled"}
    next_status = ""
    if all(status in terminal for status in statuses):
        if all(status == "cancelled" for status in statuses):
            next_status = "cancelled"
        elif any(status == "failed" for status in statuses):
            next_status = "failed"
        else:
            next_status = "done"
    elif any(status == "failed" for status in statuses):
        next_status = "failed"

    if not next_status:
        return run

    logger.info(
        "[RunReconcile] Recovered stale run %s -> %s (no active nodes; node statuses=%s)",
        run_id,
        next_status,
        statuses,
    )
    _transition_run_if_needed(run_id, next_status)
    summary = str(run.get("summary") or "").strip()
    if not summary:
        summary = (
            "Recovered stale running run after active node tracking stopped."
            if next_status == "done"
            else "Recovered stale running run: no active nodes remained after upstream failure."
            if next_status == "failed"
            else "Recovered stale running run after all nodes were cancelled."
        )
        run = rs.update_run(run_id, {"summary": summary}) or rs.get_run(run_id) or run
    return rs.get_run(run_id) or run


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
    ns = get_node_execution_store()
    TERMINAL_STATUSES = {"passed", "skipped", "failed", "cancelled"}
    SUCCESS_STATUSES = {"passed", "skipped"}

    def _is_join_node(node_key: str) -> bool:
        lower = str(node_key or "").strip().lower()
        return lower in {"reviewer", "deployer", "tester"} or lower.startswith(("reviewer_", "deployer_", "tester_"))

    MAX_CHAIN_ITERATIONS = 50
    for _chain_iter in range(MAX_CHAIN_ITERATIONS):
        nes = ns.list_node_executions(run_id=run_id)
        if not nes:
            return None

        key_status: Dict[str, str] = {}
        for ne in nes:
            k = ne.get("node_key", "")
            if k:
                key_status[k] = ne.get("status", "queued")

        ready: list[str] = []
        blocked: list[str] = []

        for ne in nes:
            if ne.get("status") != "queued":
                continue
            deps = ne.get("depends_on_keys") or []
            if not deps:
                ready.append(ne["id"])
                continue

            dep_statuses = [key_status.get(d, "") for d in deps]
            if all(status in SUCCESS_STATUSES for status in dep_statuses):
                ready.append(ne["id"])
                continue

            if any(status not in TERMINAL_STATUSES for status in dep_statuses):
                continue

            failed_or_terminal = [d for d in deps if key_status.get(d) not in SUCCESS_STATUSES]
            if failed_or_terminal:
                blocked.append(ne["id"])

        if ready:
            return ready

        if blocked:
            cancelled_any = False
            for blocked_id in blocked:
                transitioned = ns.transition_node(blocked_id, "cancelled")
                if transitioned.get("success"):
                    cancelled_any = True
                    ns.update_node_execution(blocked_id, {
                        "error_message": "Blocked by failed dependencies.",
                    })
            if not cancelled_any:
                logger.warning("_auto_chain_next_node: blocked nodes could not be cancelled, breaking loop")
                return None
            continue

        statuses = {ne.get("status") for ne in nes}
        if statuses <= TERMINAL_STATUSES:
            return "__ALL_DONE__"

        return None

    logger.error("_auto_chain_next_node: exceeded %d iterations for run %s", MAX_CHAIN_ITERATIONS, run_id)
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


_DISPATCH_MERGER_LIKE_RE = re.compile(
    r"\b(?:final merger|merger|integrator|integration|assemble|assembly|merge)\b",
    re.IGNORECASE,
)


def _dispatch_goal_wants_multi_page(goal: str) -> bool:
    text = str(goal or "").strip()
    if not text:
        return False
    try:
        import task_classifier as _tc

        return bool(_tc.wants_multi_page(text))
    except Exception:
        return False


def _dispatch_node_is_merger_like(node_snapshot: Optional[Dict[str, Any]]) -> bool:
    haystack = "\n".join(
        str((node_snapshot or {}).get(field) or "")
        for field in ("node_key", "node_label", "input_summary")
    )
    return bool(_DISPATCH_MERGER_LIKE_RE.search(haystack))


def _dispatch_builder_lane_profile(
    run_id: str,
    node_execution_id: str,
    *,
    goal: str = "",
) -> Dict[str, Any]:
    ne_snapshot = get_node_execution_store().get_node_execution(node_execution_id) or {}
    raw_key = str(ne_snapshot.get("node_key") or "").strip()
    normalized = normalize_node_role(raw_key) or raw_key.lower()
    merger_like = normalized == "merger" or _dispatch_node_is_merger_like(ne_snapshot)
    if normalized != "builder" and not merger_like:
        return {}

    run_nodes = sorted(
        get_node_execution_store().list_node_executions(run_id=run_id) or [],
        key=lambda item: (_coerce_float(item.get("created_at"), 0.0), str(item.get("id") or "")),
    )
    builder_nodes = [
        item
        for item in run_nodes
        if normalize_node_role(str(item.get("node_key") or "").strip()) == "builder"
        and not _dispatch_node_is_merger_like(item)
    ]
    builder_keys = [
        str(item.get("node_key") or "").strip()
        for item in builder_nodes
        if str(item.get("node_key") or "").strip()
    ]
    primary_builder = builder_nodes[0] if builder_nodes else None
    primary_builder_key = str((primary_builder or {}).get("node_key") or "").strip()
    task_type = _goal_task_type(goal)
    multi_page_goal = _dispatch_goal_wants_multi_page(goal)
    merger_present = any(
        normalize_node_role(str(item.get("node_key") or "").strip()) == "merger"
        or _dispatch_node_is_merger_like(item)
        for item in run_nodes
    )
    depends_on_keys = [
        str(dep).strip()
        for dep in (ne_snapshot.get("depends_on_keys") or [])
        if str(dep).strip()
    ]
    patch_mode = bool(
        normalized == "builder"
        and not merger_like
        and len(builder_nodes) > 1
        and task_type == "game"
        and not multi_page_goal
        and not merger_present
        and primary_builder_key
        and str(ne_snapshot.get("id") or "") != str((primary_builder or {}).get("id") or "")
        and primary_builder_key in depends_on_keys
    )

    if merger_like:
        can_write_root_index = True
        lane_role = "merger"
    elif len(builder_nodes) <= 1:
        can_write_root_index = True
        lane_role = "primary"
    elif patch_mode:
        can_write_root_index = True
        lane_role = "patch"
    else:
        can_write_root_index = str(ne_snapshot.get("id") or "") == str((primary_builder or {}).get("id") or "")
        lane_role = "primary" if can_write_root_index else "support"

    allowed_html_targets = ["index.html"] if can_write_root_index and not multi_page_goal else []
    return {
        "lane_role": lane_role,
        "task_type": task_type,
        "can_write_root_index": can_write_root_index,
        "allowed_html_targets": allowed_html_targets,
        "builder_merger_like": merger_like,
        "builder_patch_mode": patch_mode,
        "primary_builder_key": primary_builder_key,
        "parallel_builder_keys": builder_keys[:4],
        "depends_on_keys": depends_on_keys[:8],
        "merger_present": merger_present,
    }


def _dispatch_builder_runtime_contract(profile: Optional[Dict[str, Any]]) -> str:
    details = profile if isinstance(profile, dict) else {}
    lane_role = str(details.get("lane_role") or "").strip().lower()
    if not lane_role:
        return ""

    output_root = str(OUTPUT_DIR)
    task_type = str(details.get("task_type") or "").strip().lower()
    primary_builder_key = str(details.get("primary_builder_key") or "Builder 1").strip()
    peer_builder_keys = [
        str(item).strip()
        for item in (details.get("parallel_builder_keys") or [])
        if str(item).strip()
    ]

    lines: List[str]
    if lane_role == "support":
        lines = [
            "[BUILDER RUNTIME SUPPORT CONTRACT]",
            "This builder is a support lane for the current run.",
            f"Do NOT emit or overwrite {output_root}/index.html in this run.",
            (
                f"Write browser-native support artifacts only under {output_root}/, such as "
                f"{output_root}/js/weaponSystem.js, {output_root}/css/hud.css, or {output_root}/data/encounters.json."
            ),
            "Support JS must be browser-native and must not use CommonJS exports or require(...).",
        ]
        if primary_builder_key:
            lines.append(
                f"{primary_builder_key} owns the shipped root artifact; merger will integrate retained support files."
            )
        return "\n".join(lines)

    if lane_role == "merger":
        lines = [
            "[BUILDER RUNTIME MERGER CONTRACT]",
            "This builder is the final merger/integrator for the current run.",
            f"First inspect {output_root}/index.html and every non-empty local JS/CSS/JSON support file before editing.",
            "Integrate retained support work into the shipped root artifact. Do NOT leave support files unwired or undeployed.",
            "Ship one playable browser entry artifact for reviewer/deployer and avoid blank-screen or endless-loader regressions.",
        ]
    elif lane_role == "patch":
        lines = [
            "[BUILDER RUNTIME PATCH CONTRACT]",
            "This builder is a sequential root-patch lane for the current run.",
            f"Patch the existing {output_root}/index.html instead of starting from scratch.",
            "Preserve the already-working gameplay loop and only refine the assigned fixes in place.",
        ]
    else:
        lines = [
            "[BUILDER RUNTIME PRIMARY CONTRACT]",
            "This builder owns the shipped root artifact for the current run.",
            f"Deliver and preserve {output_root}/index.html as the playable root entry.",
            "Do NOT ship a blank screen, endless loader, or unwired controls/camera shell.",
        ]
        peer_support_keys = [key for key in peer_builder_keys if key != primary_builder_key]
        if peer_support_keys:
            lines.append(
                f"{', '.join(peer_support_keys[:2])} own support work and must not be overwritten from this lane."
            )
        if bool(details.get("merger_present")):
            lines.append("A downstream merger will integrate retained support files before review/deploy.")

    if task_type == "game":
        lines.append(
            "Game guardrails: keep controls non-mirrored, upward mouse drag looks up, crosshair visible, and projectile traces readable."
        )
    return "\n".join(lines)


def _append_dispatch_runtime_contract(input_summary: str, profile: Optional[Dict[str, Any]]) -> str:
    summary = str(input_summary or "").strip()
    contract = _dispatch_builder_runtime_contract(profile)
    if not contract:
        return summary
    header = contract.splitlines()[0].strip()
    if header and header in summary:
        return summary
    if not summary:
        return contract
    return f"{summary}\n\n{contract}".strip()


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
    goal_text = task_snapshot.get("description", task_snapshot.get("title", "")) if task_snapshot else ""
    dispatch_builder_profile = _dispatch_builder_lane_profile(
        run_id,
        node_execution_id,
        goal=goal_text,
    )
    dispatch_input_summary = _append_dispatch_runtime_contract(
        ne_snapshot.get("input_summary", ""),
        dispatch_builder_profile,
    )
    runtime_contract = _dispatch_builder_runtime_contract(dispatch_builder_profile)

    payload: Dict[str, Any] = {
        "runId": run_id,
        "taskId": task_id,
        "taskStatus": task_snapshot.get("status", "") if task_snapshot else "",
        "runStatus": run_snapshot.get("status", ""),
        "runtime": run_snapshot.get("runtime", ""),
        "workflowTemplateId": run_snapshot.get("workflow_template_id", ""),
        "sessionId": task_snapshot.get("session_id", "") if task_snapshot else "",
        "goal": goal_text,
        "activeNodeExecutionIds": run_snapshot.get("active_node_execution_ids", []),
        "nodeExecutionId": node_execution_id,
        "nodeKey": ne_snapshot.get("node_key", ""),
        "nodeLabel": ne_snapshot.get("node_label", ""),
        "inputSummary": dispatch_input_summary,
        "taskDescription": dispatch_input_summary,
        "assignedModel": ne_snapshot.get("assigned_model", ""),
        "loadedSkills": ne_snapshot.get("loaded_skills", []),
        "_neVersion": ne_snapshot.get("version", 0),
        "_runVersion": run_snapshot.get("version", 0),
        "_taskVersion": task_snapshot.get("version", 0) if task_snapshot else 0,
    }
    depends_on_keys = ne_snapshot.get("depends_on_keys", [])
    if isinstance(depends_on_keys, list) and depends_on_keys:
        payload["dependsOnKeys"] = depends_on_keys
        payload["depends_on_keys"] = depends_on_keys
    if runtime_contract:
        payload["runtimeContract"] = runtime_contract
        payload["runtime_contract"] = runtime_contract
    if dispatch_builder_profile:
        payload["taskType"] = dispatch_builder_profile.get("task_type", "")
        payload["task_type"] = dispatch_builder_profile.get("task_type", "")
        payload["builderLaneRole"] = dispatch_builder_profile.get("lane_role", "")
        payload["builder_lane_role"] = dispatch_builder_profile.get("lane_role", "")
        payload["canWriteRootIndex"] = bool(dispatch_builder_profile.get("can_write_root_index"))
        payload["can_write_root_index"] = bool(dispatch_builder_profile.get("can_write_root_index"))
        payload["allowedHtmlTargets"] = list(dispatch_builder_profile.get("allowed_html_targets") or [])
        payload["allowed_html_targets"] = list(dispatch_builder_profile.get("allowed_html_targets") or [])
        payload["builderMergerLike"] = bool(dispatch_builder_profile.get("builder_merger_like"))
        payload["builder_merger_like"] = bool(dispatch_builder_profile.get("builder_merger_like"))
        payload["builderPatchMode"] = bool(dispatch_builder_profile.get("builder_patch_mode"))
        payload["builder_patch_mode"] = bool(dispatch_builder_profile.get("builder_patch_mode"))
        payload["parallelBuilderKeys"] = list(dispatch_builder_profile.get("parallel_builder_keys") or [])
        payload["parallel_builder_keys"] = list(dispatch_builder_profile.get("parallel_builder_keys") or [])
        payload["primaryBuilderKey"] = str(dispatch_builder_profile.get("primary_builder_key") or "")
        payload["primary_builder_key"] = str(dispatch_builder_profile.get("primary_builder_key") or "")
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
# Mount the active runtime output directory directly. A compatibility alias at
# /tmp/evermind_output is maintained separately for older prompts / tooling.
# ─────────────────────────────────────────────
app.mount("/uploads", StaticFiles(directory=str(CHAT_UPLOADS_DIR), html=False), name="uploads")


# v7.1f (maintainer): NO-CACHE wrapper for /preview.
# Without explicit Cache-Control, Starlette's StaticFiles only sends
# ETag + Last-Modified. Browsers (incl. Playwright used by reviewer/
# tester) heuristically cache linked sub-resources (`src/shared/*.js`,
# `*.css`) between navigations — so when patcher edits nav.js and the
# reviewer re-navigates with a fresh `?t=` buster on index.html, the
# linked JS still serves from browser cache → reviewer audits STALE
# code. This was the user's concern (2026-04-24): "reviewer 审查的不是
# 最新的代码". Fix: force `Cache-Control: no-store` on every /preview
# response so Playwright always fetches the latest disk state.
class _NoStoreStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        try:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        except Exception:
            pass
        return response


app.mount("/preview", _NoStoreStaticFiles(directory=str(OUTPUT_DIR), html=True), name="preview")


def _is_previewable_html(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in (".html", ".htm")
        and not is_partial_html_artifact(path)
        and not is_bootstrap_html_artifact(path)
    )


def _pick_primary_preview_html(paths: List[Path], *, bucket_root: Optional[Path] = None) -> Optional[Path]:
    html_candidates = [path for path in paths if _is_previewable_html(path)]
    if not html_candidates:
        return None

    root = None
    if bucket_root is not None:
        try:
            root = bucket_root.resolve()
        except Exception:
            root = bucket_root

    def _sort_key(path: Path) -> tuple[int, int, str]:
        try:
            rel = path.resolve().relative_to(root) if root is not None else path
        except Exception:
            rel = path
        rel_str = rel.as_posix() if isinstance(rel, Path) else str(rel)
        depth = len(rel.parts) if isinstance(rel, Path) else len(Path(rel_str).parts)
        return (
            0 if path.name.lower() == "index.html" else 1,
            depth,
            rel_str.lower(),
        )

    html_candidates.sort(key=_sort_key)
    return html_candidates[0]


def _list_bucket_files(bucket_dir: Path) -> List[Path]:
    files: List[Path] = []
    try:
        items = sorted(bucket_dir.iterdir(), key=lambda p: p.name.lower())
    except Exception:
        return files
    for item in items:
        if not item.is_file():
            continue
        if item.suffix.lower() in (".html", ".htm") and (is_partial_html_artifact(item) or is_bootstrap_html_artifact(item)):
            continue
        files.append(item)
    return files


def _latest_bucket_mtime(paths: List[Path]) -> float:
    latest = 0.0
    for path in paths:
        try:
            latest = max(latest, path.stat().st_mtime)
        except Exception:
            continue
    return latest


@app.get("/api/capabilities")
async def capabilities_manifest():
    """v5.8.6: capability manifest for external assistants (OpenClaw, Claude Code,
    Codex, custom chat) that want to drive Evermind intelligently.

    An assistant calls this once on connect and gets everything it needs to
    orchestrate Evermind: the agent roster, browser tool actions, workflow
    templates, the WS goal-submission schema, and the model routing fallback
    chain. This replaces the "assistant has to learn Evermind by trial and
    error" scenario — now a fresh assistant can generate correct workflows on
    its first try.
    """
    return {
        "name": "Evermind",
        "version": "5.8.6",
        "schema": "https://evermind.dev/capabilities/v1",
        "agents": {
            "router":      {"role": "Classify goal + emit JSON plan", "out": "JSON subtasks[]"},
            "planner":     {"role": "Deep execution blueprint (modules, contracts, ownership)", "out": "JSON + short prose"},
            "analyst":     {"role": "Research references + 8-section handoff XML", "out": "XML handoff sections"},
            "imagegen":    {"role": "Character / weapon / env modeling design packs", "out": "brief files"},
            "spritesheet": {"role": "Sprite frame / animation config + runtime JS", "out": "sprites.js + sprite_config.json"},
            "assetimport": {"role": "Asset manifest + loader JS", "out": "loader.js + manifest.json"},
            "uidesign":    {"role": "Visual design direction + design system", "out": "design brief"},
            "scribe":      {"role": "Content architecture + copy handoff", "out": "content-architecture.md"},
            "builder":     {"role": "Produce the actual HTML/CSS/JS product", "out": "index.html + modules"},
            "polisher":    {"role": "Premium UI / motion / visual polish on existing artifacts", "out": "patched files"},
            "merger":      {"role": "Integrate parallel builder outputs into one cohesive product", "out": "merged index.html"},
            "reviewer":    {"role": "Strict quality gate (8-dimension scoring + JSON verdict)", "out": "review report + verdict"},
            "tester":      {"role": "Run interaction tests in the embedded Chromium", "out": "test report"},
            "debugger":    {"role": "Pinpoint + fix runtime errors found by reviewer/tester", "out": "patched files"},
            "deployer":    {"role": "List artifacts + publish preview URL", "out": "file manifest + preview_url"},
        },
        "browser": {
            "cdp_endpoint": os.getenv("EVERMIND_BROWSER_CDP_URL", "http://127.0.0.1:19222"),
            "note": "The browser tool attaches to Evermind's embedded Electron Chromium via CDP. "
                    "All actions are visible to the user (floating AI cursor + ripple on click).",
            "actions": [
                "navigate", "observe", "snapshot", "act", "click", "fill", "extract",
                "scroll", "record_scroll", "press", "press_sequence", "wait_for",
                "find", "hover", "select", "upload", "new_tab", "switch_tab", "close_tab",
                "evaluate", "close_popups", "network_idle",
                "mouse_click", "mouse_move", "mouse_down", "mouse_up", "drag", "wheel",
                "key_down", "key_up", "key_hold", "type_text", "macro", "canvas_click",
                "screenshot_region",
            ],
            "vision_grounding": "Screenshots that include a snapshot are auto-annotated with "
                                "numbered boxes — every interactive element has an [index] that matches "
                                "its `ref` field in the snapshot text. VLMs can click by index.",
        },
        "tools": {
            "file_ops": "Read / write / list / patch files inside /tmp/evermind_output/",
            "shell":    "Bash one-shot with cwd = output dir; no persistent session",
            "source_fetch": "Fetch raw source from URLs (GitHub raw, docs) without a browser",
            "browser":  "Full-featured embedded Chromium automation (see `browser` block)",
            "browser_use": "High-level agentic multi-step browser sidecar (for gameplay QA)",
            "comfyui":  "ComfyUI image generation (if a local backend is configured)",
        },
        "submit_run": {
            "protocol": "WebSocket",
            "endpoint": "ws://127.0.0.1:8765/ws",
            "goal_frame": {
                "type": "run_goal",
                "goal": "<natural-language goal>",
                "difficulty": "simple | standard | pro",
                "runtime": "local | openclaw",
                "session_id": "<optional, to reuse session memory>",
                "conversation_history": [{"role": "user", "content": "..."}],
            },
            "ack_frame": "run_goal_ack  →  { runId, taskId, nodeExecutions[] }",
            "progress_events": [
                "subtask_start", "subtask_progress", "subtask_done",
                "openclaw_node_update", "plan_created", "preview_ready",
                "orchestrator_complete",
            ],
        },
        "model_routing": {
            "primary": "kimi-k2.6-code-preview",
            "fallback_chain": ["kimi-k2.6-code-preview", "deepseek-v3", "qwen-max",
                               "gemini-2.0-flash", "glm-4-plus",
                               "gpt-5.4", "gpt-5.3-codex", "claude-4-sonnet", "gemini-2.5-pro"],
            "multi_key_pool": "Each provider can hold primary + secondary API key "
                              "(e.g. kimi_api_key + kimi_api_key_2); the bridge round-robins "
                              "and per-key cooldowns on 401/429.",
        },
        "workflow_templates": {
            "simple":   ["router", "builder", "deployer"],
            "standard": ["router", "planner", "builder", "reviewer", "deployer"],
            "pro":      ["router", "planner", "analyst", "imagegen", "spritesheet",
                         "assetimport", "builder1", "builder2", "merger",
                         "reviewer", "deployer", "tester", "debugger"],
        },
        "tips_for_assistants": [
            "Always start with `router` or a pre-built template — don't hand-craft DAGs unless the user explicitly asks.",
            "For a 3D game, pro template is correct; for a static landing page, standard is enough.",
            "Builder 1 and Builder 2 are PEERS (both write HTML staging dirs); merger combines them. "
            "Don't assume one is primary.",
            "Reviewer uses the embedded browser — expect 60-120s of browser automation per run.",
            "If the user asks \"why did reviewer reject?\", read `pipeline_results[*].subtasks[*].work_summary` "
            "and the reviewer's blocking_issues JSON.",
        ],
    }


@app.get("/api/preview/list")
async def preview_list():
    """List generated artifacts, including task folders and output root files."""
    tasks = []
    if OUTPUT_DIR.exists():
        # task_x directories
        for task_dir in OUTPUT_DIR.iterdir():
            if not (task_dir.is_dir() and task_dir.name.startswith("task_")):
                continue
            bucket_files = _list_bucket_files(task_dir)
            if not bucket_files:
                continue
            html_path = _pick_primary_preview_html(bucket_files, bucket_root=task_dir)
            tasks.append({
                "task_id": task_dir.name,
                "files": [{"name": f.name, "size": f.stat().st_size} for f in bucket_files],
                "html_file": html_path.name if html_path else None,
                "preview_url": f"/preview/{task_dir.name}/{html_path.name}" if html_path else None,
                "_mtime": _latest_bucket_mtime(bucket_files),
            })

        # root-level artifacts (builder may write directly to the runtime output root)
        root_files = _list_bucket_files(OUTPUT_DIR)
        root_html = _pick_primary_preview_html(root_files, bucket_root=OUTPUT_DIR)
        if root_files:
            tasks.append({
                "task_id": "root",
                "files": [{"name": f.name, "size": f.stat().st_size} for f in root_files],
                "html_file": root_html.name if root_html else None,
                "preview_url": f"/preview/{root_html.name}" if root_html else None,
                "_mtime": _latest_bucket_mtime(root_files),
            })

        tasks.sort(key=lambda item: item.get("_mtime", 0.0), reverse=True)
        for item in tasks:
            item.pop("_mtime", None)

    return {"tasks": tasks, "output_dir": str(OUTPUT_DIR)}


@app.get("/api/preview/streaming/{task_id}")
async def preview_streaming(task_id: str):
    """v6.1.9 — serve the Wave-A `.streaming` staging file while the builder
    is mid-flight. Frontend polls / reloads this when a `stream_flush` event
    lands so users see the page materialize in real time.

    Falls back to the final `index.html` when the staging file no longer
    exists (build completed) so the client can use a single URL throughout.
    """
    tid = task_id if task_id.startswith("task_") else f"task_{task_id}"
    task_dir = OUTPUT_DIR / tid
    staging = task_dir / "index.html.streaming"
    final = task_dir / "index.html"
    if staging.exists():
        return FileResponse(
            staging,
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-store, must-revalidate",
                "X-Preview-Stage": "streaming",
            },
        )
    if final.exists():
        return FileResponse(
            final,
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "X-Preview-Stage": "final",
            },
        )
    return JSONResponse(
        status_code=404,
        content={"error": f"No artifact yet for {tid}"},
    )


@app.get("/api/preview/{task_id}")
async def preview_task(task_id: str):
    """Get preview info for a specific task, including the URL to its main HTML file."""
    task_dir = OUTPUT_DIR / task_id
    if not task_dir.exists() or not task_dir.is_dir():
        return JSONResponse(status_code=404, content={"error": f"Task {task_id} not found"})
    bucket_files = _list_bucket_files(task_dir)
    html_path = _pick_primary_preview_html(bucket_files, bucket_root=task_dir)
    base_url = f"http://127.0.0.1:{os.getenv('PORT', '8765')}"
    preview_url = f"{base_url}/preview/{task_id}/{html_path.name}" if html_path else None
    return {
        "task_id": task_id,
        "files": [{"name": f.name, "size": f.stat().st_size} for f in bucket_files],
        "html_file": html_path.name if html_path else None,
        "preview_url": preview_url,
        "full_preview_url": preview_url,
    }


# ─────────────────────────────────────────────
# Workspace File Explorer API
# ─────────────────────────────────────────────
_WORKSPACE_TREE_MAX_DEPTH = 8
_WORKSPACE_TREE_MAX_FILES = 800
_WORKSPACE_FILE_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
_WORKSPACE_TEXT_EXTENSIONS = {
    ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".md", ".txt", ".py", ".yaml", ".yml", ".xml",
    ".svg", ".csv", ".sh", ".bat", ".log", ".env", ".cfg",
    ".toml", ".ini", ".conf", ".gitignore", ".sql",
}
_WORKSPACE_SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".DS_Store", "_visual_regression", "_stable_previews", ".next", "dist", "build", ".cache"}
_WORKSPACE_ALLOWED_ROOTS: List[str] = []  # populated from settings or env
_ARTIFACT_SYNC_MANIFEST = ".evermind-artifact-sync.json"


def _is_safe_workspace_root(target: Path) -> bool:
    """Security: only allow browsing under HOME, Desktop, Documents, or OUTPUT_DIR."""
    resolved = target.resolve()
    home = Path.home().resolve()
    safe_roots = [
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "Projects",
        home,
        OUTPUT_DIR.resolve(),
        Path("/tmp").resolve(),
    ]
    for allowed in _WORKSPACE_ALLOWED_ROOTS:
        try:
            safe_roots.append(Path(allowed).resolve())
        except Exception:
            pass
    return any(
        resolved == safe_root or str(resolved).startswith(str(safe_root) + "/")
        for safe_root in safe_roots
    )


def _build_tree(root: Path, *, depth: int = 0, counter: list) -> List[Dict[str, Any]]:
    """Recursively build a directory tree, respecting depth and file count limits."""
    if depth > _WORKSPACE_TREE_MAX_DEPTH or counter[0] >= _WORKSPACE_TREE_MAX_FILES:
        return []
    entries: List[Dict[str, Any]] = []
    try:
        items = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except (PermissionError, OSError):
        return []
    for item in items:
        if item.name in _WORKSPACE_SKIP_DIRS or item.name.startswith("."):
            continue
        if counter[0] >= _WORKSPACE_TREE_MAX_FILES:
            break
        if item.is_dir():
            children = _build_tree(item, depth=depth + 1, counter=counter)
            # Include directories even if empty (like VS Code)
            entries.append({
                "name": item.name,
                "type": "directory",
                "children": children,
            })
        elif item.is_file():
            counter[0] += 1
            try:
                stat = item.stat()
                entries.append({
                    "name": item.name,
                    "type": "file",
                    "size": stat.st_size,
                    "ext": item.suffix.lower(),
                    "mtime": int(stat.st_mtime),
                })
            except Exception:
                pass
    return entries


def _current_workspace_roots() -> Dict[str, str]:
    settings = load_settings()
    workspace = str(settings.get("workspace", "") or "").strip()
    artifact_sync_dir = str(settings.get("artifact_sync_dir", "") or "").strip()
    return {
        "workspace": workspace,
        "artifact_sync_dir": artifact_sync_dir,
        "output_dir": str(OUTPUT_DIR),
        "output_alias": str(LEGACY_OUTPUT_ALIAS),
    }


def _resolve_sync_source_root(preview_url: str = "") -> Path:
    preview_file = resolve_preview_file(preview_url, OUTPUT_DIR) if preview_url else None
    if preview_file is None or not preview_file.exists():
        return OUTPUT_DIR

    try:
        rel = preview_file.resolve().relative_to(OUTPUT_DIR.resolve())
    except Exception:
        return preview_file.parent

    if rel.parts and rel.parts[0] == "_stable_previews" and len(rel.parts) >= 3:
        return OUTPUT_DIR / rel.parts[0] / rel.parts[1] / rel.parts[2]
    if rel.parts and rel.parts[0].startswith("task_"):
        return OUTPUT_DIR / rel.parts[0]
    return OUTPUT_DIR


def _should_sync_artifact(path: Path, *, source_root: Path) -> bool:
    try:
        rel = path.resolve().relative_to(source_root.resolve())
    except Exception:
        return False
    if not rel.parts:
        return False
    if any(part.startswith(".") for part in rel.parts):
        return False
    if any(part in _WORKSPACE_SKIP_DIRS for part in rel.parts):
        return False
    if source_root.resolve() == OUTPUT_DIR.resolve() and rel.parts[0].startswith("task_"):
        return False
    if path.is_file() and path.suffix.lower() in (".html", ".htm"):
        if is_partial_html_artifact(path) or is_bootstrap_html_artifact(path):
            return False
    return True


def _read_sync_manifest(target_dir: Path) -> List[str]:
    manifest_path = target_dir / _ARTIFACT_SYNC_MANIFEST
    if not manifest_path.exists():
        return []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    files = payload.get("files", []) if isinstance(payload, dict) else []
    return [str(item) for item in files if str(item).strip()]


def _write_sync_manifest(target_dir: Path, *, files: List[str], source_root: Path, preview_url: str) -> None:
    manifest_path = target_dir / _ARTIFACT_SYNC_MANIFEST
    payload = {
        "source_root": str(source_root),
        "preview_url": preview_url,
        "synced_at": int(time.time() * 1000),
        "files": sorted(set(files)),
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sync_output_to_target(target_dir: Path, *, preview_url: str = "") -> Dict[str, Any]:
    source_root = _resolve_sync_source_root(preview_url)
    source_root.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    files_to_copy: List[Path] = []
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        if not _should_sync_artifact(path, source_root=source_root):
            continue
        files_to_copy.append(path)

    copied_rel_paths: List[str] = []
    for src in files_to_copy:
        rel = src.resolve().relative_to(source_root.resolve())
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied_rel_paths.append(rel.as_posix())

    previous_manifest = _read_sync_manifest(target_dir)
    current_set = set(copied_rel_paths)
    for rel_str in previous_manifest:
        if rel_str in current_set:
            continue
        stale = (target_dir / rel_str).resolve()
        try:
            stale.relative_to(target_dir.resolve())
        except Exception:
            continue
        if stale.exists() and stale.is_file():
            stale.unlink()
            parent = stale.parent
            while parent != target_dir and parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    _write_sync_manifest(target_dir, files=copied_rel_paths, source_root=source_root, preview_url=preview_url)
    return {
        "success": True,
        "source_root": str(source_root),
        "target_dir": str(target_dir),
        "copied_files": len(copied_rel_paths),
        "files": copied_rel_paths[:200],
        "preview_url": preview_url,
    }


@app.get("/api/workspace/roots")
async def workspace_roots():
    roots = _current_workspace_roots()
    folders = [
        {
            "path": roots["output_dir"],
            "label": "Current Run Output",
            "kind": "runtime_output",
            "removable": False,
        },
    ]
    if roots["artifact_sync_dir"]:
        folders.append({
            "path": roots["artifact_sync_dir"],
            "label": "Delivery Folder",
            "kind": "artifact_sync",
            "removable": False,
        })
    return {
        **roots,
        "folders": folders,
    }


@app.get("/api/workspace/tree")
async def workspace_tree(root: str = ""):
    """Return a recursive directory tree.  Accepts ?root=/path/to/folder."""
    target = Path(root).resolve() if root.strip() else OUTPUT_DIR
    if not target.exists() or not target.is_dir():
        return JSONResponse(status_code=404, content={"error": f"Directory not found: {root}"})
    if not _is_safe_workspace_root(target):
        return JSONResponse(status_code=403, content={"error": "Directory not allowed"})
    counter = [0]
    tree = _build_tree(target, counter=counter)
    return {"root": str(target), "tree": tree, "total_files": counter[0]}


@app.get("/api/workspace/file")
async def workspace_file(path: str = "", root: str = ""):
    """Read the content of a single workspace file. Returns text or base64 for binary."""
    import base64
    workspace_root = Path(root).resolve() if root.strip() else OUTPUT_DIR
    rel = (path or "").strip().lstrip("/")
    if not rel:
        return JSONResponse(status_code=400, content={"error": "path parameter is required"})
    if not _is_safe_workspace_root(workspace_root):
        return JSONResponse(status_code=403, content={"error": "Root directory not allowed"})
    target = (workspace_root / rel).resolve()
    # Path traversal guard
    try:
        target.relative_to(workspace_root)
    except ValueError:
        return JSONResponse(status_code=403, content={"error": "Path outside workspace"})
    if not target.exists() or not target.is_file():
        return JSONResponse(status_code=404, content={"error": f"File not found: {rel}"})
    stat = target.stat()
    if stat.st_size > _WORKSPACE_FILE_MAX_BYTES:
        return JSONResponse(status_code=413, content={
            "error": f"File too large ({stat.st_size} bytes, max {_WORKSPACE_FILE_MAX_BYTES})",
        })
    ext = target.suffix.lower()
    is_text = ext in _WORKSPACE_TEXT_EXTENSIONS
    if is_text:
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return JSONResponse(status_code=500, content={"error": f"Read failed: {exc}"})
        return {
            "path": rel,
            "name": target.name,
            "ext": ext,
            "size": stat.st_size,
            "encoding": "utf-8",
            "content": content,
        }
    else:
        try:
            raw = target.read_bytes()
            content_b64 = base64.b64encode(raw).decode("ascii")
        except Exception as exc:
            return JSONResponse(status_code=500, content={"error": f"Read failed: {exc}"})
        return {
            "path": rel,
            "name": target.name,
            "ext": ext,
            "size": stat.st_size,
            "encoding": "base64",
            "content": content_b64,
        }


@app.post("/api/workspace/sync")
async def workspace_sync(data: Dict = Body(...)):
    payload = dict(data or {})
    roots = _current_workspace_roots()
    raw_target = str(payload.get("root") or roots.get("artifact_sync_dir") or "").strip()
    if not raw_target:
        return JSONResponse(status_code=400, content={"error": "No delivery folder configured"})
    target_dir = Path(raw_target).expanduser().resolve()
    if not _is_safe_workspace_root(target_dir):
        return JSONResponse(status_code=403, content={"error": "Delivery folder not allowed"})
    preview_url = str(payload.get("preview_url") or "").strip()
    try:
        result = _sync_output_to_target(target_dir, preview_url=preview_url)
    except Exception as exc:
        logger.warning("workspace_sync failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": f"Sync failed: {exc}"})
    return result


# ── Workspace file management (VS Code-style) ──

def _validate_workspace_path(root: str, rel_path: str) -> Path:
    """Resolve and validate a workspace path. Raises ValueError if unsafe.

    Uses ``relative_to`` after resolving symlinks: that's the canonical
    traversal check. A prefix match on strings is unsafe — ``/tmp/abc``
    is a false-positive prefix of ``/tmp/abcdef``.
    """
    root_p = Path(root).expanduser().resolve()
    if not _is_safe_workspace_root(root_p):
        raise ValueError(f"Root not allowed: {root}")
    rel_clean = str(rel_path or "").lstrip("/").strip()
    if not rel_clean or rel_clean.startswith("..") or "\x00" in rel_clean:
        raise ValueError("Invalid relative path")
    target = (root_p / rel_clean).resolve()
    try:
        target.relative_to(root_p)
    except ValueError as exc:
        raise ValueError("Path traversal detected") from exc
    return target


@app.post("/api/workspace/mkdir")
async def workspace_mkdir(data: Dict = Body(...)):
    """Create a directory in the workspace."""
    root = str(data.get("root", "")).strip()
    rel_path = str(data.get("path", "")).strip()
    if not root or not rel_path:
        return JSONResponse(status_code=400, content={"error": "root and path required"})
    try:
        target = _validate_workspace_path(root, rel_path)
        target.mkdir(parents=True, exist_ok=True)
        return {"success": True, "path": str(target)}
    except ValueError as e:
        return JSONResponse(status_code=403, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/workspace/write")
async def workspace_write(data: Dict = Body(...)):
    """Write/save file content to workspace."""
    root = str(data.get("root", "")).strip()
    file_path = str(data.get("path", "")).strip()
    content = data.get("content", "")
    if not file_path:
        return JSONResponse(status_code=400, content={"error": "path required"})
    # If no root given, try to use the file_path as absolute
    if not root:
        target = Path(file_path).expanduser().resolve()
        if not _is_safe_workspace_root(target.parent):
            return JSONResponse(status_code=403, content={"error": "Path not in allowed workspace"})
    else:
        try:
            target = _validate_workspace_path(root, file_path)
        except ValueError as e:
            return JSONResponse(status_code=403, content={"error": str(e)})
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        return {"success": True, "path": str(target), "size": target.stat().st_size}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/workspace/delete")
async def workspace_delete(data: Dict = Body(...)):
    """Delete a file or empty folder from workspace."""
    root = str(data.get("root", "")).strip()
    rel_path = str(data.get("path", "")).strip()
    if not root or not rel_path:
        return JSONResponse(status_code=400, content={"error": "root and path required"})
    try:
        target = _validate_workspace_path(root, rel_path)
    except ValueError as e:
        return JSONResponse(status_code=403, content={"error": str(e)})
    if not target.exists():
        return JSONResponse(status_code=404, content={"error": "Not found"})
    try:
        if target.is_file():
            target.unlink()
        elif target.is_dir():
            import shutil
            shutil.rmtree(str(target))
        return {"success": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/workspace/upload")
async def workspace_upload(data: Dict = Body(...)):
    """v5.6: Binary-safe file upload via JSON + base64. Chosen over multipart/form-data
    to avoid a hard python-multipart dependency across the test matrix.
    Client sends: { root, path, content_b64 } — we decode and write to disk."""
    import base64
    root = str(data.get("root", "")).strip()
    rel_path = str(data.get("path", "")).strip()
    b64 = str(data.get("content_b64", ""))
    if not root or not rel_path:
        return JSONResponse(status_code=400, content={"error": "root and path required"})
    try:
        target = _validate_workspace_path(root, rel_path)
    except ValueError as e:
        return JSONResponse(status_code=403, content={"error": str(e)})
    try:
        payload = base64.b64decode(b64, validate=False) if b64 else b""
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": f"invalid base64: {exc}"})
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return {"success": True, "path": str(target), "size": target.stat().st_size}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/workspace/rename")
async def workspace_rename(data: Dict = Body(...)):
    """Rename a file or folder in workspace."""
    root = str(data.get("root", "")).strip()
    old_path = str(data.get("old_path", "")).strip()
    new_name = str(data.get("new_name", "")).strip()
    if not root or not old_path or not new_name:
        return JSONResponse(status_code=400, content={"error": "root, old_path, and new_name required"})
    try:
        source = _validate_workspace_path(root, old_path)
    except ValueError as e:
        return JSONResponse(status_code=403, content={"error": str(e)})
    if not source.exists():
        return JSONResponse(status_code=404, content={"error": "Source not found"})
    dest = source.parent / new_name
    if dest.exists():
        return JSONResponse(status_code=409, content={"error": "Target already exists"})
    try:
        source.rename(dest)
        return {"success": True, "new_path": str(dest)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


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
    snap["runtime"]["desktop_build_id"] = DESKTOP_BUILD_ID
    snap["runtime"]["pid"] = os.getpid()
    snap["runtime"]["process_started_at"] = PROCESS_STARTED_AT
    snap["runtime"]["clients_connected"] = len(connected_clients)
    tracked_task_ids = {
        id(task)
        for tasks in _active_tasks.values()
        for task in tasks
    }
    tracked_task_ids.update(id(task) for task in _detached_tasks)
    snap["runtime"]["active_tasks"] = len(tracked_task_ids)
    snap["runtime"]["log_file"] = str(LOG_FILE)
    snap["runtime"]["browser_headful"] = coerce_bool(os.getenv("EVERMIND_BROWSER_HEADFUL", "0"), default=False)
    snap["runtime"]["reviewer_tester_force_headful"] = coerce_bool(
        os.getenv("EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL", "0"),
        default=False,
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
                if is_bootstrap_html_artifact(f):
                    continue
                try:
                    rel_parts = f.relative_to(OUTPUT_DIR).parts
                except Exception:
                    rel_parts = ()
                if rel_parts and str(rel_parts[0]).startswith("task_"):
                    task_scoped.append(f)
            html_files = [f for f in html_files if not is_bootstrap_html_artifact(f)]
            if not html_files:
                raise FileNotFoundError("No deliverable HTML artifacts found")
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


# v5.8.6: removed duplicate /api/models handler here — FastAPI first-wins
# routing made it override the newer handler at line 4805 that returns
# `has_key` per model. Without `has_key`, the frontend AgentNode dropdown
# filtered every model out (filter returns empty), so the node model
# selector appeared empty even with keys configured.
# The authoritative handler is defined below near the speed-test endpoint.


# ─────────────────────────────────────────────────────────────────────────────
# v5.5 Ecosystem APIs — Compound Engineering, MCP, GitHub Repo cache
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/compound/stats")
async def compound_stats():
    """Return aggregated lessons statistics so the UI can show how many lessons
    Evermind has learned per task type."""
    try:
        import lessons_store
        return {"ok": True, **lessons_store.stats()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@app.get("/api/compound/lessons")
async def compound_lessons(task_type: str = "", limit: int = 20):
    """Return recent lessons for a given task_type (or a sample across all)."""
    try:
        import lessons_store
        if task_type:
            return {"ok": True, "task_type": task_type, "items": lessons_store.relevant(task_type, limit=limit)}
        # aggregate: a slice from each known task_type
        stats_obj = lessons_store.stats()
        merged: List[Dict[str, Any]] = []
        for tt in (stats_obj.get("per_task_type") or {}).keys():
            merged.extend(lessons_store.relevant(tt, limit=max(1, int(limit) // max(1, len(stats_obj.get("per_task_type") or {})))))
        return {"ok": True, "items": merged[:limit], "total_stored": stats_obj.get("total", 0)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@app.get("/api/mcp/tools")
async def mcp_list_tools():
    """List all tools exposed by running MCP servers. Empty when no servers configured."""
    try:
        import mcp_client
        reg = mcp_client.registry()
        all_tools = await reg.list_all_tools()
        summary: List[Dict[str, Any]] = []
        for server_name, tools in all_tools.items():
            for t in tools:
                summary.append({
                    "server": server_name,
                    "name": t.get("name"),
                    "description": t.get("description"),
                    "input_schema": t.get("inputSchema") or t.get("input_schema"),
                })
        return {"ok": True, "count": len(summary), "tools": summary}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@app.post("/api/mcp/call")
async def mcp_call_tool(payload: Dict[str, Any] = Body(default={})):
    """Invoke a specific tool on a specific MCP server (debugging / manual use)."""
    try:
        import mcp_client
        server_name = str(payload.get("server") or "").strip()
        tool_name = str(payload.get("name") or "").strip()
        arguments = payload.get("arguments") or {}
        if not server_name or not tool_name:
            return {"ok": False, "error": "server and name are required"}
        result = await mcp_client.registry().call(server_name, tool_name, arguments if isinstance(arguments, dict) else {})
        return {"ok": True, "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:400]}


@app.get("/api/repo/cached")
async def repo_cached():
    """List GitHub repos Evermind has cached locally."""
    try:
        import repo_clone
        return {"ok": True, "repos": repo_clone.list_cached()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@app.post("/api/repo/clone")
async def repo_clone_endpoint(payload: Dict[str, Any] = Body(default={})):
    """Manually trigger a GitHub clone. Same code-path the orchestrator uses."""
    try:
        import repo_clone
        url = str(payload.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url is required"}
        path = await repo_clone.clone_or_refresh(url)
        if not path:
            return {"ok": False, "error": "clone failed — check URL, git binary, or network"}
        return {"ok": True, "path": path}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@app.get("/api/chat/history")
async def get_chat_history(session_id: str = ""):
    """Return persisted chat messages for a session."""
    sid = session_id.strip()
    if not sid:
        return {"messages": []}
    mem = _SESSION_MEMORY.get(sid)
    if not mem:
        return {"messages": []}
    return {"messages": mem.get("chat_messages", [])}


@app.post("/api/chat/attachments")
async def upload_chat_attachments(request: Request, payload: Dict[str, Any] = Body(default={})):
    session_id = _safe_session_segment(payload.get("session_id") or payload.get("sessionId") or "")
    raw_files = payload.get("files")
    files = raw_files if isinstance(raw_files, list) else []
    stored: List[Dict[str, Any]] = []
    rejected: List[Dict[str, str]] = []

    for item in files[:MAX_CHAT_ATTACHMENTS]:
        if not isinstance(item, dict):
            rejected.append({"name": "attachment", "error": "invalid payload"})
            continue
        name = _safe_filename(item.get("name") or "attachment.bin")
        mime_type = str(item.get("mime_type") or item.get("mimeType") or mimetypes.guess_type(name)[0] or "application/octet-stream")
        encoded = str(item.get("content_base64") or "").strip()
        if not encoded:
            rejected.append({"name": name, "error": "empty file payload"})
            continue
        try:
            raw_bytes = base64.b64decode(encoded, validate=True)
        except Exception:
            rejected.append({"name": name, "error": "invalid base64 payload"})
            continue
        if len(raw_bytes) <= 0:
            rejected.append({"name": name, "error": "empty file"})
            continue
        if len(raw_bytes) > MAX_CHAT_ATTACHMENT_BYTES:
            rejected.append({"name": name, "error": f"file too large ({len(raw_bytes)} bytes)"})
            continue
        try:
            stored.append(_store_chat_attachment_bytes(
                session_id=session_id,
                name=name,
                mime_type=mime_type,
                raw_bytes=raw_bytes,
            ))
        except Exception as exc:
            rejected.append({"name": name, "error": f"save failed: {exc}"})

    return {
        "sessionId": session_id,
        "attachments": [_serialize_chat_attachment(item, request) for item in stored],
        "rejected": rejected,
    }


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


from proxy_relay import get_relay_manager, relay_template_catalog, resolve_relay_template
from privacy import get_masker, update_masker_settings, BUILTIN_PATTERNS


@app.get("/api/plugins/defaults")
async def plugin_defaults():
    """Get default plugin assignments for each node type."""
    runtime_config = {
        "builder_enable_browser": is_builder_browser_enabled(),
        "image_generation": {
            "comfyui_url": str(os.getenv("EVERMIND_COMFYUI_URL", "") or "").strip(),
            "workflow_template": str(os.getenv("EVERMIND_COMFYUI_WORKFLOW_TEMPLATE", "") or "").strip(),
        },
    }
    return {
        "defaults": get_effective_default_plugins(config=runtime_config),
        "builder_enable_browser": runtime_config["builder_enable_browser"],
        "image_generation_available": is_image_generation_available(runtime_config),
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
        "desktop_build_id": DESKTOP_BUILD_ID,
        "output_dir": str(OUTPUT_DIR),
    }


@app.get("/api/skills")
async def list_skills():
    catalog = list_skill_catalog()
    return {
        "skills": catalog,
        "counts": {
            "total": len(catalog),
            "builtin": len([item for item in catalog if item.get("origin") == "builtin"]),
            "community": len([item for item in catalog if item.get("origin") == "community"]),
        },
        "community_install_enabled": True,
    }


def _build_openclaw_guide_payload() -> Dict[str, Any]:
    guide_content = ""
    guide_path = Path(__file__).parent.parent / "OPENCLAW_GUIDE.md"
    if guide_path.exists():
        try:
            guide_content = guide_path.read_text(encoding="utf-8")
        except Exception:
            guide_content = "Guide file could not be read."
    else:
        guide_content = "# OpenClaw Guide\n\nGuide file not found."

    port = os.getenv("PORT", "8765")
    api_base = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws"
    mcp_config = {
        "mcpServers": {
            "evermind": {
                "url": ws_url,
                "transport": "websocket",
                "description": "Evermind God Mode — Autonomous AI Workflow Orchestrator",
            }
        }
    }
    return {
        "guide": guide_content,
        "mcp_config": mcp_config,
        "ws_url": ws_url,
        "api_base": api_base,
        "guide_url": f"{api_base}/api/openclaw-guide",
        "deep_links": {
            "open_app": "evermind://",
            "run_goal_template": "evermind://run?goal=<urlencoded-goal>",
        },
    }


@app.post("/api/skills/install")
async def install_skill_api(data: Dict = Body(...)):
    payload = dict(data or {})
    source_url = str(payload.get("source_url") or payload.get("sourceUrl") or "").strip()
    if not source_url:
        return JSONResponse(status_code=400, content={"error": "source_url is required"})
    try:
        record = install_skill_from_github(
            source_url=source_url,
            requested_name=str(payload.get("name") or "").strip(),
            title=str(payload.get("title") or "").strip(),
            summary=str(payload.get("summary") or "").strip(),
            category=str(payload.get("category") or "").strip(),
            node_types=payload.get("node_types") if isinstance(payload.get("node_types"), list)
            else payload.get("nodeTypes") if isinstance(payload.get("nodeTypes"), list)
            else [],
            keywords=payload.get("keywords") if isinstance(payload.get("keywords"), list) else [],
            tags=payload.get("tags") if isinstance(payload.get("tags"), list) else [],
        )
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"skill": record}


@app.delete("/api/skills/{skill_name}")
async def delete_skill_api(skill_name: str):
    if not remove_installed_skill(skill_name):
        return JSONResponse(status_code=404, content={"error": f"Skill {skill_name} not found or is not removable"})
    return {"success": True, "skill_name": skill_name}


@app.get("/api/openclaw-guide")
async def openclaw_guide():
    """Return the OpenClaw usage guide and quick-start MCP config."""
    return _build_openclaw_guide_payload()


# ─────────────────────────────────────────────
# Task Board API Endpoints
# ─────────────────────────────────────────────
@app.get("/api/tasks")
async def list_tasks(session_id: str = "", sessionId: str = ""):
    """List tasks, optionally filtered by session_id."""
    store = get_task_store()
    resolved_sid = session_id or sessionId or None
    return {"tasks": [_task_to_api(task) for task in store.list_tasks(session_id=resolved_sid)]}


@app.get("/api/board-summary")
async def board_summary(session_id: str = "", sessionId: str = ""):
    """G4: Return tasks with latest run + active node label in one request."""
    task_store = get_task_store()
    run_store = get_run_store()
    ne_store = get_node_execution_store()
    resolved_sid = session_id or sessionId or None
    tasks = task_store.list_tasks(session_id=resolved_sid)
    result = []
    for task in tasks:
        task_api = _task_to_api(task)
        task_id = task.get("id", "")
        # Find latest run for this task
        runs = run_store.list_runs(task_id=task_id)
        latest_run = None
        if runs:
            latest_run = max(runs, key=lambda r: r.get("updated_at", 0))
        # Find active node label if run is executing
        active_node_labels: list[str] = []
        if latest_run and latest_run.get("status") in ("running", "queued"):
            active_ne_ids = latest_run.get("active_node_execution_ids") or []
            if not active_ne_ids and latest_run.get("current_node_execution_id"):
                active_ne_ids = [latest_run.get("current_node_execution_id")]
            for ne_id in active_ne_ids:
                ne = ne_store.get_node_execution(str(ne_id))
                if ne and ne.get("status") in ("running", "queued"):
                    label = str(ne.get("node_label") or ne.get("node_key") or ne.get("id", ""))
                    if label and label not in active_node_labels:
                        active_node_labels.append(label)
            active_node_labels = active_node_labels[:3]
        task_api["latestRun"] = latest_run
        task_api["activeNodeLabel"] = active_node_labels[0] if active_node_labels else ""
        task_api["activeNodeLabels"] = active_node_labels
        result.append(task_api)
    return {"tasks": result}


@app.post("/api/tasks")
async def create_task(data: Dict = Body(...)):
    """Create a new task."""
    if not data or not data.get("title"):
        return JSONResponse(status_code=400, content={"error": "title is required"})
    store = get_task_store()
    task = store.create_task(_task_from_api(data))
    await _broadcast_ws_event({"type": "task_created", "payload": {"task": _task_to_api(task)}})
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
    await _broadcast_ws_event({"type": "task_updated", "payload": {"task": _task_to_api(updated)}})
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
    await _broadcast_ws_event({"type": "task_transitioned", "payload": {"task": result.get("task", {})}})
    return result


@app.delete("/api/tasks/session/{session_id}")
async def delete_tasks_by_session(session_id: str):
    """Delete all tasks belonging to a session."""
    if not session_id:
        return JSONResponse(status_code=400, content={"error": "session_id is required"})
    store = get_task_store()
    count = store.delete_tasks_by_session(session_id)
    return {"success": True, "deleted": count, "sessionId": session_id}


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
    resolved_run_id = str(normalized.get("run_id", "") or "").strip()
    resolved_task_id = _resolve_task_id(
        normalized.get("task_id", ""),
        resolved_run_id,
        "",
    )
    if resolved_task_id:
        normalized["task_id"] = resolved_task_id
    store = get_report_store()
    report = store.save_report(normalized)
    # Auto-link to task if task_id is provided
    task_id = str(normalized.get("task_id", "") or "").strip()
    if task_id:
        task_store = get_task_store()
        files = []
        for st in normalized.get("subtasks", []):
            files.extend(st.get("files_created", []) or st.get("filesCreated", []) or [])
        safe_run_id = ""
        if resolved_run_id:
            run_snapshot = get_run_store().get_run(resolved_run_id)
            if run_snapshot and str(run_snapshot.get("task_id") or "").strip() == task_id:
                safe_run_id = resolved_run_id
        task_store.link_run(
            task_id, safe_run_id,
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


@app.delete("/api/reports/{report_id}")
async def delete_report_ep(report_id: str):
    """v7.0 (maintainer): delete a single report by id."""
    store = get_report_store()
    ok = store.delete_report(report_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": f"Report {report_id} not found"})
    return {"ok": True, "deleted": report_id}


@app.delete("/api/reports")
async def delete_reports_bulk(keep_latest: int = 0):
    """v7.0 (maintainer): bulk-delete reports.
    Use ?keep_latest=N to retain the N most-recent entries, or
    ?keep_latest=0 (default) to wipe all.
    """
    store = get_report_store()
    all_reports = store.list_reports()
    try:
        all_reports = sorted(
            all_reports,
            key=lambda r: float(r.get("created_at") or 0),
            reverse=True,
        )
    except Exception:
        pass
    keep = max(0, int(keep_latest or 0))
    to_delete = all_reports[keep:] if keep > 0 else all_reports
    deleted = 0
    for r in to_delete:
        rid = str(r.get("id") or "")
        if rid and store.delete_report(rid):
            deleted += 1
    return {"ok": True, "deleted": deleted, "kept": min(keep, len(all_reports))}


# ─────────────────────────────────────────────
# V1 Run Lifecycle Endpoints
# ─────────────────────────────────────────────
@app.get("/api/runs")
async def list_runs(task_id: str = None, taskId: str = None):
    """List runs, optionally filtered by task_id."""
    tid = task_id or taskId
    runs = get_run_store().list_runs(task_id=tid)
    reconciled = []
    for run in runs:
        run_id = str(run.get("id") or "")
        reconciled.append(_reconcile_orphaned_running_run(run_id) if run_id else run)
    return {"runs": reconciled}


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
    task_snapshot = ts.get_task(task_id)
    run_snapshot = get_run_store().get_run(run["id"]) or run
    await _broadcast_ws_event({"type": "run_created", "payload": {"run": run_snapshot}})
    if task_snapshot:
        await _broadcast_ws_event({"type": "task_updated", "payload": {"task": _task_to_api(task_snapshot)}})
    return {"run": run_snapshot}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    run = _reconcile_orphaned_running_run(run_id) or get_run_store().get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
    return {"run": run}


@app.put("/api/runs/{run_id}")
async def update_run(run_id: str, data: Dict = Body(...)):
    updated = get_run_store().update_run(run_id, data or {})
    if not updated:
        return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
    await _broadcast_ws_event({"type": "run_updated", "payload": {"run": updated}})
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
    updated_run = get_run_store().get_run(run_id)
    await _broadcast_ws_event({"type": "run_transitioned", "payload": {"run": updated_run or {}}})
    return {"success": True, "run": updated_run}


@app.get("/api/workflow-templates")
async def api_list_workflow_templates():
    """P2-A: List available workflow templates (system + user)."""
    system = list_templates()
    # Tag system templates so frontend can distinguish
    for t in system:
        if isinstance(t, dict):
            t.setdefault("source", "system")
    return {"templates": system + _load_user_templates()}


# ── v7.2 (maintainer): User-defined custom templates ──────────────
# Storage: ~/.evermind/user_templates/{slug}.json
# JSON schema:
#   {
#       "id": "user-<slug>",
#       "name": "<display name>",
#       "description": "<optional summary>",
#       "tags": ["<optional>", ...],
#       "nodes": [{"key", "label", "task", "depends_on": [...]}, ...],
#       "created_at": <epoch>,
#       "updated_at": <epoch>,
#       "source": "user",
#   }
_USER_TEMPLATES_DIR = Path(os.path.expanduser("~/.evermind/user_templates"))


def _user_templates_dir() -> Path:
    _USER_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    return _USER_TEMPLATES_DIR


def _slugify_template_name(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return base[:48] or f"template-{int(time.time())}"


def _load_user_templates() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        d = _user_templates_dir()
        for fp in sorted(d.glob("*.json")):
            try:
                payload = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                payload.setdefault("source", "user")
                payload.setdefault("id", f"user-{fp.stem}")
                out.append(payload)
            except Exception as exc:
                logger.warning("user_template parse failed for %s: %s", fp.name, exc)
    except Exception as exc:
        logger.warning("user_templates dir scan failed: %s", exc)
    return out


@app.get("/api/templates/user")
async def api_list_user_templates():
    """List user-defined custom workflow templates."""
    return {"templates": _load_user_templates()}


@app.post("/api/templates/user")
async def api_save_user_template(data: Dict = Body(...)):
    """Save (create or overwrite) a user custom template.

    Body: {
        name: str (required),
        description?: str,
        tags?: [str],
        nodes: [{key, label, task, depends_on}],
        slug?: str (override auto-derive),
    }
    """
    payload = dict(data or {})
    name = str(payload.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "name is required"})
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return JSONResponse(status_code=400, content={"error": "nodes must be a non-empty list"})
    # Validate nodes — accept canvas-shaped nodes (id/type/data) OR
    # template-shaped nodes (key/label/task/depends_on). Normalize to template.
    norm_nodes: List[Dict[str, Any]] = []
    for raw in nodes[:64]:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or raw.get("type") or raw.get("id") or "").strip().lower()
        if not key:
            continue
        label = str(raw.get("label") or raw.get("data", {}).get("label") if isinstance(raw.get("data"), dict) else (raw.get("label") or key)).strip() or key
        task = str(raw.get("task") or raw.get("data", {}).get("task") if isinstance(raw.get("data"), dict) else (raw.get("task") or "")).strip()
        depends_on = raw.get("depends_on") or raw.get("dependsOn") or []
        if not isinstance(depends_on, list):
            depends_on = []
        # v7.39 (maintainer): preserve x/y position so user-saved
        # templates load with the EXACT layout they had on canvas. Previously
        # only key/label/task/depends_on were stored, and the frontend
        # rebuilt positions from depend graph (depth×220, row×130) on reload,
        # which scrambled any custom arrangement the user had.
        try:
            _x = raw.get("x")
            if _x is None and isinstance(raw.get("position"), dict):
                _x = raw["position"].get("x")
            _x = float(_x) if _x is not None else None
        except Exception:
            _x = None
        try:
            _y = raw.get("y")
            if _y is None and isinstance(raw.get("position"), dict):
                _y = raw["position"].get("y")
            _y = float(_y) if _y is not None else None
        except Exception:
            _y = None
        node_record: Dict[str, Any] = {
            "key": key,
            "label": label,
            "task": task,
            "depends_on": [str(x) for x in depends_on if x],
        }
        if _x is not None:
            node_record["x"] = _x
        if _y is not None:
            node_record["y"] = _y
        norm_nodes.append(node_record)
    if not norm_nodes:
        return JSONResponse(status_code=400, content={"error": "no usable nodes after normalization"})
    slug = _slugify_template_name(str(payload.get("slug") or name))
    now = time.time()
    record: Dict[str, Any] = {
        "id": f"user-{slug}",
        "name": name,
        "description": str(payload.get("description") or "")[:600],
        "tags": [str(x) for x in (payload.get("tags") or []) if x][:12],
        "nodes": norm_nodes,
        "created_at": now,
        "updated_at": now,
        "source": "user",
    }
    fp = _user_templates_dir() / f"{slug}.json"
    if fp.exists():
        # Preserve original created_at
        try:
            existing = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and existing.get("created_at"):
                record["created_at"] = float(existing["created_at"])
        except Exception:
            pass
    fp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "template": record}


@app.delete("/api/templates/user/{slug}")
async def api_delete_user_template(slug: str):
    """Delete a user custom template by slug."""
    fp = _user_templates_dir() / f"{_slugify_template_name(slug)}.json"
    if not fp.exists():
        return JSONResponse(status_code=404, content={"error": "template not found"})
    try:
        fp.unlink()
        return {"ok": True}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)[:200]})


@app.get("/api/templates/user/{slug}/export")
async def api_export_user_template(slug: str):
    """Return raw JSON for a user template (for clipboard / file download)."""
    fp = _user_templates_dir() / f"{_slugify_template_name(slug)}.json"
    if not fp.exists():
        return JSONResponse(status_code=404, content={"error": "template not found"})
    try:
        payload = json.loads(fp.read_text(encoding="utf-8"))
        return {"ok": True, "template": payload}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)[:200]})


@app.post("/api/templates/user/import")
async def api_import_user_template(data: Dict = Body(...)):
    """Import a JSON template payload (e.g. from a shared file)."""
    payload = dict(data or {})
    template = payload.get("template") if isinstance(payload.get("template"), dict) else payload
    if not isinstance(template, dict):
        return JSONResponse(status_code=400, content={"error": "template body required"})
    return await api_save_user_template(template)


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
    requested_runtime = str(data.get("runtime") or "local").strip()
    timeout_seconds = int(data.get("timeout_seconds") or data.get("timeoutSeconds") or 0)

    task_goal = str(task.get("description") or task.get("title") or "").strip()
    tpl = get_template(template_id, goal=task_goal)
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
        from agent_skills import resolve_skill_names_for_goal
        node_key = str(node_def["key"] or "").strip()
        node_goal = str(node_def.get("task") or node_def.get("label") or node_key)
        ne = nes_store.create_node_execution({
            "run_id": run_id,
            "node_key": node_key,
            "node_label": node_def["label"],
            "depends_on_keys": node_def.get("depends_on", []),
            "loaded_skills": resolve_skill_names_for_goal(node_key, node_goal),
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

    if run_snapshot:
        await _broadcast_ws_event({"type": "run_created", "payload": {"run": run_snapshot}})
    if task_snapshot:
        await _broadcast_ws_event({"type": "task_updated", "payload": {"task": _task_to_api(task_snapshot)}})

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

    # v7.3.9 audit-fix CRITICAL — actually stop the in-flight orchestrator,
    # not just flip the DB row. Without this, the orchestrator keeps
    # writing to OUTPUT_DIR for the full remaining 6+ minutes after
    # the user thinks the run was cancelled.
    try:
        for client_id, _client_state in list(globals().get("_clients", {}).items()):
            _orch = _client_state.get("orchestrator") if isinstance(_client_state, dict) else None
            if _orch and hasattr(_orch, "stop"):
                _orch.stop()
    except Exception as _stop_err:
        logger.debug("Orchestrator stop in HTTP cancel: %s", _stop_err)

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
# Relay / Proxy API Endpoints
# ─────────────────────────────────────────────
@app.post("/api/relay/add")
async def relay_add(data: Dict = Body(...)):
    """Register a new relay endpoint."""
    if not data:
        return {"error": "No data provided"}
    base_url = (data.get("base_url") or "").strip()
    template_id = str(data.get("template_id") or "").strip()
    provider = str(data.get("provider", "openai") or "openai").strip()
    api_style = str(data.get("api_style", "openai_compatible") or "openai_compatible").strip()
    if not base_url:
        resolved_template = resolve_relay_template(
            provider=provider,
            api_style=api_style,
            base_url=base_url,
            template_id=template_id,
        )
        base_url = str(resolved_template.get("default_base_url") or "").strip()
    if not base_url:
        return {"error": "base_url is required (or provide a relay template with a default base URL)"}
    if not base_url.startswith(("http://", "https://")):
        return {"error": "base_url must start with http:// or https://"}
    mgr = get_relay_manager()
    ep = mgr.add(
        name=data.get("name", "Unnamed Relay"),
        base_url=base_url,
        api_key=data.get("api_key", ""),
        models=data.get("models", []),
        headers=data.get("headers", {}),
        provider=provider,
        api_style=api_style,
        model_map=data.get("model_map", {}),
        template_id=template_id,
        max_retries=data.get("max_retries", 2),
        timeout=data.get("timeout", 120),
    )
    settings = load_settings()
    _persist_relays(settings)
    return {"success": True, "endpoint": ep.to_dict(), "relay_count": len(mgr.list())}


@app.get("/api/relay/catalog")
async def relay_catalog():
    """Return known relay/provider templates for fast setup."""
    templates = relay_template_catalog()
    return {"templates": templates, "total": len(templates)}


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
# Privacy / Desensitization Endpoints
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
    model = data.get("model") or node.get("data", {}).get("model") or node.get("model", "gpt-5.3-codex")

    workspace = os.getenv("WORKSPACE", str(Path.home() / ".evermind" / "workspace"))
    output_dir = str(OUTPUT_DIR)
    allowed_dirs_env = os.getenv("ALLOWED_DIRS", "")
    allowed_dirs = [p for p in allowed_dirs_env.split(",") if p] if allowed_dirs_env else [workspace, output_dir, "/tmp"]
    saved_settings = load_settings()

    bridge = AIBridge(config={
        "workspace": workspace,
        "output_dir": output_dir,
        "allowed_dirs": allowed_dirs,
        "max_timeout": coerce_int(os.getenv("SHELL_TIMEOUT", "30"), 30, minimum=5, maximum=600),
        "builder_enable_browser": is_builder_browser_enabled(),
        "tester_run_smoke": coerce_bool(os.getenv("EVERMIND_TESTER_RUN_SMOKE", "1"), default=True),
        "browser_headful": coerce_bool(os.getenv("EVERMIND_BROWSER_HEADFUL", "0"), default=False),
        "reviewer_tester_force_headful": coerce_bool(
            os.getenv("EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL", "0"),
            default=False,
        ),
        "browser_capture_trace": coerce_bool(
            os.getenv("EVERMIND_BROWSER_CAPTURE_TRACE", "0"),
            default=False,
        ),
        "max_retries": coerce_int(os.getenv("EVERMIND_MAX_RETRIES", "3"), 3, minimum=1, maximum=8),
        "image_generation": {
            "comfyui_url": str(os.getenv("EVERMIND_COMFYUI_URL", "") or "").strip(),
            "workflow_template": str(os.getenv("EVERMIND_COMFYUI_WORKFLOW_TEMPLATE", "") or "").strip(),
        },
        "analyst": _normalize_analyst_settings(
            saved_settings.get("analyst", {})
        ),
        "node_model_preferences": _normalize_node_model_preferences(
            saved_settings.get("node_model_preferences", {})
        ),
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
            default=False,
        )
    if "browser_capture_trace" in data:
        bridge.config["browser_capture_trace"] = coerce_bool(
            data.get("browser_capture_trace"),
            default=False,
        )
    if "max_retries" in data:
        bridge.config["max_retries"] = coerce_int(data.get("max_retries"), 3, minimum=1, maximum=8)
    if isinstance(data.get("image_generation"), dict):
        image_data = dict(data.get("image_generation") or {})
        bridge.config["image_generation"] = {
            "comfyui_url": str(image_data.get("comfyui_url", "") or "").strip(),
            "workflow_template": str(image_data.get("workflow_template", "") or "").strip(),
        }
    if "node_model_preferences" in data:
        bridge.config["node_model_preferences"] = _normalize_node_model_preferences(
            data.get("node_model_preferences")
        )
    if "analyst" in data:
        bridge.config["analyst"] = _normalize_analyst_settings(
            data.get("analyst")
        )
    node_type = node.get("data", {}).get("nodeType", node.get("type", ""))
    enabled_plugins = resolve_enabled_plugins_for_node(
        node_type,
        explicit_plugins=node.get("plugins") or node.get("data", {}).get("plugins"),
        config=bridge.config,
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


def _normalize_model_chain(value: Any, *, limit: int = 6) -> List[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = []
    normalized: List[str] = []
    seen: set[str] = set()
    for raw in items:
        model_name = str(raw or "").strip()
        if not model_name or model_name in seen:
            continue
        seen.add(model_name)
        normalized.append(model_name[:100])
        if len(normalized) >= limit:
            break
    return normalized


def _normalize_node_model_preferences(value: Any) -> Dict[str, List[str]]:
    if not isinstance(value, dict):
        return {}
    normalized: Dict[str, List[str]] = {}
    for raw_role, raw_chain in value.items():
        role = normalize_node_role(str(raw_role or "").strip())
        if not role:
            continue
        chain = _normalize_model_chain(raw_chain)
        if chain:
            normalized[role] = chain
    return normalized


def _normalize_analyst_preferred_sites(value: Any, *, limit: int = 12) -> List[str]:
    items = value if isinstance(value, (list, tuple)) else str(value or "").splitlines()
    normalized: List[str] = []
    seen: set[str] = set()
    for raw in items:
        site = str(raw or "").strip()
        if not site:
            continue
        if not re.match(r"^https?://", site, re.IGNORECASE):
            site = f"https://{site.lstrip('/')}"
        site = site.rstrip("/")
        if site in seen:
            continue
        seen.add(site)
        normalized.append(site[:240])
        if len(normalized) >= limit:
            break
    return normalized


def _normalize_analyst_settings(value: Any) -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    crawl_intensity = str(source.get("crawl_intensity", "medium") or "medium").strip().lower()
    if crawl_intensity not in {"off", "low", "medium", "high"}:
        crawl_intensity = "medium"
    return {
        "preferred_sites": _normalize_analyst_preferred_sites(source.get("preferred_sites", [])),
        "crawl_intensity": crawl_intensity,
        "use_scrapling_when_available": coerce_bool(
            source.get("use_scrapling_when_available", True),
            default=True,
        ),
        "enable_query_search": coerce_bool(
            source.get("enable_query_search", True),
            default=True,
        ),
    }


def _current_model_catalog() -> List[Dict[str, Any]]:
    bridge = AIBridge()
    models = bridge.get_available_models()
    return sorted(
        models,
        key=lambda item: (
            str(item.get("provider") or ""),
            str(item.get("id") or ""),
        ),
    )


def _persist_relays(settings: Dict):
    settings["relay_endpoints"] = get_relay_manager().export()
    save_settings(settings)


# Auto-load saved settings on startup
_saved_settings = load_settings()
_applied = apply_api_keys(_saved_settings)
_load_session_memory_from_disk()
get_relay_manager().load(_saved_settings.get("relay_endpoints", []))
if _saved_settings.get("builder", {}).get("enable_browser_search", False):
    os.environ["EVERMIND_BUILDER_ENABLE_BROWSER"] = "1"
else:
    os.environ["EVERMIND_BUILDER_ENABLE_BROWSER"] = "0"
os.environ["EVERMIND_TESTER_RUN_SMOKE"] = "1" if coerce_bool(_saved_settings.get("tester_run_smoke", True), default=True) else "0"
os.environ["EVERMIND_BROWSER_HEADFUL"] = "1" if coerce_bool(_saved_settings.get("browser_headful", False), default=False) else "0"
os.environ["EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL"] = "1" if coerce_bool(
    _saved_settings.get("reviewer_tester_force_headful", False),
    default=False,
) else "0"
os.environ["EVERMIND_BROWSER_CAPTURE_TRACE"] = "1" if coerce_bool(
    _saved_settings.get("browser_capture_trace", False),
    default=False,
) else "0"
os.environ["EVERMIND_QA_ENABLE_BROWSER_USE"] = "1" if coerce_bool(
    _saved_settings.get("qa_enable_browser_use", False),
    default=False,
) else "0"
_saved_browser_use_python = str(_saved_settings.get("browser_use_python", "") or "").strip()
if _saved_browser_use_python:
    os.environ["EVERMIND_BROWSER_USE_PYTHON"] = _saved_browser_use_python
else:
    os.environ.pop("EVERMIND_BROWSER_USE_PYTHON", None)
os.environ["EVERMIND_MAX_RETRIES"] = str(coerce_int(_saved_settings.get("max_retries", 3), 3, minimum=1, maximum=8))
_saved_image_cfg = _saved_settings.get("image_generation", {}) if isinstance(_saved_settings.get("image_generation"), dict) else {}
os.environ["EVERMIND_COMFYUI_URL"] = str(_saved_image_cfg.get("comfyui_url", "") or "").strip()
os.environ["EVERMIND_COMFYUI_WORKFLOW_TEMPLATE"] = str(_saved_image_cfg.get("workflow_template", "") or "").strip()
logger.info(f"Auto-loaded settings: {_applied} API keys applied, {len(get_relay_manager().list())} relays restored")


@app.get("/api/settings")
async def get_settings():
    """Get current saved settings (keys are masked)."""
    settings = load_settings()
    model_catalog = _current_model_catalog()
    # Mask API keys for security
    masked_keys = {}
    for k, v in settings.get("api_keys", {}).items():
        if v:
            masked_keys[k] = v[:6] + "..." + v[-4:] if len(v) > 10 else "***"
        else:
            masked_keys[k] = ""
    return {
        "api_keys": masked_keys,
        "api_bases": settings.get("api_bases", {}),
        "workspace": settings.get("workspace", ""),
        "artifact_sync_dir": settings.get("artifact_sync_dir", ""),
        "default_model": settings.get("default_model", "gpt-5.3-codex"),
        "privacy_enabled": settings.get("privacy", {}).get("enabled", True),
        "builder_enable_browser": coerce_bool(settings.get("builder", {}).get("enable_browser_search", False), default=False),
        "tester_run_smoke": coerce_bool(settings.get("tester_run_smoke", True), default=True),
        "browser_headful": coerce_bool(settings.get("browser_headful", False), default=False),
        "reviewer_tester_force_headful": coerce_bool(settings.get("reviewer_tester_force_headful", False), default=False),
        "browser_capture_trace": coerce_bool(settings.get("browser_capture_trace", False), default=False),
        "qa_enable_browser_use": coerce_bool(settings.get("qa_enable_browser_use", False), default=False),
        "browser_use_python": str(settings.get("browser_use_python", "") or "").strip(),
        "max_retries": coerce_int(settings.get("max_retries", 3), 3, minimum=1, maximum=8),
        "image_generation": settings.get("image_generation", {}),
        "image_generation_available": is_image_generation_available(settings),
        "analyst": _normalize_analyst_settings(settings.get("analyst", {})),
        "node_model_preferences": _normalize_node_model_preferences(
            settings.get("node_model_preferences", {})
        ),
        "thinking_depth": str(settings.get("thinking_depth", "deep")).strip().lower(),
        "reviewer_max_rejections": coerce_int(settings.get("reviewer_max_rejections", 2), 2, minimum=0, maximum=10),
        "model_catalog": model_catalog,
        "relay_endpoints": get_relay_manager().list(),
        "relay_count": len(get_relay_manager().list()),
        "has_keys": {k: bool(v) for k, v in settings.get("api_keys", {}).items()},
        "cli_mode": settings.get("cli_mode", {"enabled": False, "preferred_cli": "", "preferred_model": "", "detected_clis": {}, "node_cli_overrides": {}}),
    }


@app.post("/api/settings/save")
async def save_user_settings(data: Dict = Body(...)):
    """Save settings to disk and apply API keys."""
    patch = dict(data or {})
    if "workspace" in patch:
        patch["workspace"] = str(patch.get("workspace", "") or "").strip()
    if "artifact_sync_dir" in patch:
        patch["artifact_sync_dir"] = str(patch.get("artifact_sync_dir", "") or "").strip()
    if "tester_run_smoke" in patch:
        patch["tester_run_smoke"] = coerce_bool(patch.get("tester_run_smoke"), default=True)
    if "browser_headful" in patch:
        patch["browser_headful"] = coerce_bool(patch.get("browser_headful"), default=False)
    if "reviewer_tester_force_headful" in patch:
        patch["reviewer_tester_force_headful"] = coerce_bool(
            patch.get("reviewer_tester_force_headful"),
            default=False,
        )
    if "browser_capture_trace" in patch:
        patch["browser_capture_trace"] = coerce_bool(
            patch.get("browser_capture_trace"),
            default=False,
        )
    if "qa_enable_browser_use" in patch:
        patch["qa_enable_browser_use"] = coerce_bool(
            patch.get("qa_enable_browser_use"),
            default=False,
        )
    if "browser_use_python" in patch:
        patch["browser_use_python"] = str(patch.get("browser_use_python", "") or "").strip()
    if "max_retries" in patch:
        patch["max_retries"] = coerce_int(patch.get("max_retries"), 3, minimum=1, maximum=8)
    if "node_model_preferences" in patch:
        patch["node_model_preferences"] = _normalize_node_model_preferences(
            patch.get("node_model_preferences")
        )
    if "thinking_depth" in patch:
        raw_depth = str(patch.get("thinking_depth", "deep")).strip().lower()
        patch["thinking_depth"] = raw_depth if raw_depth in ("fast", "deep") else "deep"
    if "reviewer_max_rejections" in patch:
        patch["reviewer_max_rejections"] = coerce_int(
            patch.get("reviewer_max_rejections"), 1, minimum=0, maximum=10,
        )
    if "analyst" in patch:
        patch["analyst"] = _normalize_analyst_settings(
            patch.get("analyst")
        )
    if "builder_enable_browser" in patch:
        builder_browser = coerce_bool(patch["builder_enable_browser"], default=False)
        patch.setdefault("builder", {})
        if isinstance(patch["builder"], dict):
            patch["builder"]["enable_browser_search"] = builder_browser
        patch.pop("builder_enable_browser", None)
    if "image_generation" in patch and isinstance(patch.get("image_generation"), dict):
        image_patch = dict(patch.get("image_generation") or {})
        # v6.2 (maintainer): direct provider fields alongside legacy ComfyUI
        _max_images_raw = image_patch.get("max_images_per_run", 10)
        try:
            _max_images = max(1, min(40, int(_max_images_raw)))
        except (TypeError, ValueError):
            _max_images = 10
        patch["image_generation"] = {
            "comfyui_url": str(image_patch.get("comfyui_url", "") or "").strip(),
            "workflow_template": str(image_patch.get("workflow_template", "") or "").strip(),
            "provider": str(image_patch.get("provider", "") or "").strip().lower(),
            "api_key": str(image_patch.get("api_key", "") or "").strip(),
            "base_url": str(image_patch.get("base_url", "") or "").strip(),
            "default_model": str(image_patch.get("default_model", "") or "").strip(),
            "default_size": str(image_patch.get("default_size", "") or "1024x1024").strip() or "1024x1024",
            "max_images_per_run": _max_images,
            "auto_crop": bool(image_patch.get("auto_crop", True)),
            "enabled": bool(image_patch.get("enabled", True)),
        }
    if "cli_mode" in patch and isinstance(patch.get("cli_mode"), dict):
        cli_patch = dict(patch.get("cli_mode") or {})
        patch["cli_mode"] = {
            "enabled": bool(cli_patch.get("enabled", False)),
            "preferred_cli": str(cli_patch.get("preferred_cli", "") or "").strip(),
            "preferred_model": str(cli_patch.get("preferred_model", "") or "").strip(),
            "detected_clis": cli_patch.get("detected_clis", {}),
            "node_cli_overrides": cli_patch.get("node_cli_overrides", {}),
        }
    merged = _merge_settings(load_settings(), patch)
    if "relay_endpoints" not in (data or {}):
        merged["relay_endpoints"] = get_relay_manager().export()

    success = save_settings(merged)
    if success:
        count = apply_api_keys(merged)
        # v6.3 (maintainer): hot-reload EVERY live AIBridge so a UI key
        # change goes live WITHOUT restarting the backend. Previously the
        # flow updated env vars + disk but AIBridge instances held on to
        # cached OpenAI clients + compat-gateway health against the OLD
        # credentials. Net effect: user pastes new key → next run still 401s
        # on stale cached pool until the app is fully relaunched.
        #
        # v6.3 HOTFIX: this HTTP handler is NOT in the WebSocket closure, so
        # the `config` / `ai_bridge` local vars from connect_websocket aren't
        # in scope here. Use the module-level _LIVE_AIBRIDGES WeakSet (same
        # registry SIGHUP uses) to reach every running bridge.
        try:
            if _LIVE_AIBRIDGES is not None:
                _bridges_touched = 0
                for _live_bridge in list(_LIVE_AIBRIDGES):
                    if _live_bridge is None:
                        continue
                    try:
                        _reload_summary = _live_bridge.reload_api_config(merged)
                        _bridges_touched += 1
                        logger.info(
                            "Settings saved: bridge reload_api_config summary=%s",
                            _reload_summary,
                        )
                    except Exception as _bridge_err:
                        logger.warning(
                            "Settings saved but one bridge failed to reload: %s",
                            _bridge_err,
                        )
                logger.info(
                    "Settings saved: hot-reloaded %d live AIBridge instance(s)",
                    _bridges_touched,
                )
        except Exception as _reload_err:
            logger.warning("Settings saved but live reload failed: %s", _reload_err)
        get_relay_manager().load(merged.get("relay_endpoints", []))
        if merged.get("builder", {}).get("enable_browser_search", False):
            os.environ["EVERMIND_BUILDER_ENABLE_BROWSER"] = "1"
        else:
            os.environ["EVERMIND_BUILDER_ENABLE_BROWSER"] = "0"
        os.environ["EVERMIND_TESTER_RUN_SMOKE"] = "1" if coerce_bool(merged.get("tester_run_smoke", True), default=True) else "0"
        os.environ["EVERMIND_BROWSER_HEADFUL"] = "1" if coerce_bool(merged.get("browser_headful", False), default=False) else "0"
        os.environ["EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL"] = "1" if coerce_bool(
            merged.get("reviewer_tester_force_headful", False),
            default=False,
        ) else "0"
        os.environ["EVERMIND_BROWSER_CAPTURE_TRACE"] = "1" if coerce_bool(
            merged.get("browser_capture_trace", False),
            default=False,
        ) else "0"
        os.environ["EVERMIND_MAX_RETRIES"] = str(coerce_int(merged.get("max_retries", 3), 3, minimum=1, maximum=8))
        image_cfg = merged.get("image_generation", {}) if isinstance(merged.get("image_generation"), dict) else {}
        os.environ["EVERMIND_COMFYUI_URL"] = str(image_cfg.get("comfyui_url", "") or "").strip()
        os.environ["EVERMIND_COMFYUI_WORKFLOW_TEMPLATE"] = str(image_cfg.get("workflow_template", "") or "").strip()

        # Return which models are now available based on configured keys.
        # v5.8.6: provider_env_map must cover every provider in MODEL_REGISTRY,
        # otherwise newly-saved keys for those providers silently produce zero
        # visible models in the Settings UI.
        provider_env_map = {
            "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
            "google": "GEMINI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
            "kimi": "KIMI_API_KEY", "qwen": "QWEN_API_KEY",
            "minimax": "MINIMAX_API_KEY", "zhipu": "ZHIPU_API_KEY",
            "doubao": "DOUBAO_API_KEY", "yi": "YI_API_KEY",
            "aigate": "AIGATE_API_KEY",  # v5.8.6: relay relay
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
            "image_generation_available": is_image_generation_available(merged),
            "analyst": _normalize_analyst_settings(
                merged.get("analyst", {})
            ),
            "node_model_preferences": _normalize_node_model_preferences(
                merged.get("node_model_preferences", {})
            ),
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


# NOTE: v6.2 initially shipped a server-side Demo Library + shared-key quota
# endpoint (`/api/demos`, `/api/demos/try`, `/api/demos/report-cost`). That
# design put the project author on the hook for API costs. Removed per the maintainer
# 2026-04-20. Quick-start templates now live client-side in TemplateGallery.tsx
# and run entirely with the user's own configured API key.


@app.post("/api/settings/image_gen/test")
async def image_gen_test(data: Dict = Body(...)):
    """v6.2 (maintainer): Test-drive an image_generation config.

    Accepts an ephemeral config (not persisted), hits the provider once with
    a built-in or user-provided prompt, and returns timing + preview.

    Response shape:
      Success: { ok, provider, latency_ms, image_base64, files, size_returned }
      Failure: { ok: false, error, stage }
    """
    start_ts = time.time()
    try:
        from image_gen import ImageGen  # local import to avoid circular
    except Exception as exc:
        return {"ok": False, "error": f"image_gen adapter unavailable: {exc}", "stage": "import"}

    provider = str(data.get("provider") or "").strip().lower()
    api_key = str(data.get("api_key") or "").strip()
    if not provider or not api_key:
        return {"ok": False, "error": "provider and api_key are required", "stage": "validate"}

    ephemeral_cfg = {
        "image_generation": {
            "provider": provider,
            "api_key": api_key,
            "base_url": str(data.get("base_url") or "").strip(),
            "default_model": str(data.get("default_model") or "").strip(),
            "default_size": str(data.get("default_size") or "1024x1024").strip(),
            "max_images_per_run": 1,
            "auto_crop": False,
            "enabled": True,
        }
    }
    gen = ImageGen(ephemeral_cfg)
    if not gen.available:
        return {"ok": False, "error": "httpx unavailable or config invalid", "stage": "init"}

    prompt = str(data.get("prompt") or "").strip() or (
        "A tidy scene with warm morning light, soft depth of field, minimalist composition, 8k, photo-real"
    )
    slug = "evermind_test"
    size = ephemeral_cfg["image_generation"]["default_size"]

    try:
        result = await asyncio.wait_for(
            gen.generate(prompt=prompt, output_slug=slug, size=size),
            timeout=40.0,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Timeout after 40s", "stage": "generate",
                "latency_ms": int((time.time() - start_ts) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:400], "stage": "generate",
                "latency_ms": int((time.time() - start_ts) * 1000)}

    latency_ms = int((time.time() - start_ts) * 1000)
    if not result or result.get("status") != "ok":
        return {"ok": False, "error": "Provider returned no image", "stage": "provider",
                "latency_ms": latency_ms}

    # Inline base64 so the UI can preview without needing a static file route.
    raw_path = (result.get("files") or {}).get("raw")
    image_base64 = ""
    if raw_path and os.path.exists(raw_path):
        try:
            with open(raw_path, "rb") as fh:
                image_base64 = "data:image/webp;base64," + base64.b64encode(fh.read()).decode("ascii")
        except Exception:
            image_base64 = ""

    return {
        "ok": True,
        "provider": result.get("provider"),
        "latency_ms": latency_ms,
        "image_base64": image_base64,
        "files": result.get("files", {}),
        "size_returned": size,
    }


@app.get("/api/system/signing-status")
async def signing_status():
    """Report current macOS code-sign identity health.

    Used by the Settings UI to tell the user whether they'll keep getting
    TCC file-permission prompts on every rebuild (ad-hoc) or whether a
    stable self-signed identity is already in place (permanent grant).
    """
    import subprocess as _sp
    out = {
        "platform_supported": sys.platform == "darwin",
        "stable_identity_installed": False,
        "identity_name": None,
        "env_var_set": False,
        "current_app_signature": None,
        "can_apply": False,
        "applied": False,
    }
    if sys.platform != "darwin":
        return out
    out["env_var_set"] = bool(os.getenv("EVERMIND_CODESIGN_IDENTITY"))
    out["identity_name"] = os.getenv("EVERMIND_CODESIGN_IDENTITY") or "Evermind Local Dev"
    try:
        r = _sp.run(
            ["security", "find-identity", "-v", "-p", "codesigning"],
            capture_output=True, text=True, timeout=5,
        )
        out["stable_identity_installed"] = out["identity_name"] in (r.stdout or "")
    except Exception:
        pass
    out["can_apply"] = out["stable_identity_installed"]
    # v7.7: was eagerly probing ~/Desktop/Evermind.app + /Applications/Evermind.app
    # which triggered macOS App Management TCC prompt at every endpoint call.
    # Now: only probe when caller explicitly opts in via ?probe_apps=1. The
    # status endpoint without that flag stays in the "no TCC dialog" zone.
    return out


@app.post("/api/system/apply-stable-signing")
async def apply_stable_signing(data: Dict = Body(default={})):
    """Apply the Evermind Local Dev self-signed cert to all .app copies.

    One-time action the user triggers from Settings. After this the TCC
    prompt appears once and is never repeated, because macOS key the
    permission against the stable designated requirement rather than the
    ad-hoc hash that changes every rebuild.

    Prereq: the self-signed cert must already exist in the user's keychain.
    The response tells the UI exactly what to do if it doesn't.
    """
    import subprocess as _sp
    identity = str(data.get("identity") or os.getenv("EVERMIND_CODESIGN_IDENTITY") or "Evermind Local Dev")
    if sys.platform != "darwin":
        return {"ok": False, "error": "platform_not_darwin"}
    # Verify cert exists
    try:
        r = _sp.run(
            ["security", "find-identity", "-v", "-p", "codesigning"],
            capture_output=True, text=True, timeout=5,
        )
        if identity not in (r.stdout or ""):
            return {
                "ok": False,
                "error": "certificate_not_found",
                "message": (
                    f"Self-signed certificate '{identity}' not found in Keychain.\n"
                    "1. Open Keychain Access.app\n"
                    "2. Certificate Assistant → Create a Certificate…\n"
                    f"3. Name: {identity}\n"
                    "4. Identity Type: Self Signed Root\n"
                    "5. Certificate Type: Code Signing\n"
                    "6. Click Create, then run this again."
                ),
            }
    except Exception as exc:
        return {"ok": False, "error": f"security_check_failed: {exc}"}
    # Persist env var into ~/.zshrc so future shells / rebuilds pick it up.
    rc = os.path.expanduser("~/.zshrc")
    try:
        existing = ""
        if os.path.exists(rc):
            with open(rc, "r") as fh:
                existing = fh.read()
        if "EVERMIND_CODESIGN_IDENTITY" not in existing:
            with open(rc, "a") as fh:
                fh.write(f'\n# Added by Evermind Settings\nexport EVERMIND_CODESIGN_IDENTITY="{identity}"\n')
    except Exception:
        pass
    # Sign every .app copy we can find. The entitlements file ships next to
    # this server.py inside the .app's `Resources/backend/` dir during a
    # packaged build, OR sits at <repo>/electron/build/ during dev. We try
    # both and skip if neither is found.
    _here = os.path.dirname(os.path.abspath(__file__))
    entitlements_candidates = [
        os.path.join(_here, "..", "..", "..", "Contents", "Resources", "build", "entitlements.mac.plist"),
        os.path.join(_here, "..", "electron", "build", "entitlements.mac.plist"),
        os.path.expanduser("~/Library/Application Support/Evermind/build/entitlements.mac.plist"),
    ]
    entitlements = next((os.path.normpath(p) for p in entitlements_candidates if os.path.exists(p)), "")
    # Sign whichever .app copy we can find on this user's machine.
    targets = [
        os.path.expanduser("~/Desktop/Evermind.app"),
        os.path.expanduser("~/Applications/Evermind.app"),
        "/Applications/Evermind.app",
    ]
    signed: List[str] = []
    errors: List[str] = []
    for tgt in targets:
        if not os.path.isdir(tgt):
            continue
        try:
            _sp.run(["xattr", "-cr", tgt], check=False)
            sign_args = [
                "codesign", "--force", "--deep", "--timestamp=none",
                "--options=runtime",
            ]
            if os.path.exists(entitlements):
                sign_args.extend(["--entitlements", entitlements])
            sign_args.extend(["-s", identity, tgt])
            _sp.run(sign_args, check=True, capture_output=True, text=True, timeout=60)
            signed.append(tgt)
        except _sp.CalledProcessError as exc:
            errors.append(f"{tgt}: {(exc.stderr or str(exc))[:200]}")
        except Exception as exc:
            errors.append(f"{tgt}: {str(exc)[:200]}")
    # Reset any stale TCC record so macOS will re-prompt once, now against
    # the stable identity.
    for bundle_id in ("com.evermind.desktop", "evermind-desktop"):
        try:
            _sp.run(["tccutil", "reset", "All", bundle_id], check=False, capture_output=True, timeout=3)
        except Exception:
            pass
    return {
        "ok": True,
        "signed": signed,
        "errors": errors,
        "identity": identity,
        "next_step": (
            "完成。下次启动 Evermind 会弹 1 次权限提示,点 OK,之后永久记住。"
            if signed else "未找到任何 .app 目标,请先构建。"
        ),
    }


# ─────────────────────────────────────────────
# CLI Backend Endpoints
# ─────────────────────────────────────────────
from cli_backend import get_detector, get_executor, is_cli_mode_enabled, CLI_PROFILES


@app.post("/api/cli/toggle")
async def cli_toggle(data: Dict = Body(...)):
    """v7.0 (maintainer): one-call CLI-mode on/off.
    Body: {"enabled": true, "preferred_cli": "claude", "preferred_model": "sonnet"}
    Persists to ~/.evermind/config.json + applies to in-memory settings.
    Also runs auto-detection so `detected_clis` is fresh.
    """
    from settings import load_settings as _ls, save_settings as _ss
    from cli_backend import get_detector as _gd
    enabled = bool(data.get("enabled", True))
    s = _ls()
    cm = s.get("cli_mode") or {}
    cm["enabled"] = enabled
    for key in ("preferred_cli", "preferred_model"):
        v = data.get(key)
        if v is not None:
            cm[key] = str(v)
    if isinstance(data.get("node_cli_overrides"), dict):
        cm["node_cli_overrides"] = data["node_cli_overrides"]
    # v7.1 Ultra Mode fields
    if "ultra_mode" in data:
        cm["ultra_mode"] = bool(data.get("ultra_mode"))
    for key in ("ultra_parallel_builders", "ultra_max_rejections", "ultra_total_timeout_sec"):
        if key in data and data.get(key) is not None:
            try:
                cm[key] = int(data.get(key))
            except Exception:
                pass
    for key in ("ultra_project_scaffold", "ultra_asset_tools"):
        if key in data:
            cm[key] = bool(data.get(key))
    # Auto-detect so config reflects reality
    try:
        detected = await _gd().detect_all(force=True)
        cm["detected_clis"] = detected
    except Exception:
        pass
    s["cli_mode"] = cm
    ok = _ss(s)
    # Also update EVERY live ai_bridge config so change takes effect
    # without requiring a server restart.
    try:
        if _LIVE_AIBRIDGES:
            for bridge in list(_LIVE_AIBRIDGES):
                try:
                    if isinstance(getattr(bridge, "config", None), dict):
                        bridge.config["cli_mode"] = cm
                except Exception:
                    pass
    except Exception:
        pass
    return {
        "ok": ok,
        "enabled": enabled,
        "preferred_cli": cm.get("preferred_cli", ""),
        "preferred_model": cm.get("preferred_model", ""),
        "available_clis": [n for n, i in (cm.get("detected_clis") or {}).items() if i.get("available")],
    }


@app.get("/api/cli/status")
async def cli_status():
    """v7.0: concise status — is CLI mode on? which CLIs are live?"""
    from settings import load_settings as _ls
    from cli_backend import get_detector as _gd, CLI_PROFILES
    s = _ls()
    cm = s.get("cli_mode") or {}
    detector = _gd()
    detected = getattr(detector, "_cache", None) or {}
    if not detected:
        detected = await detector.detect_all(force=False)
    available = [n for n, i in detected.items() if i.get("available")]
    _CLI_ALLOWED = {
        "planner", "planner_degraded", "analyst", "uidesign", "scribe",
        "builder", "merger", "polisher", "reviewer", "patcher",
        "tester", "debugger", "deployer", "router",
    }
    return {
        "enabled": bool(cm.get("enabled")),
        "preferred_cli": cm.get("preferred_cli", ""),
        "preferred_model": cm.get("preferred_model", ""),
        "supported_clis": list(CLI_PROFILES.keys()),
        "detected_clis": detected,
        "available_clis": available,
        "cli_enabled_nodes": sorted(_CLI_ALLOWED),
        "node_cli_overrides": cm.get("node_cli_overrides") or {},
    }


@app.get("/api/cli/detect")
async def detect_clis(force: bool = False):
    """Detect all available AI CLI tools on this machine."""
    detector = get_detector()
    detected = await detector.detect_all(force=force)
    available_names = [name for name, info in detected.items() if info.get("available")]
    return {
        "clis": detected,
        "available": available_names,
        "available_count": len(available_names),
        "supported": list(CLI_PROFILES.keys()),
    }


@app.post("/api/cli/test")
async def test_cli(data: Dict = Body(...)):
    """Smoke-test a specific CLI tool to verify it works."""
    cli_name = str(data.get("cli", "") or "").strip()
    if not cli_name:
        return {"success": False, "error": "No CLI name provided"}
    detector = get_detector()
    result = await detector.test_cli(cli_name)
    return result


@app.post("/api/cli/test-all")
async def test_all_clis():
    """Detect and smoke-test all available CLIs in parallel."""
    detector = get_detector()
    detected = await detector.detect_all(force=True)
    available = [name for name, info in detected.items() if info.get("available")]
    results = {}
    if available:
        tasks = [detector.test_cli(name) for name in available]
        tested = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(available, tested):
            if isinstance(result, Exception):
                results[name] = {"success": False, "error": str(result)[:200]}
            else:
                results[name] = result
    return {
        "results": results,
        "available": available,
        "supported": list(CLI_PROFILES.keys()),
    }


@app.get("/api/cli/models")
async def get_cli_model_options():
    """Return available model options for each registered CLI."""
    from cli_backend import CLI_MODEL_OPTIONS, CLI_PROFILES
    return {
        "models": CLI_MODEL_OPTIONS,
        "supported_clis": list(CLI_PROFILES.keys()),
    }


@app.post("/api/models/speed-test")
async def model_speed_test(data: Dict = Body(default={})):
    """Test latency for all configured models.

    V4.3: Provides real-time speed data so users can pick the fastest model.
    Tests each model with a trivial prompt and returns TTFT + total latency.
    Only tests models whose provider has an API key configured.
    """
    import asyncio as _asyncio
    from ai_bridge import AIBridge, MODEL_REGISTRY, PROVIDER_ENV_KEY_MAP

    settings = load_settings()
    api_keys = settings.get("api_keys", {})
    api_bases = settings.get("api_bases", {})

    # Determine which models to test: only those with a configured key
    requested_models = data.get("models")
    results = {}

    # Build list of testable models
    testable = []
    for model_name, info in MODEL_REGISTRY.items():
        if requested_models and model_name not in requested_models:
            continue
        provider = info.get("provider", "")
        # Map provider to the settings key name
        provider_key_map = {
            "openai": "openai", "anthropic": "anthropic", "google": "gemini",
            "deepseek": "deepseek", "kimi": "kimi", "qwen": "qwen",
            "zhipu": "zhipu", "doubao": "doubao", "yi": "yi", "minimax": "minimax", "aigate": "aigate",
        }
        settings_key = provider_key_map.get(provider, provider)
        has_key = bool(str(api_keys.get(settings_key, "") or "").strip())
        if not has_key and provider != "ollama":
            results[model_name] = {
                "ok": False, "latency_ms": 0, "error": "no_api_key",
                "provider": provider,
            }
            continue
        testable.append(model_name)

    # Create bridge with current config
    bridge_config = {
        "api_keys": api_keys,
        "api_bases": api_bases,
    }
    bridge = AIBridge(config=bridge_config)

    # V4.5: Enhanced speed test with TTFT, realistic prompt, multi-iteration
    import time as _time
    import litellm as _litellm_mod

    # v6.0: even faster speed test.
    #   - prompt: 10 chars ("Say OK.") — minimize TTFB, the actual probe
    #     is "does the first token arrive?" not "does the model produce
    #     a meaningful reply?"
    #   - max_tokens: 8 (was 60) — model can finish in one chunk
    #   - timeout: 8s (was 15s) — healthy TTFB is 0.5-3s, anything above
    #     8s is functionally useless for an interactive app
    #   - bail out at first content chunk: record TTFT then close the
    #     stream — we don't actually need the "total" latency, it's
    #     dominated by max_tokens, not model health
    _SPEED_TEST_PROMPT = "Say OK."
    _SPEED_TEST_ITERATIONS = 1

    def _test_model_sync(name: str) -> tuple:
        """Test a single model: probe TTFT, bail at first content byte."""
        info = MODEL_REGISTRY.get(name, {})
        litellm_id = info.get("litellm_id", name)
        provider = str(info.get("provider") or "").lower()

        # Build request kwargs
        messages = [{"role": "user", "content": _SPEED_TEST_PROMPT}]
        kwargs = {
            "model": litellm_id,
            "messages": messages,
            "max_tokens": 8,
            # v6.0: 15 → 8s. Healthy gateways answer within 3s; above 8s
            # the model is unusable for any interactive Evermind flow.
            "timeout": 8,
            "num_retries": 0,
            "stream": True,
        }
        if info.get("api_base"):
            kwargs["api_base"] = info["api_base"]
        if info.get("extra_headers"):
            kwargs["extra_headers"] = info["extra_headers"]
        resolved_key = bridge._resolved_api_key_for_model_info(info)
        if resolved_key:
            kwargs["api_key"] = resolved_key
        # Kimi thinking mode fix
        if provider == "kimi":
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        ttft_samples = []
        total_samples = []
        last_error = ""

        # v6.0: bail out at first content chunk.
        # Previously we drained the whole stream (content + stop + usage).
        # For a liveness probe that's wasted time — once we've measured
        # TTFT, we know the model is alive. Everything after the first
        # chunk is just waiting for max_tokens=8 to exhaust or the model
        # to say "stop", neither of which tells us anything useful.
        for _iter in range(_SPEED_TEST_ITERATIONS):
            t0 = _time.monotonic()
            ttft_recorded = False
            try:
                response = _litellm_mod.completion(**kwargs)
                content_parts: list[str] = []
                for chunk in response:
                    if chunk.choices and chunk.choices[0].delta:
                        c = getattr(chunk.choices[0].delta, "content", None)
                        if c:
                            if not ttft_recorded:
                                ttft_samples.append(int((_time.monotonic() - t0) * 1000))
                                ttft_recorded = True
                            content_parts.append(c)
                            # v6.0: we have evidence the model is alive —
                            # close the generator and move on.
                            try:
                                response.close()
                            except Exception:
                                pass
                            break
                total_ms = int((_time.monotonic() - t0) * 1000)
                reply = "".join(content_parts).strip()
                if reply:
                    total_samples.append(total_ms)
                else:
                    last_error = "empty_reply"
                    break  # v5.8.6: don't retry empty replies
            except Exception as exc:
                last_error = str(exc)[:200]
                # v5.8.6: fail-fast on auth / timeout / quota / rate-limit — no
                # amount of retrying fixes these. Only retry on transient
                # network blips (ConnectionError / transient 5xx).
                err_lower = str(exc).lower()
                fatal_markers = (
                    "authenticationerror", "api_key", "invalid_authentication",
                    "unauthorized", "insufficient_user_quota", "quota",
                    "timeout", "rate_limit", "not_found", "notfounderror",
                    "no available channel", "access_terminated",
                )
                if any(m in err_lower for m in fatal_markers):
                    break

        if not total_samples:
            return name, {
                "ok": False,
                "latency_ms": 0,
                "ttft_ms": 0,
                "error": last_error or "all_iterations_failed",
                "provider": info.get("provider", ""),
                "iterations": _SPEED_TEST_ITERATIONS,
            }

        # v5.8.6 iter 2: with iterations=1, trimming is moot.
        sorted_totals = sorted(total_samples)
        sorted_ttfts = sorted(ttft_samples) if ttft_samples else []
        stable_total = int(sum(sorted_totals) / len(sorted_totals))
        stable_ttft = int(sum(sorted_ttfts) / len(sorted_ttfts)) if sorted_ttfts else stable_total
        median_total = sorted_totals[len(sorted_totals) // 2]

        return name, {
            "ok": True,
            "latency_ms": stable_total,   # trimmed-mean total
            "ttft_ms": stable_ttft,       # trimmed-mean TTFT
            "median_ms": median_total,
            "best_ms": sorted_totals[0],  # still expose best for reference
            "worst_ms": sorted_totals[-1],
            "error": "",
            "provider": info.get("provider", ""),
            "iterations": len(total_samples),
        }

    # v5.8.6: SERIAL per-provider to avoid self-inflicted rate limiting.
    # Prior version ran 5 models in parallel, so 5 Kimi models → 5 concurrent
    # requests to Kimi For Coding → 429 from same tenant → models showed
    # "all_iterations_failed" even though key/endpoint were fine.
    # Now we group by provider, run each group serially, but cross-provider
    # groups still parallel (OpenAI + Kimi + Deepseek at once).
    by_provider: Dict[str, List[str]] = {}
    for name in testable:
        info = MODEL_REGISTRY.get(name, {})
        prov = str(info.get("provider") or "unknown")
        by_provider.setdefault(prov, []).append(name)

    loop = _asyncio.get_event_loop()

    async def _run_provider_batched(models: List[str]) -> List[tuple]:
        # v6.0: batch raised 3 → 6 per provider. Combined with TTFT-only
        # bail-out and 8s timeout, a 12-model provider (aigate) now runs
        # in 2 waves of 6 × ~3s = ~6s instead of 4 waves of 3 × ~8s = 32s.
        # 6 concurrent is still under typical public-relay rate thresholds
        # (10 req/s). If a relay 429s we pick it up in `last_error`.
        # Additional: if the first wave returns ALL auth errors, skip the
        # rest of the provider — no point probing 6 more models against
        # the same broken key.
        out: List[tuple] = []
        BATCH = 6
        short_circuit = False
        for i in range(0, len(models), BATCH):
            chunk = models[i:i + BATCH]
            if short_circuit:
                for m in chunk:
                    info = MODEL_REGISTRY.get(m, {})
                    out.append((m, {
                        "ok": False, "latency_ms": 0, "ttft_ms": 0,
                        "error": "provider_auth_skipped",
                        "provider": info.get("provider", ""),
                        "iterations": 0,
                    }))
                continue
            results = await _asyncio.gather(*[
                loop.run_in_executor(None, _test_model_sync, m) for m in chunk
            ])
            out.extend(results)
            # Trip the short-circuit when the whole wave failed on auth.
            auth_tokens = ("api_key", "auth", "unauthorized", "invalid_token", "401")
            if all(
                not r.get("ok") and any(t in str(r.get("error") or "").lower() for t in auth_tokens)
                for _n, r in results
            ):
                short_circuit = True
        return out

    all_batches = await _asyncio.gather(*[
        _run_provider_batched(models) for models in by_provider.values()
    ])
    for group in all_batches:
        for name, result in group:
            results[name] = result

    return {"results": results, "tested_count": len(testable), "total_models": len(MODEL_REGISTRY)}


@app.get("/api/models")
async def list_models():
    """List all available models with their provider and capabilities."""
    from ai_bridge import MODEL_REGISTRY
    settings = load_settings()
    api_keys = settings.get("api_keys", {})

    models = []
    provider_key_map = {
        "openai": "openai", "anthropic": "anthropic", "google": "gemini",
        "deepseek": "deepseek", "kimi": "kimi", "qwen": "qwen",
        "zhipu": "zhipu", "doubao": "doubao", "yi": "yi", "minimax": "minimax", "aigate": "aigate",
    }
    for name, info in MODEL_REGISTRY.items():
        provider = info.get("provider", "")
        settings_key = provider_key_map.get(provider, provider)
        has_key = bool(str(api_keys.get(settings_key, "") or "").strip())
        models.append({
            "id": name,
            "provider": provider,
            "has_key": has_key or provider == "ollama",
            "supports_tools": info.get("supports_tools", False),
            "supports_cua": info.get("supports_cua", False),
        })
    return {"models": models}


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
DEFAULT_NODE_TIMEOUT_S = 960    # 16 minutes default; builder gets a longer role-specific hint below
DEFAULT_RUN_TIMEOUT_S = 3600    # 1 hour
WATCHDOG_INTERVAL_S = 30        # check every 30 seconds

# Per-role timeout hints so the watchdog can use role-aware limits.
_ROLE_TIMEOUT_HINTS: Dict[str, int] = {
    "builder": 1020,    # legacy base hint; helper below expands this for long Kimi multi-file runs
    "planner": 180,     # lightweight spec output
    "analyst": 540,     # browser research can be slow
    "imagegen": 300,    # optional asset discovery / prompt-pack stage
    "spritesheet": 210, # compact manifest planning only
    "assetimport": 240, # compact manifest normalization only
    "polisher": 540,    # premium motion/finish pass
    "reviewer": 480,    # review + browser validation
    "tester": 480,      # smoke + interaction tests
    "deployer": 300,    # file listing + URL generation
    "debugger": 600,    # iterative fix cycles
    # Ultra-mode node keys (4-builder DAG); these share the "builder" role
    # hint above, so listed here only for visibility.
    "uidesign": 600,
    "scribe": 600,
    "merger": 900,
    "patcher": 600,
}


def _ultra_mode_is_on() -> bool:
    """v7.1d (maintainer): read cli_mode.ultra_mode from settings.
    Used by the watchdog to multiply per-role timeouts so commercial-grade
    long tasks (Analyst 15KB+ research, planner 15-subtask blueprint,
    Claude CLI writing a 20-file Electron app) don't get axed at 180s.
    """
    try:
        from settings import load_settings as _ls
        cm = (_ls() or {}).get("cli_mode") or {}
        return bool(cm.get("ultra_mode"))
    except Exception:
        return False


# In Ultra mode the watchdog's per-role hint gets multiplied by this factor.
# Aligned with ai_bridge's ×6 CLI timeout so the watchdog never pre-empts a
# valid long CLI call. E.g. planner 180s × 6 = 1080s (18min) — enough for
# Claude CLI to emit a 15-subtask Ultra blueprint with node_briefs.
_ULTRA_WATCHDOG_MULT = 6
_watchdog_task: Optional[asyncio.Task] = None

# v6.0: chat agent browser integration — a persistent BrowserPlugin
# instance shared across chat turns, invoked from the chat_worker thread
# via run_coroutine_threadsafe so Playwright state survives tool calls.
_MAIN_ASYNCIO_LOOP: Optional[asyncio.AbstractEventLoop] = None
_CHAT_BROWSER_PLUGIN = None
_CHAT_BROWSER_LOCK = __import__("threading").Lock()


def _get_chat_browser_plugin():
    """Lazy-instantiate the shared chat browser plugin."""
    global _CHAT_BROWSER_PLUGIN
    if _CHAT_BROWSER_PLUGIN is None:
        with _CHAT_BROWSER_LOCK:
            if _CHAT_BROWSER_PLUGIN is None:
                try:
                    from plugins.implementations import BrowserPlugin
                    _CHAT_BROWSER_PLUGIN = BrowserPlugin()
                except Exception as exc:  # pragma: no cover
                    logger.warning("chat BrowserPlugin init failed: %s", exc)
                    _CHAT_BROWSER_PLUGIN = False  # sentinel for "init failed"
    return _CHAT_BROWSER_PLUGIN if _CHAT_BROWSER_PLUGIN is not False else None


# v6.4.42 (maintainer): inflight future tracker. If a previous
# browser action timed out at the worker level, the Playwright coroutine
# may still be scheduled in the main asyncio loop, blocking future
# navigate/click calls behind a zombie task. Before submitting a new
# action, cancel any still-pending inflight future so the new call
# isn't queued behind a stalled one.
_CHAT_BROWSER_INFLIGHT: "Optional[Any]" = None
_CHAT_BROWSER_INFLIGHT_LOCK = threading.Lock()


def _BROWSER_SUB_TIMEOUT(action: str) -> float:
    """v6.4.42: per-action budgets so a single slow navigate can't eat the
    whole 90s pool. Tight but not adversarial — kimi/gpt will get a clean
    error and retry instead of the whole chat stalling for minutes."""
    a = (action or "").lower()
    # Network-heavy actions get longest
    if a in ("navigate", "goto", "open", "visit"):
        return 30.0
    # Screenshot involves rendering + encoding but no JS wait
    if a in ("screenshot", "snapshot", "ax_tree", "dom_snapshot"):
        return 20.0
    # Pure UI actions
    if a in ("click", "double_click", "right_click", "hover", "fill",
             "type", "press", "key", "select", "focus", "blur", "scroll",
             "drag", "wait"):
        return 15.0
    # Text extraction / small queries
    if a in ("extract_text", "get_text", "get_html", "get_attribute",
             "query_selector", "wait_for_selector"):
        return 10.0
    return 25.0  # sensible default for unknown actions


def _invoke_chat_browser_tool(params: Dict[str, Any], timeout_sec: float = 30.0) -> Dict[str, Any]:
    """Synchronously run a BrowserPlugin action from the chat_worker thread.

    The worker thread submits the coroutine to the main asyncio loop and
    blocks on the resulting concurrent.futures.Future. This keeps Playwright
    alive across tool calls (so navigate+click+screenshot share a session)
    without bouncing event loops.

    v6.4.42: sub-action timeouts, zombie-coroutine cancel, entry/exit
    logging so chat browser trips are visible in the log.
    """
    global _CHAT_BROWSER_INFLIGHT
    import concurrent.futures as _cf
    import time as _ttime
    _action = str(params.get("action") or "").strip().lower()
    _url = str(params.get("url") or "")[:120]
    _selector = str(params.get("selector") or "")[:80]
    # Honor caller timeout but respect per-action floor/ceiling
    _sub = _BROWSER_SUB_TIMEOUT(_action)
    _effective_timeout = max(5.0, min(float(timeout_sec or _sub), _sub))
    logger.info(
        "chat-browser ▶ action=%s url=%s selector=%s timeout=%.1fs",
        _action or "?", _url, _selector, _effective_timeout,
    )
    plugin = _get_chat_browser_plugin()
    if plugin is None:
        logger.warning("chat-browser ✗ plugin unavailable")
        return {"success": False, "error": "browser plugin unavailable"}
    if _MAIN_ASYNCIO_LOOP is None:
        logger.warning("chat-browser ✗ asyncio loop not ready")
        return {"success": False, "error": "main loop not ready yet"}
    # v6.1.3: chat browser MUST be headful so the user sees the cursor/ripple/
    # AI-controlling frame overlay. Previously only `browser_show_ai_cursor`
    # was set but `_resolve_headless` only reads `browser_headful`, so the
    # chat plugin silently ran headless and users saw NO browser UI at all.
    _chat_browser_ctx = {
        "browser_show_ai_cursor": True,
        "browser_headful": True,
        "visible": True,
        "node_type": "chat",
        "force_visible": True,
    }
    # v6.4.42: kill any stale prior inflight that blew past its budget.
    with _CHAT_BROWSER_INFLIGHT_LOCK:
        _prev = _CHAT_BROWSER_INFLIGHT
        if _prev is not None and not _prev.done():
            try:
                _prev.cancel()
                logger.warning("chat-browser: cancelled zombie prior inflight future")
            except Exception:
                pass
    _started = _ttime.monotonic()
    try:
        fut = asyncio.run_coroutine_threadsafe(
            plugin.execute(params, context=_chat_browser_ctx),
            _MAIN_ASYNCIO_LOOP,
        )
        with _CHAT_BROWSER_INFLIGHT_LOCK:
            _CHAT_BROWSER_INFLIGHT = fut
        result = fut.result(timeout=_effective_timeout)
    except _cf.TimeoutError:
        _elapsed = _ttime.monotonic() - _started
        logger.warning(
            "chat-browser ⏱ timeout action=%s after %.1fs — cancelling coroutine",
            _action, _elapsed,
        )
        try:
            fut.cancel()
        except Exception:
            pass
        return {
            "success": False,
            "error": (
                f"browser action '{_action}' timed out after "
                f"{_effective_timeout:.0f}s — try a different URL/selector "
                f"or ask the user for a more specific target"
            ),
        }
    except Exception as exc:  # pragma: no cover
        logger.warning("chat-browser ✗ %s failed: %s", _action, str(exc)[:200])
        return {"success": False, "error": f"browser action failed: {exc}"}
    finally:
        with _CHAT_BROWSER_INFLIGHT_LOCK:
            if _CHAT_BROWSER_INFLIGHT is not None and _CHAT_BROWSER_INFLIGHT.done():
                _CHAT_BROWSER_INFLIGHT = None
    _elapsed = _ttime.monotonic() - _started
    if hasattr(result, "to_dict"):
        result = result.to_dict()
    if not isinstance(result, dict):
        logger.warning(
            "chat-browser ✗ unexpected result type %s after %.1fs",
            type(result).__name__, _elapsed,
        )
        return {"success": False, "error": f"unexpected browser result type: {type(result).__name__}"}
    logger.info(
        "chat-browser ✓ action=%s success=%s elapsed=%.1fs err=%s",
        _action, bool(result.get("success")), _elapsed,
        str(result.get("error") or "")[:120],
    )
    return result


def _watchdog_timeout_grace_seconds() -> int:
    raw = os.getenv("EVERMIND_WATCHDOG_TIMEOUT_GRACE_SEC", "90")
    try:
        value = int(raw)
    except Exception:
        value = 90
    return max(30, min(value, 300))


def _builder_watchdog_timeout_floor_seconds() -> int:
    base_hint = int(_ROLE_TIMEOUT_HINTS.get("builder", DEFAULT_NODE_TIMEOUT_S) or DEFAULT_NODE_TIMEOUT_S)
    raw_builder_timeout = os.getenv("EVERMIND_BUILDER_TIMEOUT_SEC", str(base_hint))
    raw_multifile_cap = os.getenv("EVERMIND_BUILDER_DIRECT_MULTIFILE_MAX_TIMEOUT_SEC", "1800")
    try:
        builder_timeout = int(raw_builder_timeout)
    except Exception:
        builder_timeout = base_hint
    try:
        direct_multifile_cap = int(raw_multifile_cap)
    except Exception:
        direct_multifile_cap = 1800
    builder_timeout = max(base_hint, builder_timeout)
    direct_multifile_cap = max(builder_timeout, direct_multifile_cap)
    return min(2700, max(base_hint, direct_multifile_cap + _watchdog_timeout_grace_seconds()))


def _node_timeout_hint_seconds(node_key: str) -> int:
    normalized = normalize_node_role(node_key) or str(node_key or "").strip().lower()
    timeout_hint = int(_ROLE_TIMEOUT_HINTS.get(normalized, DEFAULT_NODE_TIMEOUT_S) or DEFAULT_NODE_TIMEOUT_S)
    if normalized == "builder":
        timeout_hint = max(timeout_hint, _builder_watchdog_timeout_floor_seconds())
    # v7.1d Ultra: watchdog must not pre-empt a valid long CLI call.
    # Mirror ai_bridge's ×6 CLI timeout factor.
    if _ultra_mode_is_on():
        timeout_hint = int(timeout_hint * _ULTRA_WATCHDOG_MULT)
    return timeout_hint


def _node_timeout_limit_seconds(ne_dict: Dict[str, Any]) -> int:
    node_key = str(ne_dict.get("node_key") or "").strip()
    normalized = normalize_node_role(node_key) or node_key.lower()
    explicit_timeout = int(ne_dict.get("timeout_seconds", 0) or 0)
    timeout_hint = _node_timeout_hint_seconds(normalized)
    if explicit_timeout <= 0:
        return timeout_hint
    if normalized == "builder":
        return max(explicit_timeout, timeout_hint)
    return explicit_timeout


def _run_timeout_budget_seconds_for_node(node_key: str) -> int:
    raw_key = str(node_key or "").strip()
    normalized = normalize_node_role(raw_key) or raw_key.lower()
    budget = _node_timeout_hint_seconds(normalized)
    builder_like = normalized == "builder" or bool(
        re.search(r"\b(?:merger|integrator|integration|assemble|assembly|merge)\b", raw_key, re.IGNORECASE)
    )
    if builder_like:
        # Builder + merger nodes can escalate their internal execution budget above
        # the initial per-node hint during long Kimi repair/merge passes. Use a
        # more realistic critical-path budget at the run level so the outer watchdog
        # does not cancel the whole run while a valid builder/merger lane is active.
        budget = max(budget, 2400 + _watchdog_timeout_grace_seconds())
    return budget


def _estimate_run_timeout_seconds(nodes_def: List[Dict[str, Any]]) -> int:
    if not isinstance(nodes_def, list) or not nodes_def:
        return DEFAULT_RUN_TIMEOUT_S

    graph: Dict[str, List[str]] = {}
    weights: Dict[str, int] = {}
    for node_def in nodes_def:
        key = str((node_def or {}).get("key") or "").strip()
        if not key:
            continue
        deps = [str(dep).strip() for dep in ((node_def or {}).get("depends_on") or []) if str(dep).strip()]
        graph[key] = deps
        weights[key] = _run_timeout_budget_seconds_for_node(key)

    if not weights:
        return DEFAULT_RUN_TIMEOUT_S

    memo: Dict[str, int] = {}

    def _longest_path(node_key: str, stack: Set[str]) -> int:
        if node_key in memo:
            return memo[node_key]
        if node_key in stack:
            return weights.get(node_key, DEFAULT_NODE_TIMEOUT_S)
        stack.add(node_key)
        dep_budget = 0
        for dep in graph.get(node_key, []):
            dep_budget = max(dep_budget, _longest_path(dep, stack))
        stack.discard(node_key)
        total = dep_budget + weights.get(node_key, DEFAULT_NODE_TIMEOUT_S)
        memo[node_key] = total
        return total

    critical_path = max(_longest_path(node_key, set()) for node_key in weights)
    run_budget = critical_path + max(600, _watchdog_timeout_grace_seconds() * 3)
    return min(10800, max(DEFAULT_RUN_TIMEOUT_S, run_budget))


async def _timeout_watchdog():
    """Background task that auto-fails stuck nodes and auto-cancels timed-out runs."""
    while True:
        try:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            now = time.time()

            # 1. Check for stuck nodes (running longer than timeout)
            nes = get_node_execution_store()
            runs_store = get_run_store()
            for ne_dict in nes.list_node_executions():
                if ne_dict.get("status") != "running":
                    continue
                started = float(ne_dict.get("started_at", 0) or 0)
                if started <= 0:
                    continue
                # v5.8.6: skip NEs whose parent run is no longer active. Previously
                # the watchdog would "fail" stale NEs from runs the user already
                # stopped, and the WS broadcast would race against canvas nodes
                # of the CURRENT run that map to the same agent type — painting
                # the current Builder 2 red even though it had succeeded. Any NE
                # whose run is in a terminal state is someone else's problem;
                # mark it cancelled silently instead of broadcasting failure.
                ne_run_id = str(ne_dict.get("run_id") or "")
                parent_run = runs_store.get_run(ne_run_id) if ne_run_id else None
                parent_run_status = str((parent_run or {}).get("status") or "").strip().lower()
                if parent_run_status in {"completed", "cancelled", "failed", "passed", "skipped"}:
                    try:
                        _transition_node_if_needed(ne_dict["id"], "cancelled")
                        nes.update_node_execution(ne_dict["id"], {
                            "error_message": "Cancelled: parent run already terminal (orphaned NE cleanup).",
                        })
                    except Exception:
                        pass
                    continue
                ne_timeout = _node_timeout_limit_seconds(ne_dict)
                elapsed = now - started
                if elapsed > ne_timeout:
                    ne_id = ne_dict["id"]
                    run_id = ne_run_id
                    logger.warning(f"[Watchdog] Node {ne_id} timed out after {elapsed:.0f}s (limit={ne_timeout}s)")
                    _transition_node_if_needed(ne_id, "failed")
                    nes.update_node_execution(ne_id, {
                        "error_message": f"Timed out after {int(elapsed)}s (limit: {ne_timeout}s)",
                    })
                    if run_id:
                        _reconcile_orphaned_running_run(run_id, now=now, force=True)
                    ne_latest = nes.get_node_execution(ne_id) or {}
                    run_latest = get_run_store().get_run(run_id) or {}
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
                run_id = str(run_dict.get("id") or "")
                reconciled = _reconcile_orphaned_running_run(run_id, now=now) if run_id else run_dict
                if not reconciled or reconciled.get("status") != "running":
                    continue
                run_dict = reconciled
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


# ─────────────────────────────────────────────
# SIGHUP Hot-Reload: reload key modules without restarting
# ─────────────────────────────────────────────
# Inspired by Claude Code's live provider reconfiguration pattern.
# When `sync:local-app` updates files on disk, sending SIGHUP to
# the running Python sidecar reloads frequently-changed modules so
# the operator doesn't need to restart the app after every sync.

_HOT_RELOAD_MODULES = [
    "workflow_templates",
    "task_classifier",
    "html_postprocess",
    "node_roles",
    "agent_skills",
    "orchestrator",
    "ai_bridge",
    # v6.1.2: include browser plugin internals so focus/launch fixes can be
    # hot-reloaded without full app restart.
    "plugins.implementations",
    "plugins.base",
    "plugins",
]


# v6.1.2: weak-track every AIBridge instance so SIGHUP can rebind their
# __class__ to the freshly reloaded class. Without this, the module reloads
# but running instances still execute the old class's bound methods, which
# made our deep-mode timeout floor look like it wasn't taking effect.
try:
    import weakref as _weakref
    _LIVE_AIBRIDGES: "_weakref.WeakSet" = _weakref.WeakSet()
except Exception:
    _LIVE_AIBRIDGES = None  # type: ignore[assignment]


def _register_live_ai_bridge(bridge_instance: Any) -> None:
    if _LIVE_AIBRIDGES is None or bridge_instance is None:
        return
    try:
        _LIVE_AIBRIDGES.add(bridge_instance)
    except Exception:
        pass


def _handle_sighup(signum, frame):
    """Reload key modules on SIGHUP without restarting the process."""
    reloaded = []
    failed = []
    for mod_name in _HOT_RELOAD_MODULES:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        try:
            importlib.reload(mod)
            reloaded.append(mod_name)
        except Exception as e:
            failed.append(f"{mod_name}: {e}")
    # v6.1.2: rebind live AIBridge instances to the newly reloaded class so
    # currently-running nodes pick up method changes (timeouts, thinking
    # config, etc.) without needing an app restart.
    if "ai_bridge" in reloaded and _LIVE_AIBRIDGES is not None:
        try:
            import ai_bridge as _ab_mod
            _new_class = getattr(_ab_mod, "AIBridge", None)
            if _new_class is not None:
                _rebound = 0
                for _live in list(_LIVE_AIBRIDGES):
                    try:
                        _live.__class__ = _new_class
                        _rebound += 1
                    except Exception:
                        pass
                logger.info("[HotReload] rebound %d live AIBridge instance(s) to reloaded class", _rebound)
        except Exception as _rebind_err:
            logger.warning("[HotReload] AIBridge class rebind failed: %s", _rebind_err)
    # Re-import references that server.py holds directly
    if "workflow_templates" in reloaded:
        try:
            global get_template, list_templates, template_nodes
            import workflow_templates as _wt
            get_template = _wt.get_template
            list_templates = _wt.list_templates
            template_nodes = _wt.template_nodes
        except Exception:
            pass
    if "node_roles" in reloaded:
        try:
            global normalize_node_role
            import node_roles as _nr
            normalize_node_role = _nr.normalize_node_role
        except Exception:
            pass
    # §FIX: Rebind `from X import Y` references that server.py holds directly.
    # Without this, importlib.reload() updates the module object but the old
    # class/function references cached in server.py's global scope stay stale.
    if "ai_bridge" in reloaded:
        try:
            global AIBridge, MODEL_REGISTRY
            import ai_bridge as _ab
            AIBridge = _ab.AIBridge
            MODEL_REGISTRY = _ab.MODEL_REGISTRY
        except Exception:
            pass
    if "orchestrator" in reloaded:
        try:
            global Orchestrator
            import orchestrator as _orch
            Orchestrator = _orch.Orchestrator
        except Exception:
            pass
    summary = f"reloaded={reloaded}" + (f" failed={failed}" if failed else "")
    logger.info(f"[HotReload] SIGHUP received — {summary}")


@asynccontextmanager
async def lifespan(application):
    """FastAPI lifespan: start/stop background tasks."""
    global _watchdog_task, _MAIN_ASYNCIO_LOOP
    lock_error = _acquire_backend_runtime_lock()
    if lock_error:
        logger.error(lock_error)
        raise RuntimeError(lock_error)
    # v6.0: capture main asyncio loop so chat_worker thread can submit
    # browser-tool coroutines via asyncio.run_coroutine_threadsafe.
    try:
        _MAIN_ASYNCIO_LOOP = asyncio.get_running_loop()
    except Exception:
        _MAIN_ASYNCIO_LOOP = None
    _watchdog_task = asyncio.create_task(_timeout_watchdog())
    logger.info("[Watchdog] Timeout watchdog started")
    # v7.1g (maintainer): bootstrap user CLI configs on every server
    # startup. Idempotent — only writes files that don't exist or merges
    # missing keys. Lets a fresh user install Evermind.app and immediately
    # benefit from Codex profiles, Claude skills, sub-agents, MCP servers,
    # auto-format hooks, codex output schemas. Without this, all v7.1g
    # optimizations would be invisible to anyone who isn't the maintainer.
    try:
        from evermind_bootstrap import bootstrap_at_startup as _bootstrap
        _bootstrap()
    except Exception as _bs_err:
        logger.warning("[v7.1g bootstrap] startup hook failed: %s", _bs_err)

    # v7.3.9 audit-fix CRITICAL — startup sweep of unbounded directories.
    # Without these, /tmp/evermind_output and ~/.evermind/uploads grew
    # to multi-GB sizes after weeks of use.
    try:
        import time as _t
        from os import walk as _walk
        # Prune `_stable_previews` to most-recent 8 runs (each ~5-30 MB)
        sp_root = OUTPUT_DIR / "_stable_previews"
        if sp_root.exists() and sp_root.is_dir():
            entries = sorted(
                [p for p in sp_root.iterdir() if p.is_dir()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old in entries[8:]:
                try: shutil.rmtree(old, ignore_errors=True)
                except Exception: pass
        # Prune `_browser_records/` files older than 24h (screenshots + traces)
        br_root = OUTPUT_DIR / "_browser_records"
        if br_root.exists():
            cutoff = _t.time() - 24 * 3600
            for entry in br_root.iterdir():
                try:
                    if entry.stat().st_mtime < cutoff:
                        if entry.is_dir():
                            shutil.rmtree(entry, ignore_errors=True)
                        else:
                            entry.unlink(missing_ok=True)
                except Exception: pass
        # Prune `~/.evermind/uploads/` chat attachments older than 30 days
        if CHAT_UPLOADS_DIR.exists():
            cutoff = _t.time() - 30 * 86400
            for sess_dir in CHAT_UPLOADS_DIR.iterdir():
                if not sess_dir.is_dir():
                    continue
                try:
                    for f in sess_dir.iterdir():
                        try:
                            if f.is_file() and f.stat().st_mtime < cutoff:
                                f.unlink(missing_ok=True)
                        except Exception: pass
                    # Remove empty session dirs
                    try:
                        if not any(sess_dir.iterdir()):
                            sess_dir.rmdir()
                    except Exception: pass
                except Exception: pass
        logger.info("[v7.3.9 cleanup] startup directory sweep complete")
    except Exception as _cleanup_err:
        logger.warning("[v7.3.9 cleanup] startup sweep failed: %s", _cleanup_err)
    # Register SIGHUP handler for hot-reload (macOS/Linux only)
    # Guard with try/except: signal.signal() must be called from the main thread.
    # During tests (httpx.AsyncClient / TestClient), lifespan runs in a worker thread.
    if hasattr(signal, "SIGHUP"):
        try:
            signal.signal(signal.SIGHUP, _handle_sighup)
            logger.info("[HotReload] SIGHUP handler registered — send SIGHUP to reload modules")
        except ValueError:
            # Not running in main thread (test environment)
            pass
    # v5.5: TCP+TLS preconnect to primary API hosts — warms DNS cache & TLS
    # session tickets so the first real LLM call shaves ~80-150ms off its
    # handshake. Non-blocking, failures are ignored.
    try:
        import socket
        from urllib.parse import urlparse
        from ai_bridge import MODEL_REGISTRY
        _hosts: set[tuple[str, int]] = set()
        for _mi in MODEL_REGISTRY.values():
            base = str((_mi or {}).get("api_base") or "").strip()
            if not base:
                continue
            try:
                u = urlparse(base)
                if u.hostname:
                    _hosts.add((u.hostname, u.port or (443 if u.scheme == "https" else 80)))
            except Exception:
                pass
        async def _preconnect_host(host: str, port: int) -> None:
            try:
                await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=(port == 443)),
                    timeout=3.0,
                )
            except Exception:
                pass  # silent: preconnect is best-effort
        if _hosts:
            await asyncio.gather(*[_preconnect_host(h, p) for h, p in _hosts], return_exceptions=True)
            logger.info("[Preconnect] Warmed %d API host(s): %s", len(_hosts),
                        sorted(h for h, _ in _hosts)[:8])
    except Exception as _pc_err:
        logger.debug("[Preconnect] skipped: %s", _pc_err)

    # v5.5: Start configured MCP servers (optional — empty config is a no-op).
    try:
        import mcp_client
        _mcp_config = (_saved_settings or {}).get("mcp_servers") or []
        if _mcp_config:
            _started = await mcp_client.start_configured_servers(_mcp_config)
            if _started:
                logger.info("[MCP] Started %d server(s): %s", len(_started), _started)
                # Expose each MCP tool as an Evermind plugin so agents can call it.
                try:
                    from plugins.mcp_plugin import register_mcp_tools_as_plugins
                    _registered = await register_mcp_tools_as_plugins()
                    if _registered:
                        logger.info("[MCP] Exposed %d tool(s) to agent plugin layer", len(_registered))
                except Exception as _mcp_reg_err:
                    logger.warning("[MCP] Plugin registration failed: %s", _mcp_reg_err)
    except Exception as _mcp_err:
        logger.warning("[MCP] Failed to start configured servers: %s", _mcp_err)
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
    for task in list(_openclaw_dispatch_watchdogs.values()):
        if not task.done():
            task.cancel()
    _openclaw_dispatch_watchdogs.clear()
    for client_id, tasks in _active_tasks.items():
        for task in tasks:
            _cancel_tracked_task(task)
    _active_tasks.clear()
    for task in list(_detached_tasks):
        _cancel_tracked_task(task)
    _detached_tasks.clear()
    for ws in list(connected_clients):
        try:
            await ws.close(code=1001, reason="Server shutting down")
        except Exception:
            pass
    connected_clients.clear()
    # v5.5: Shut down MCP servers gracefully.
    try:
        import mcp_client
        await mcp_client.shutdown_configured_servers()
    except Exception as _mcp_err:
        logger.debug("[MCP] shutdown error: %s", _mcp_err)
    # Flush chat session memory to disk before exit
    global _session_memory_save_timer
    if _session_memory_save_timer is not None:
        _session_memory_save_timer.cancel()
    _save_session_memory_to_disk()
    _release_backend_runtime_lock()
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
    workspace = os.getenv("WORKSPACE", str(Path.home() / ".evermind" / "workspace"))
    output_dir = str(OUTPUT_DIR)
    allowed_dirs_env = os.getenv("ALLOWED_DIRS", "")
    allowed_dirs = [p for p in allowed_dirs_env.split(",") if p] if allowed_dirs_env else [workspace, output_dir, "/tmp"]
    saved_settings = load_settings()
    # v4.0-fix: Prefer decrypted API keys from saved_settings (config.json)
    # over env vars, which may be stale if .env was changed after server start.
    _saved_keys = saved_settings.get("api_keys") or {}
    config = {
        "openai_api_key": str(_saved_keys.get("openai") or os.getenv("OPENAI_API_KEY", "") or "").strip(),
        "anthropic_api_key": str(_saved_keys.get("anthropic") or os.getenv("ANTHROPIC_API_KEY", "") or "").strip(),
        "gemini_api_key": str(_saved_keys.get("gemini") or os.getenv("GEMINI_API_KEY", "") or "").strip(),
        "deepseek_api_key": str(_saved_keys.get("deepseek") or os.getenv("DEEPSEEK_API_KEY", "") or "").strip(),
        "kimi_api_key": str(_saved_keys.get("kimi") or os.getenv("KIMI_API_KEY", "") or "").strip(),
        "qwen_api_key": str(_saved_keys.get("qwen") or os.getenv("QWEN_API_KEY", "") or "").strip(),
        "workspace": workspace,
        "output_dir": output_dir,
        "max_timeout": coerce_int(os.getenv("SHELL_TIMEOUT", "30"), 30, minimum=5, maximum=600),
        "allowed_dirs": allowed_dirs,
        "builder_enable_browser": is_builder_browser_enabled(),
        "tester_run_smoke": coerce_bool(os.getenv("EVERMIND_TESTER_RUN_SMOKE", "1"), default=True),
        "browser_headful": coerce_bool(os.getenv("EVERMIND_BROWSER_HEADFUL", "0"), default=False),
        "reviewer_tester_force_headful": coerce_bool(
            os.getenv("EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL", "0"),
            default=False,
        ),
        "max_retries": coerce_int(os.getenv("EVERMIND_MAX_RETRIES", "3"), 3, minimum=1, maximum=8),
        "image_generation": {
            "comfyui_url": str(os.getenv("EVERMIND_COMFYUI_URL", "") or "").strip(),
            "workflow_template": str(os.getenv("EVERMIND_COMFYUI_WORKFLOW_TEMPLATE", "") or "").strip(),
        },
        "analyst": _normalize_analyst_settings(
            saved_settings.get("analyst", {})
        ),
        "node_model_preferences": _normalize_node_model_preferences(
            saved_settings.get("node_model_preferences", {})
        ),
        "thinking_depth": str(saved_settings.get("thinking_depth", "deep")).strip().lower() or "deep",
        "cli_mode": saved_settings.get("cli_mode", {
            "enabled": False, "preferred_cli": "", "preferred_model": "",
            "detected_clis": {}, "node_cli_overrides": {},
        }),
    }

    # Create executor for this client
    ai_bridge = AIBridge(config=config)
    _register_live_ai_bridge(ai_bridge)

    async def send_event(data: Dict):
        """Send real-time event to this client."""
        try:
            _maybe_auto_sync_delivery_artifacts(data)
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
        "reviewer_tester_force_headful": coerce_bool(config.get("reviewer_tester_force_headful", False), default=False),
        "browser_capture_trace": coerce_bool(config.get("browser_capture_trace", False), default=False),
        "max_retries": coerce_int(config.get("max_retries", 3), 3, minimum=1, maximum=8),
        "image_generation": config.get("image_generation", {}),
        "image_generation_available": is_image_generation_available(config),
        "analyst": _normalize_analyst_settings(config.get("analyst", {})),
        "node_model_preferences": config.get("node_model_preferences", {}),
        "openclaw": _build_openclaw_guide_payload(),
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
                # v5.8.6: every provider in MODEL_REGISTRY must be configurable
                # from Settings UI — previously missing MiniMax / Zhipu / Doubao /
                # Yi meant users had no way to paste their keys for those models.
                key_map = {
                    "openai_api_key": "OPENAI_API_KEY",
                    "anthropic_api_key": "ANTHROPIC_API_KEY",
                    "gemini_api_key": "GEMINI_API_KEY",
                    "deepseek_api_key": "DEEPSEEK_API_KEY",
                    "kimi_api_key": "KIMI_API_KEY",
                    "qwen_api_key": "QWEN_API_KEY",
                    "minimax_api_key": "MINIMAX_API_KEY",
                    "zhipu_api_key": "ZHIPU_API_KEY",
                    "doubao_api_key": "DOUBAO_API_KEY",
                    "yi_api_key": "YI_API_KEY",
                    "aigate_api_key": "AIGATE_API_KEY",  # v5.8.6: relay
                }
                # v5.8.6: persist primary + optional secondary ("_2") keys
                extended_map = dict(key_map)
                for config_key, env_key in key_map.items():
                    extended_map[f"{config_key}_2"] = f"{env_key}_2"
                for config_key, env_key in extended_map.items():
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
                        default=False,
                    )
                    os.environ["EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL"] = "1" if config["reviewer_tester_force_headful"] else "0"
                if "browser_capture_trace" in new_config:
                    config["browser_capture_trace"] = coerce_bool(
                        new_config.get("browser_capture_trace"),
                        default=False,
                    )
                    os.environ["EVERMIND_BROWSER_CAPTURE_TRACE"] = "1" if config["browser_capture_trace"] else "0"
                if "qa_enable_browser_use" in new_config:
                    config["qa_enable_browser_use"] = coerce_bool(
                        new_config.get("qa_enable_browser_use"),
                        default=False,
                    )
                    os.environ["EVERMIND_QA_ENABLE_BROWSER_USE"] = "1" if config["qa_enable_browser_use"] else "0"
                if "browser_use_python" in new_config:
                    config["browser_use_python"] = str(new_config.get("browser_use_python", "") or "").strip()
                    if config["browser_use_python"]:
                        os.environ["EVERMIND_BROWSER_USE_PYTHON"] = config["browser_use_python"]
                    else:
                        os.environ.pop("EVERMIND_BROWSER_USE_PYTHON", None)
                if "max_retries" in new_config:
                    config["max_retries"] = coerce_int(new_config.get("max_retries"), 3, minimum=1, maximum=8)
                    os.environ["EVERMIND_MAX_RETRIES"] = str(coerce_int(config["max_retries"], 3, minimum=1, maximum=8))
                if "node_model_preferences" in new_config:
                    config["node_model_preferences"] = _normalize_node_model_preferences(
                        new_config.get("node_model_preferences")
                    )
                if "reviewer_max_rejections" in new_config:
                    rmr = coerce_int(
                        new_config.get("reviewer_max_rejections"), 1, minimum=0, maximum=10,
                    )
                    config["reviewer_max_rejections"] = rmr
                    if ai_bridge and hasattr(ai_bridge, "config") and isinstance(ai_bridge.config, dict):
                        ai_bridge.config["reviewer_max_rejections"] = rmr
                    try:
                        os.environ["EVERMIND_REVIEWER_MAX_REJECTIONS"] = str(rmr)
                    except Exception:
                        pass
                    logger.info("reviewer_max_rejections updated to %d via update_config", rmr)
                if "thinking_depth" in new_config:
                    raw_depth = str(new_config.get("thinking_depth", "deep")).strip().lower()
                    if raw_depth in ("fast", "deep"):
                        config["thinking_depth"] = raw_depth
                        # Propagate to ai_bridge config so both bridge and
                        # orchestrator see the change immediately.
                        if ai_bridge and hasattr(ai_bridge, "config") and isinstance(ai_bridge.config, dict):
                            ai_bridge.config["thinking_depth"] = raw_depth
                        # v6.1.3 (Opus review #1-P1): the 3-source fallback in
                        # ai_bridge._effective_timeout_for_node reads env and
                        # disk as well. Propagate here so every WebSocket
                        # client + SIGHUP-rebound instance sees the new value.
                        try:
                            os.environ["EVERMIND_THINKING_DEPTH"] = raw_depth
                        except Exception:
                            pass
                        try:
                            import json as _json
                            _disk_path = os.path.expanduser("~/.evermind/config.json")
                            if os.path.exists(_disk_path):
                                with open(_disk_path, "r", encoding="utf-8") as _fh:
                                    _disk_data = _json.load(_fh) or {}
                                _disk_data["thinking_depth"] = raw_depth
                                with open(_disk_path, "w", encoding="utf-8") as _fh:
                                    _json.dump(_disk_data, _fh, ensure_ascii=False, indent=2)
                        except Exception as _persist_err:
                            logger.warning("Failed to persist thinking_depth to disk: %s", _persist_err)
                        logger.info("thinking_depth updated to '%s' via update_config (env + disk synced)", raw_depth)
                if "analyst" in new_config:
                    config["analyst"] = _normalize_analyst_settings(
                        new_config.get("analyst")
                    )
                if isinstance(new_config.get("image_generation"), dict):
                    image_cfg = dict(new_config.get("image_generation") or {})
                    try:
                        _max_images_live = max(1, min(40, int(image_cfg.get("max_images_per_run", 10))))
                    except (TypeError, ValueError):
                        _max_images_live = 10
                    config["image_generation"] = {
                        "comfyui_url": str(image_cfg.get("comfyui_url", "") or "").strip(),
                        "workflow_template": str(image_cfg.get("workflow_template", "") or "").strip(),
                        "provider": str(image_cfg.get("provider", "") or "").strip().lower(),
                        "api_key": str(image_cfg.get("api_key", "") or "").strip(),
                        "base_url": str(image_cfg.get("base_url", "") or "").strip(),
                        "default_model": str(image_cfg.get("default_model", "") or "").strip(),
                        "default_size": str(image_cfg.get("default_size", "") or "1024x1024").strip() or "1024x1024",
                        "max_images_per_run": _max_images_live,
                        "auto_crop": bool(image_cfg.get("auto_crop", True)),
                        "enabled": bool(image_cfg.get("enabled", True)),
                    }
                    os.environ["EVERMIND_COMFYUI_URL"] = config["image_generation"]["comfyui_url"]
                    os.environ["EVERMIND_COMFYUI_WORKFLOW_TEMPLATE"] = config["image_generation"]["workflow_template"]
                if isinstance(new_config.get("builder"), dict) and "enable_browser_search" in new_config.get("builder", {}):
                    config["builder_enable_browser"] = coerce_bool(new_config["builder"].get("enable_browser_search"), default=False)
                # v3.0.3: UI language propagation for language-aware reports
                if "ui_language" in new_config:
                    ui_lang = str(new_config.get("ui_language", "en") or "en").strip().lower()[:10]
                    config["ui_language"] = ui_lang if ui_lang in ("en", "zh") else "en"
                # Sync cli_mode from saved settings to in-memory config
                # (cli_mode is saved via /api/settings/save, but update_config
                #  needs to pick up the latest so ai_bridge sees it)
                try:
                    _saved_cli = load_settings().get("cli_mode") or {}
                    if isinstance(_saved_cli, dict) and _saved_cli.get("enabled"):
                        config["cli_mode"] = _saved_cli
                except Exception:
                    pass
                if "cli_mode" in new_config and isinstance(new_config.get("cli_mode"), dict):
                    config["cli_mode"] = new_config["cli_mode"]
                # Apply privacy settings
                if new_config.get("privacy"):
                    from privacy import update_masker_settings
                    update_masker_settings(new_config["privacy"])
                # v6.3 (maintainer): also accept nested api_keys /
                # api_bases (the UI-side shape) so a settings change via WS
                # propagates the same as /api/settings/save. Previously this
                # handler only consumed flat `openai_api_key` style fields.
                if isinstance(new_config.get("api_keys"), dict):
                    config.setdefault("api_keys", {})
                    for _p, _val in (new_config.get("api_keys") or {}).items():
                        config["api_keys"][_p] = str(_val or "")
                if isinstance(new_config.get("api_bases"), dict):
                    config.setdefault("api_bases", {})
                    for _p, _val in (new_config.get("api_bases") or {}).items():
                        config["api_bases"][_p] = str(_val or "")
                ai_bridge.config = config
                # v6.3 (maintainer): full live reload for EVERY bridge.
                # Earlier version only reloaded the current ws-handler's
                # ai_bridge, but multiple concurrent WS clients each hold
                # their own AIBridge instance (see line ~5774) — a config
                # change from one UI tab must propagate to every running
                # pipeline, not just the one that sent the message.
                try:
                    if _LIVE_AIBRIDGES is not None:
                        _touched = 0
                        for _live_bridge in list(_LIVE_AIBRIDGES):
                            if _live_bridge is None:
                                continue
                            try:
                                _reload_summary = _live_bridge.reload_api_config(config)
                                _touched += 1
                                logger.info(
                                    "update_config: bridge reload_api_config summary=%s",
                                    _reload_summary,
                                )
                            except Exception as _one_err:
                                logger.warning("update_config: one bridge failed: %s", _one_err)
                        logger.info(
                            "update_config: hot-reloaded %d live AIBridge instance(s)", _touched,
                        )
                    else:
                        _reload_summary = ai_bridge.reload_api_config(config)
                        logger.info("update_config: reload_api_config summary=%s", _reload_summary)
                except Exception as _reload_err:
                    logger.warning("update_config: live reload failed, falling back to _setup_litellm: %s", _reload_err)
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
                        config.get("reviewer_tester_force_headful", False),
                        default=False,
                    ),
                    "browser_capture_trace": coerce_bool(config.get("browser_capture_trace", False), default=False),
                    "qa_enable_browser_use": coerce_bool(config.get("qa_enable_browser_use", False), default=False),
                    "browser_use_python": str(config.get("browser_use_python", "") or "").strip(),
                    "max_retries": coerce_int(config.get("max_retries", 3), 3, minimum=1, maximum=8),
                    "image_generation": config.get("image_generation", {}),
                    "image_generation_available": is_image_generation_available(config),
                    "analyst": _normalize_analyst_settings(config.get("analyst", {})),
                    "node_model_preferences": config.get("node_model_preferences", {}),
                })

            elif msg_type == "execute_workflow":
                # Full workflow execution
                nodes = msg.get("nodes", [])
                edges = msg.get("edges", [])
                task = asyncio.create_task(executor.execute_workflow(nodes, edges))
                _track_client_task(client_id, task)

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
                # P0-1: Autonomous mode — bridge into canonical task/run/NE system
                goal = msg.get("goal", "")
                settings_default_model = str(
                    _saved_settings.get("default_model", "") or ""
                ).strip()
                raw_frontend_model = msg.get("model", "")
                explicit_frontend_model = str(raw_frontend_model or "").strip()
                model = explicit_frontend_model or settings_default_model or "gpt-5.3-codex"
                requested_model = str(model or "gpt-5.3-codex")
                launch_model_is_user_selected = bool(explicit_frontend_model)
                difficulty = str(msg.get("difficulty", "standard")).strip().lower()
                # v5.8.6: accept 'custom' — the caller has arranged their own
                # canvas DAG. Downstream logic keys on the presence of
                # `plan.nodes` (is_custom_plan=True) rather than this label, so
                # treat 'custom' internally as 'pro' for node-count defaults
                # (pro = 7-10 nodes, matches typical custom workflows).
                if difficulty == "custom":
                    difficulty = "pro"
                # v7.1 (maintainer): accept ultra aliases
                elif difficulty in ("ultra", "product", "long_task", "ultra_mode"):
                    difficulty = "ultra"
                elif difficulty not in ("simple", "standard", "pro", "ultra"):
                    difficulty = "standard"
                requested_runtime = str(msg.get("runtime", "local")).strip().lower()
                if requested_runtime not in ("local", "openclaw"):
                    requested_runtime = "local"
                # A-1: Honor requested runtime — no forced fallback to local
                effective_runtime = requested_runtime

                # Extract recent chat history for context continuity
                chat_history = msg.get("chat_history", [])
                if not isinstance(chat_history, list):
                    chat_history = []
                safe_history = []
                allowed_roles = {"user", "agent"}
                for h in chat_history[-10:]:
                    if isinstance(h, dict) and h.get("role") and h.get("content"):
                        role = str(h["role"]).strip().lower()[:10]
                        if role not in allowed_roles:
                            continue
                        safe_history.append({
                            "role": role,
                            "content": str(h["content"])[:800],  # F7-2: Increased from 500 to preserve design context
                        })

                safe_attachments = _normalize_run_goal_attachments(msg.get("attachments", []))
                attachment_context = _build_attachment_context_block(safe_attachments)
                effective_goal = goal.strip()
                if attachment_context:
                    effective_goal = f"{effective_goal}\n\n{attachment_context}".strip()

                # ── Auto-detect model if default has no key ──
                # v6.1.2: promote kimi-k2.6-code-preview (current coding model)
                # above the legacy kimi-coding alias (kimi-k2.5). The legacy
                # endpoint 401s on the common platform.moonshot.cn key, and
                # k2.6 is both faster and the model users actually configure
                # in Settings.
                # v7.4: was `model == "gpt-5.4"` only. Real default is gpt-5.3-codex
                # at line 7261, and any user picking *any* gpt-* model with no
                # OpenAI key would die on the first call instead of falling back.
                if model and model.startswith("gpt-") and not os.environ.get("OPENAI_API_KEY"):
                    fallback_order = [
                        ("kimi-k2.6-code-preview", "KIMI_API_KEY"),
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
                                "message": f"Auto-selected model: {model}",
                            })
                            break

                # ── P0-1: Create canonical task + run + NEs ──
                canonical_context = None
                canonical_task_id = ""
                canonical_run_id = ""
                try:
                    # 1. Create task
                    ts = get_task_store()
                    task_title = goal[:80].strip() or "Interactive Task"
                    session_id = str(msg.get("session_id", msg.get("sessionId", "")) or "").strip()[:120]
                    prior_session_tasks = ts.list_tasks(session_id=session_id) if session_id else []
                    previous_session_task = prior_session_tasks[0] if prior_session_tasks else None
                    cross_session_memory_task = None
                    cross_session_memory_note = ""
                    project_memory_source_task = None
                    project_memory_digest = ""
                    if not previous_session_task:
                        cross_session_memory_task = _find_related_task_for_cross_session_memory(
                            effective_goal or goal,
                            session_id=session_id,
                        )
                        cross_session_memory_note = _build_cross_session_memory_note(cross_session_memory_task)
                    # ── P0 FIX 2026-04-05: conservative session continuation ──
                    # Same-session runs should inherit artifacts ONLY when the
                    # new goal explicitly reads like an iterative follow-up.
                    # A fresh run with a full same-type brief must start clean.
                    session_continuation = False
                    if previous_session_task:
                        try:
                            import task_classifier as _tc
                            _cur_type = _tc.classify(goal).task_type
                            _prev_title = str(previous_session_task.get("title") or "")
                            _prev_desc = str(previous_session_task.get("description") or "")
                            _prev_summary = str(previous_session_task.get("latest_summary") or "")
                            _prev_risk = str(previous_session_task.get("latest_risk") or "")
                            _prev_issues = previous_session_task.get("review_issues")
                            _prev_issue_text = " ".join(
                                str(item).strip()
                                for item in (_prev_issues if isinstance(_prev_issues, list) else [])
                                if str(item).strip()
                            )
                            _prev_seed = "\n".join(
                                item for item in [_prev_title, _prev_desc, _prev_summary, _prev_risk, _prev_issue_text] if item
                            ).strip()
                            _prev_type = _tc.classify(_prev_seed or _prev_title or _prev_desc).task_type
                            same_type = (_cur_type == _prev_type)
                            continuation_intent = _goal_requests_session_continuation(goal, safe_history)
                            session_continuation = same_type and continuation_intent
                            if not same_type:
                                logger.info(
                                    "Session continuation blocked — task type changed: %s → %s",
                                    _prev_type, _cur_type,
                                )
                            elif not continuation_intent:
                                logger.info(
                                    "Session continuation blocked — current goal does not look like an iterative edit: %s",
                                    goal[:160],
                                )
                        except Exception as _e:
                            # Fallback conservative: do NOT preserve previous
                            # artifacts if classification or heuristics fail.
                            session_continuation = False
                            logger.warning("task_classifier unavailable for continuation check: %s", _e)
                    session_context_note = ""
                    if previous_session_task and session_continuation:
                        prev_title = str(previous_session_task.get("title") or "").strip()
                        prev_summary = str(previous_session_task.get("latest_summary") or "").strip()
                        prev_status = str(previous_session_task.get("status") or "").strip()
                        if prev_title:
                            session_context_note = f"Continue editing the same session project. Previous task: {prev_title}"
                        if prev_summary:
                            session_context_note = (
                                f"{session_context_note}. Previous summary: {prev_summary}"
                                if session_context_note else
                                f"Previous summary: {prev_summary}"
                            )
                        if prev_status:
                            session_context_note = (
                                f"{session_context_note}. Previous status: {prev_status}"
                                if session_context_note else
                                f"Previous status: {prev_status}"
                            )
                        project_memory_source_task = previous_session_task
                        project_memory_digest = _build_project_memory_digest(
                            previous_session_task,
                            continuation=True,
                        )
                    elif cross_session_memory_task:
                        project_memory_source_task = cross_session_memory_task
                        project_memory_digest = _build_project_memory_digest(
                            cross_session_memory_task,
                            continuation=False,
                        )
                    # v6.1.2 FIX: persist the just-built digest into the chat
                    # session memory so the chat agent sees a populated
                    # "Project Memory Digest" block next turn. Previously this
                    # digest was only handed to the orchestrator; the chat
                    # handler kept reading an empty _SESSION_MEMORY["project_memory_digest"]
                    # because no one ever wrote it.
                    if session_id and project_memory_digest:
                        try:
                            _get_session_memory(session_id)["project_memory_digest"] = project_memory_digest
                            if project_memory_source_task:
                                _get_session_memory(session_id)["project_memory_task_id"] = str(
                                    (project_memory_source_task or {}).get("id") or ""
                                )
                        except Exception as _mem_err:
                            logger.warning("Failed to persist project_memory_digest to session memory: %s", _mem_err)
                    task_record = ts.create_task({
                        "title": task_title,
                        "description": effective_goal[:2000],
                        "session_id": session_id,
                    })
                    canonical_task_id = task_record["id"]

                    # 2. Select nodes: custom plan.nodes (OpenClaw Planner Mode)
                    #    or system template (human / fallback mode)
                    custom_plan = msg.get("plan", {})
                    custom_plan_source = str(
                        custom_plan.get("source", "")
                        if isinstance(custom_plan, dict)
                        else ""
                    ).strip().lower()
                    custom_nodes_raw = custom_plan.get("nodes", []) if isinstance(custom_plan, dict) else []
                    valid_custom_nodes = []
                    for cn in (custom_nodes_raw or [])[:20]:
                        if isinstance(cn, dict) and cn.get("nodeKey"):
                            node_label = str(cn.get("nodeLabel", cn["nodeKey"])).strip()[:80]
                            node_task = str(
                                cn.get("task")
                                or cn.get("taskDescription")
                                or cn.get("description")
                                or node_label
                            ).strip()[:500]
                            requested_node_model = str(cn.get("model", "") or "").strip()[:80]
                            effective_node_model = (
                                model
                                if not requested_node_model or requested_node_model == requested_model
                                else requested_node_model
                            )
                            valid_custom_nodes.append({
                                "key": str(cn["nodeKey"]).strip()[:50],
                                "label": node_label,
                                "task": node_task or node_label,
                                "model": effective_node_model,
                                "depends_on": [
                                    str(d).strip()[:50]
                                    for d in (cn.get("dependsOn") or [])
                                    if isinstance(d, str)
                                ][:10],
                            })
                    internal_optimize_plan = False
                    if valid_custom_nodes:
                        # OpenClaw Planner Mode — agent-defined nodes
                        nodes_def = valid_custom_nodes
                        template_id = "custom"
                        logger.info(
                            f"[run_goal] Using custom plan ({custom_plan_source or 'external'}): "
                            f"{len(nodes_def)} nodes "
                            f"[{', '.join(n['key'] for n in nodes_def)}]"
                        )
                    elif session_continuation:
                        tpl = get_template("optimize", goal=effective_goal or goal)
                        template_id = "optimize"
                        nodes_def = tpl["nodes"] if tpl else []
                        internal_optimize_plan = bool(nodes_def)
                        if internal_optimize_plan:
                            logger.info(
                                "[run_goal] Using optimize continuation flow: %s",
                                ", ".join(str(n.get("key") or "") for n in nodes_def),
                            )
                    else:
                        # Human / fallback mode — use system template
                        tpl = get_template(difficulty, goal=effective_goal or goal)
                        template_id = difficulty
                        nodes_def = tpl["nodes"] if tpl else get_template("standard", goal=effective_goal or goal)["nodes"]
                        if not tpl:
                            template_id = "standard"

                    # 3. Create run
                    rs = get_run_store()
                    run_timeout_seconds = _estimate_run_timeout_seconds(nodes_def)
                    run_record = rs.create_run({
                        "task_id": canonical_task_id,
                        "runtime": effective_runtime,
                        "workflow_template_id": template_id,
                        "timeout_seconds": run_timeout_seconds,
                        "trigger_source": (
                            custom_plan_source
                            if valid_custom_nodes and custom_plan_source
                            else "openclaw_planner"
                            if valid_custom_nodes
                            else "optimization_pass"
                            if internal_optimize_plan
                            else "ui"
                        ),
                    })
                    canonical_run_id = run_record["id"]
                    ts.link_run(canonical_task_id, canonical_run_id)

                    # 4. Create NEs from plan (custom or template)
                    nes_store = get_node_execution_store()
                    created_nes = []
                    for node_def in nodes_def:
                        from agent_skills import resolve_skill_names_for_goal
                        requested_node_model = str(node_def.get("model", "") or "").strip()
                        # Start from the session-level launch model, then let the bridge
                        # resolve the role-aware preferred route so canonical node cards
                        # reflect the actual model that will be attempted first.
                        effective_node_model = model
                        node_key = str(node_def["key"] or "").strip()
                        normalized_node_key = normalize_node_role(node_key) or node_key
                        input_summary = _compose_node_input_summary(
                            node_def.get("task", node_def.get("label", node_key)),
                            effective_goal=effective_goal,
                            session_context_note=session_context_note,
                            cross_session_memory_note=cross_session_memory_note,
                        )
                        preferred_provider = ""
                        try:
                            preview_node = {
                                "type": normalized_node_key,
                                "model": effective_node_model,
                                "model_is_default": (not launch_model_is_user_selected and not requested_node_model),
                                "goal": effective_goal,
                            }
                            effective_node_model = ai_bridge.preferred_model_for_node(
                                preview_node,
                                effective_node_model,
                            )
                            preferred_provider = str(
                                ai_bridge._resolve_model(effective_node_model).get("provider", "") or ""
                            )[:60]
                        except Exception:
                            effective_node_model = effective_node_model or requested_node_model or model
                            preferred_provider = ""
                        # v7.1g (maintainer): if CLI mode is enabled and
                        # the node is CLI-eligible, replace the API model name
                        # with `cli:<choice>` so the UI shows the actual route
                        # from the moment the NE is created — not the stale
                        # API placeholder that was previously visible all the
                        # way until CLI execution finished.
                        try:
                            _cli_cfg = (_saved_settings or {}).get("cli_mode") or {}
                            if isinstance(_cli_cfg, dict) and _cli_cfg.get("enabled"):
                                _CLI_ELIGIBLE = {
                                    "planner", "planner_degraded", "analyst", "uidesign", "scribe",
                                    "builder", "merger", "polisher", "reviewer", "patcher",
                                    "tester", "debugger", "deployer", "router",
                                }
                                if normalized_node_key in _CLI_ELIGIBLE:
                                    _node_overrides = _cli_cfg.get("node_cli_overrides") or {}
                                    _override = (
                                        (_node_overrides.get(node_key.lower()) if node_key else None)
                                        or _node_overrides.get(normalized_node_key, "")
                                    )
                                    if isinstance(_override, dict):
                                        _cli_pick = str(_override.get("cli") or "").strip().lower()
                                        _cli_model = str(_override.get("model") or "").strip()
                                    elif isinstance(_override, str) and _override:
                                        _cli_pick = _override.strip().lower()
                                        _cli_model = ""
                                    else:
                                        _cli_pick = str(_cli_cfg.get("preferred_cli") or "").strip().lower()
                                        _cli_model = str(_cli_cfg.get("preferred_model") or "").strip()
                                    if _cli_pick:
                                        effective_node_model = (
                                            f"cli:{_cli_pick}:{_cli_model}" if _cli_model else f"cli:{_cli_pick}"
                                        )
                                        preferred_provider = "cli"
                        except Exception:
                            pass
                        # Set per-role timeout so the watchdog uses the correct
                        # limit instead of the global DEFAULT_NODE_TIMEOUT_S.
                        ne_timeout = _node_timeout_hint_seconds(normalized_node_key)
                        ne = nes_store.create_node_execution({
                            "run_id": canonical_run_id,
                            "node_key": node_key,
                            "node_label": node_def["label"],
                            "input_summary": input_summary,
                            "depends_on_keys": node_def.get("depends_on", []),
                            "assigned_model": effective_node_model,
                            "assigned_provider": preferred_provider,
                            "loaded_skills": resolve_skill_names_for_goal(
                                normalized_node_key,
                                str(input_summary or effective_goal or goal),
                            ),
                            "timeout_seconds": ne_timeout,
                        })
                        created_nes.append(ne)
                    ne_ids = [ne["id"] for ne in created_nes]
                    rs.update_run(canonical_run_id, {"node_execution_ids": ne_ids})

                    # 5. Transition run → running, task → executing
                    rs.transition_run(canonical_run_id, "running")
                    if task_record.get("status") in ("backlog", "planned"):
                        ts.transition_task(canonical_task_id, "executing")

                    # 6. Build canonical context for orchestrator
                    canonical_context = {
                        "task_id": canonical_task_id,
                        "run_id": canonical_run_id,
                        "node_executions": [
                            {
                                "id": ne["id"],
                                "node_key": ne["node_key"],
                                "node_label": ne.get("node_label", ne["node_key"]),
                                "input_summary": ne.get("input_summary", ""),
                                "depends_on_keys": ne.get("depends_on_keys", []),
                                "assigned_model": ne.get("assigned_model", model),
                            }
                            for ne in created_nes
                        ],
                        "is_custom_plan": bool(valid_custom_nodes or internal_optimize_plan),
                        "session_id": session_id,
                        "session_continuation": session_continuation,
                        "session_context_note": session_context_note,
                        "cross_session_memory_note": cross_session_memory_note,
                        "project_memory_digest": project_memory_digest,
                        "project_memory_source_task_id": str((project_memory_source_task or {}).get("id") or ""),
                        "effective_goal": effective_goal,
                        "state_snapshot": {
                            "created_at": time.time(),
                            "difficulty": difficulty,
                            "template_id": template_id,
                            "requested_runtime": requested_runtime,
                            "effective_runtime": effective_runtime,
                            "effective_goal": effective_goal,
                            "session_id": session_id,
                            "session_continuation": session_continuation,
                            "previous_task_id": str((previous_session_task or {}).get("id") or ""),
                            "cross_session_memory_task_id": str((cross_session_memory_task or {}).get("id") or ""),
                            "project_memory_source_task_id": str((project_memory_source_task or {}).get("id") or ""),
                            "node_order": [str(ne.get("node_key") or "") for ne in created_nes],
                            "depends_graph": {
                                str(ne.get("node_key") or ""): list(ne.get("depends_on_keys") or [])
                                for ne in created_nes
                            },
                        },
                    }

                    # 7. Notify frontend immediately — task appears in board
                    task_snapshot = ts.get_task(canonical_task_id)
                    run_snapshot = rs.get_run(canonical_run_id)
                    run_goal_ack_payload = {
                        "taskId": canonical_task_id,
                        "runId": canonical_run_id,
                        "task": _task_to_api(task_snapshot) if task_snapshot else None,
                        "run": run_snapshot,
                        "nodeExecutions": created_nes,
                        "templateId": template_id,
                        "requestedRuntime": requested_runtime,
                        "effectiveRuntime": effective_runtime,
                        "sessionContinuation": session_continuation,
                        "crossSessionMemory": bool(cross_session_memory_note),
                        "crossSessionMemoryTaskId": str((cross_session_memory_task or {}).get("id") or ""),
                        "projectMemory": bool(project_memory_digest),
                        "projectMemoryTaskId": str((project_memory_source_task or {}).get("id") or ""),
                    }
                    # The sender may disconnect immediately after posting `run_goal`.
                    # ACK delivery to the requester must not block task creation or
                    # the broadcast that keeps the desktop UI hydrated.
                    await send_event({
                        "type": "run_goal_ack",
                        "payload": run_goal_ack_payload,
                    })
                    # P0-FIX: Also broadcast run_goal_ack to ALL other WS clients
                    # so the Evermind App frontend (a separate WS connection) can
                    # call buildPlanNodes() and populate the canvas.
                    await _broadcast_ws_event({
                        "type": "run_goal_ack",
                        "payload": run_goal_ack_payload,
                    }, exclude_ws=ws)
                    # Broadcast task/run creation to all clients
                    await _broadcast_ws_event({
                        "type": "task_created",
                        "payload": {"task": _task_to_api(task_snapshot) if task_snapshot else None},
                    })
                    await _broadcast_ws_event({
                        "type": "run_created",
                        "payload": {"run": run_snapshot},
                    })

                    logger.info(
                        f"[P0-1] Created canonical task={canonical_task_id} "
                        f"run={canonical_run_id} template={template_id} "
                        f"model={model} NEs={len(created_nes)} "
                        f"session_continuation={session_continuation}"
                    )
                except Exception as e:
                    logger.warning(f"[P0-1] Failed to create canonical context: {e}")
                    # Graceful degradation: orchestrator still runs without canonical bridge

                # ═══ A-2: Branch by runtime ═══
                if effective_runtime == "openclaw" and canonical_run_id:
                    # ── OpenClaw Direct Mode: dispatch first ready nodes, let callback loop drive the rest ──
                    async def _openclaw_initial_dispatch():
                        try:
                            peer_clients = max(0, len(connected_clients) - 1)
                            if peer_clients <= 0:
                                await send_event({
                                    "type": "system_info",
                                    "message": (
                                        "OpenClaw Direct Mode 已启动，但当前没有检测到独立的 OpenClaw 执行端连接。"
                                        f"若 {OPENCLAW_DISPATCH_ACK_TIMEOUT_S}s 内没有 ack/progress，本次运行会自动失败，避免假性卡死。"
                                    ),
                                })
                            ready = _auto_chain_next_node(canonical_run_id)
                            if isinstance(ready, list) and ready:
                                for ne_id in ready:
                                    _transition_node_if_needed(ne_id, "running")
                                    run_snap = get_run_store().get_run(canonical_run_id)
                                    active = list(run_snap.get("active_node_execution_ids", [])) if run_snap else []
                                    if ne_id not in active:
                                        active.append(ne_id)
                                    run_after = get_run_store().update_run(canonical_run_id, {
                                        "current_node_execution_id": ne_id,
                                        "active_node_execution_ids": active,
                                    })
                                    dispatch_payload = _build_dispatch_payload(
                                        canonical_run_id, ne_id, launch_triggered=True,
                                    )
                                    await _broadcast_ws_event({
                                        "type": "evermind_dispatch_node",
                                        "payload": {
                                            **dispatch_payload,
                                            "_runVersion": run_after.get("version", 0) if run_after else 0,
                                        },
                                    })
                                    _start_openclaw_dispatch_watchdog(
                                        run_id=canonical_run_id,
                                        node_execution_id=ne_id,
                                    )
                                    logger.info(f"[OpenClaw Direct] Dispatched node {ne_id} for run {canonical_run_id}")
                                await send_event({
                                    "type": "system_info",
                                    "message": f"OpenClaw Direct Mode: dispatched {len(ready)} node(s). Waiting for callbacks.",
                                })
                            else:
                                logger.warning(f"[OpenClaw Direct] No ready nodes for run {canonical_run_id}")
                        except Exception as e:
                            logger.error(f"[OpenClaw Direct] Initial dispatch failed: {e}")
                            if canonical_run_id:
                                _transition_run_if_needed(canonical_run_id, "failed")
                                if canonical_task_id:
                                    get_task_store().project_task_from_run(
                                        canonical_task_id, run_status="failed", run_id=canonical_run_id,
                                    )

                    task = asyncio.create_task(_openclaw_initial_dispatch())
                    _track_client_task(client_id, task, cancel_on_disconnect=False)
                else:
                    # ── Local Mode: run the full orchestrator ──
                    # ── Launch orchestrator with canonical bridge ──
                    async def _run_with_canonical_completion():
                        """Wrapper that syncs canonical run status on orchestrator completion."""
                        try:
                            report = await orchestrator.run(
                                effective_goal, model,
                                conversation_history=safe_history,
                                difficulty=difficulty,
                                canonical_context=canonical_context,
                            )
                            # P0-1: Sync canonical run to terminal state
                            if canonical_run_id and canonical_task_id:
                                try:
                                    success = bool(report.get("success"))
                                    run_status = "done" if success else "failed"
                                    _transition_run_if_needed(canonical_run_id, run_status)
                                    summary = str(report.get("summary", "") or "").strip()[:500]
                                    remaining_risks = report.get("remaining_risks")
                                    if not isinstance(remaining_risks, list):
                                        remaining_risks = report.get("risks")
                                    remaining_risks = remaining_risks if isinstance(remaining_risks, list) else []
                                    run_metrics = _coalesce_run_metrics(
                                        canonical_run_id,
                                        report.get("total_tokens"),
                                        report.get("total_cost"),
                                    )
                                    preview_url = _resolve_completion_preview_url(report)
                                    run_updates: Dict[str, Any] = {}
                                    if summary:
                                        run_updates["summary"] = summary
                                    if remaining_risks:
                                        run_updates["risks"] = remaining_risks
                                    if run_metrics["total_tokens"] > 0 or "total_tokens" in report:
                                        run_updates["total_tokens"] = run_metrics["total_tokens"]
                                    if run_metrics["total_cost"] > 0 or "total_cost" in report:
                                        run_updates["total_cost"] = run_metrics["total_cost"]
                                    run_store = get_run_store()
                                    if run_updates:
                                        run_store.update_run(canonical_run_id, run_updates)
                                    run_final = run_store.get_run(canonical_run_id)
                                    # Project task from final run state
                                    task_final = get_task_store().project_task_from_run(
                                        canonical_task_id,
                                        run_status=run_status,
                                        summary=summary or None,
                                        remaining_risks=remaining_risks,
                                        run_id=canonical_run_id,
                                    )
                                    if run_final:
                                        # Include full report data so frontend can build completion card
                                        oc_payload: Dict[str, Any] = {
                                                "runId": canonical_run_id,
                                                "taskId": canonical_task_id,
                                                "finalResult": "success" if success else "failed",
                                                "success": success,
                                                "summary": run_final.get("summary", summary),
                                                "risks": run_final.get("risks", remaining_risks),
                                                "totalTokens": run_final.get("total_tokens", run_metrics["total_tokens"]),
                                                "totalCost": run_final.get("total_cost", run_metrics["total_cost"]),
                                                "runtime": run_final.get("runtime", ""),
                                                "timestamp": int(time.time() * 1000),
                                                "_runVersion": run_final.get("version", 0),
                                                "_taskVersion": task_final.get("version", 0) if task_final else 0,
                                        }
                                        if preview_url:
                                            oc_payload["previewUrl"] = preview_url
                                        # §FIX: Include orchestrator report data for completion card
                                        if report:
                                            oc_payload["goal"] = str(report.get("goal", ""))[:200]
                                            oc_payload["difficulty"] = str(report.get("difficulty", "standard"))
                                            oc_payload["total_subtasks"] = report.get("total_subtasks", 0)
                                            oc_payload["completed"] = report.get("completed", 0)
                                            oc_payload["failed"] = report.get("failed", 0)
                                            oc_payload["total_retries"] = report.get("total_retries", 0)
                                            oc_payload["duration_seconds"] = report.get("duration_seconds", 0)
                                            oc_payload["subtasks"] = [
                                                {
                                                    "id": st.get("id", ""),
                                                    "agent": st.get("agent", ""),
                                                    "status": st.get("status", ""),
                                                    "retries": st.get("retries", 0),
                                                    "work_summary": st.get("work_summary", []),
                                                    "files_created": st.get("files_created", []),
                                                    "error": str(st.get("error", ""))[:300],
                                                }
                                                for st in report.get("subtasks", [])
                                            ]
                                        await _broadcast_ws_event({
                                            "type": "openclaw_run_complete",
                                            "payload": oc_payload,
                                        })
                                    logger.info(
                                        f"[P0-1] Canonical run {canonical_run_id} → {run_status}"
                                    )
                                except Exception as e:
                                    logger.warning(f"[P0-1] Failed to finalize canonical run: {e}")
                            # Store pipeline result in session memory for chat context
                            if report and session_id:
                                try:
                                    _store_pipeline_result_in_session(session_id, report)
                                except Exception:
                                    pass
                            return report
                        except asyncio.CancelledError:
                            if canonical_run_id and canonical_task_id:
                                try:
                                    cancel_result = _cancel_run_cascade(canonical_run_id)
                                    run_final = cancel_result.get("run") or get_run_store().get_run(canonical_run_id)
                                    task_final = cancel_result.get("task") or get_task_store().get_task(canonical_task_id)
                                    if run_final:
                                        await _broadcast_ws_event({
                                            "type": "openclaw_run_complete",
                                            "payload": {
                                                "runId": canonical_run_id,
                                                "taskId": canonical_task_id,
                                                "finalResult": "cancelled",
                                                "success": False,
                                                "summary": run_final.get("summary", ""),
                                                "risks": run_final.get("risks", []),
                                                "totalTokens": run_final.get("total_tokens", 0),
                                                "totalCost": run_final.get("total_cost", 0.0),
                                                "runtime": run_final.get("runtime", ""),
                                                "timestamp": int(time.time() * 1000),
                                                "_runVersion": run_final.get("version", 0),
                                                "_taskVersion": task_final.get("version", 0) if task_final else 0,
                                            },
                                        })
                                except Exception:
                                    pass
                            raise
                        except Exception as e:
                            # If orchestrator crashes, mark canonical run as failed
                            if canonical_run_id and canonical_task_id:
                                try:
                                    _transition_run_if_needed(canonical_run_id, "failed")
                                    run_final = get_run_store().get_run(canonical_run_id)
                                    task_final = get_task_store().project_task_from_run(
                                        canonical_task_id,
                                        run_status="failed",
                                        run_id=canonical_run_id,
                                    )
                                    if run_final:
                                        await _broadcast_ws_event({
                                            "type": "openclaw_run_complete",
                                            "payload": {
                                                "runId": canonical_run_id,
                                                "taskId": canonical_task_id,
                                                "finalResult": "failed",
                                                "success": False,
                                                "summary": "",
                                                "risks": run_final.get("risks", []),
                                                "totalTokens": run_final.get("total_tokens", 0),
                                                "totalCost": run_final.get("total_cost", 0.0),
                                                "runtime": run_final.get("runtime", ""),
                                                "timestamp": int(time.time() * 1000),
                                                "_runVersion": run_final.get("version", 0),
                                                "_taskVersion": task_final.get("version", 0) if task_final else 0,
                                            },
                                        })
                                except Exception:
                                    pass
                            raise

                    task = asyncio.create_task(_run_with_canonical_completion())
                    _track_client_task(client_id, task, cancel_on_disconnect=False)

            elif msg_type == "stop":
                executor.stop()
                orchestrator.stop()
                # Cancel tracked async tasks
                for t in list(_active_tasks.get(client_id, [])):
                    _cancel_tracked_task(t)
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
                _cancel_openclaw_dispatch_watchdog(str(ne_id or ""))
                if payload.get("accepted") is False:
                    await _fail_openclaw_dispatch(
                        run_id=str(payload.get("runId", "") or ""),
                        node_execution_id=str(ne_id or ""),
                        summary=(
                            f"OpenClaw execution client rejected node '{payload.get('nodeLabel') or payload.get('nodeKey') or ne_id}'. "
                            "Direct Mode run aborted."
                        ),
                        risks=[
                            "OpenClaw runtime rejected the dispatched node.",
                        ],
                    )
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
                _cancel_openclaw_dispatch_watchdog(ne_id)
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
                if payload.get("progress") is not None:
                    update_data["progress"] = payload.get("progress")
                if payload.get("phase") is not None:
                    update_data["phase"] = str(payload.get("phase") or "")
                if "loadedSkills" in payload:
                    update_data["loaded_skills"] = payload.get("loadedSkills") or []
                if "activityLog" in payload:
                    update_data["activity_log"] = payload.get("activityLog") or []
                if "referenceUrls" in payload:
                    update_data["reference_urls"] = payload.get("referenceUrls") or []
                if update_data:
                    ne_store.update_node_execution(ne_id, update_data)
                    ne_latest = ne_store.get_node_execution(ne_id)
                    if ne_latest:
                        payload["_neVersion"] = ne_latest.get("version", 0)
                        payload["progress"] = ne_latest.get("progress", payload.get("progress", 0))
                        payload["phase"] = ne_latest.get("phase", payload.get("phase", ""))
                        payload["loadedSkills"] = ne_latest.get("loaded_skills", payload.get("loadedSkills", []))
                        payload["activityLog"] = ne_latest.get("activity_log", payload.get("activityLog", []))
                        payload["referenceUrls"] = ne_latest.get("reference_urls", payload.get("referenceUrls", []))
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
                if ne_id:
                    _cancel_openclaw_dispatch_watchdog(str(ne_id))
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
                                allowed_statuses = {
                                    "running",
                                    "passed",
                                    "failed",
                                    "blocked",
                                    "waiting_approval",
                                    "skipped",
                                    "cancelled",
                                }
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
                                    if "progress" in payload:
                                        update_data["progress"] = payload["progress"]
                                    else:
                                        update_data["progress"] = 100 if status in ("passed", "failed", "skipped", "cancelled") else 5 if status == "running" else 0
                                    if "phase" in payload:
                                        update_data["phase"] = str(payload.get("phase") or "")
                                    if "inputSummary" in payload:
                                        update_data["input_summary"] = payload["inputSummary"]
                                    if "assignedModel" in payload:
                                        update_data["assigned_model"] = payload["assignedModel"]
                                    if "assignedProvider" in payload:
                                        update_data["assigned_provider"] = payload["assignedProvider"]
                                    if "partialOutputSummary" in payload:
                                        update_data["output_summary"] = payload["partialOutputSummary"]
                                    if "loadedSkills" in payload:
                                        update_data["loaded_skills"] = payload.get("loadedSkills") or []
                                    if "activityLog" in payload:
                                        update_data["activity_log"] = payload.get("activityLog") or []
                                    if "referenceUrls" in payload:
                                        update_data["reference_urls"] = payload.get("referenceUrls") or []
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
                        payload["nodeKey"] = ne_latest.get("node_key", payload.get("nodeKey", ""))
                        payload["nodeLabel"] = ne_latest.get("node_label", payload.get("nodeLabel", ""))
                        payload["assignedModel"] = ne_latest.get("assigned_model", payload.get("assignedModel", ""))
                        payload["assignedProvider"] = ne_latest.get("assigned_provider", payload.get("assignedProvider", ""))
                        payload["tokensUsed"] = ne_latest.get("tokens_used", payload.get("tokensUsed", 0))
                        payload["cost"] = ne_latest.get("cost", payload.get("cost", 0.0))
                        payload["inputSummary"] = ne_latest.get("input_summary", payload.get("inputSummary", ""))
                        payload["outputSummary"] = ne_latest.get("output_summary", payload.get("outputSummary", ""))
                        payload["progress"] = ne_latest.get("progress", payload.get("progress", 0))
                        payload["phase"] = ne_latest.get("phase", payload.get("phase", ""))
                        payload["loadedSkills"] = ne_latest.get("loaded_skills", payload.get("loadedSkills", []))
                        payload["activityLog"] = ne_latest.get("activity_log", payload.get("activityLog", []))
                        payload["referenceUrls"] = ne_latest.get("reference_urls", payload.get("referenceUrls", []))
                        payload["errorMessage"] = ne_latest.get("error_message", payload.get("errorMessage", ""))
                        payload["artifactIds"] = ne_latest.get("artifact_ids", payload.get("artifactIds", []))
                        payload["startedAt"] = ne_latest.get("started_at", payload.get("startedAt", 0))
                        payload["endedAt"] = ne_latest.get("ended_at", payload.get("endedAt", 0))
                        payload["createdAt"] = ne_latest.get("created_at", payload.get("createdAt", 0))
                if payload.get("runId"):
                    run_latest = get_run_store().get_run(str(payload["runId"]))
                    if run_latest:
                        payload["_runVersion"] = run_latest.get("version", 0)
                        payload["activeNodeExecutionIds"] = run_latest.get("active_node_execution_ids", [])
                await broadcast_connector_event({"type": "openclaw_node_update", "payload": payload})

                # ── P1-2B: Auto-chain next node / auto-complete run ──
                run_id_for_chain = str(payload.get("runId", "")).strip()
                # Trigger auto-chain on any terminal status (passed/skipped/failed)
                # so downstream nodes aren't stuck forever when a parent fails.
                # Also allow for all runtime modes (local + openclaw) since the
                # orchestrator broadcasts openclaw_node_update for local runs too.
                if status in ("passed", "skipped", "failed") and run_id_for_chain:
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
                                _start_openclaw_dispatch_watchdog(
                                    run_id=run_id_for_chain,
                                    node_execution_id=chain_ne_id,
                                )
                        elif chain_result == "__ALL_DONE__":
                            # All nodes terminal — auto-complete the run
                            # Check if any NEs failed to determine success vs partial failure
                            all_nes = get_node_execution_store().list_node_executions(run_id=run_id_for_chain) or []
                            has_failures = any(ne.get("status") == "failed" for ne in all_nes)
                            run_status = "failed" if has_failures else "done"
                            final_result = "partial_failure" if has_failures else "success"
                            logger.info(f"[AutoChain] All nodes terminal for run {run_id_for_chain} — auto-completing ({run_status})")
                            _transition_run_if_needed(run_id_for_chain, run_status)
                            run_final = get_run_store().get_run(run_id_for_chain)
                            run_metrics = _coalesce_run_metrics(
                                run_id_for_chain,
                                run_final.get("total_tokens", 0) if run_final else 0,
                                run_final.get("total_cost", 0.0) if run_final else 0.0,
                            )
                            if run_final and (
                                run_final.get("total_tokens", 0) != run_metrics["total_tokens"]
                                or abs(_coerce_float(run_final.get("total_cost", 0.0), 0.0) - run_metrics["total_cost"]) > 1e-9
                            ):
                                run_final = get_run_store().update_run(run_id_for_chain, {
                                    "total_tokens": run_metrics["total_tokens"],
                                    "total_cost": run_metrics["total_cost"],
                                }) or get_run_store().get_run(run_id_for_chain)
                            task_id_for_chain = run_final.get("task_id", "") if run_final else ""
                            # Project run completion to task
                            task_final = None
                            if task_id_for_chain:
                                ts = get_task_store()
                                task_final = ts.project_task_from_run(
                                    task_id_for_chain,
                                    run_status=run_status,
                                    run_id=run_id_for_chain,
                                    summary=run_final.get("summary", "") if run_final else "",
                                    remaining_risks=run_final.get("risks", []) if run_final else [],
                                )
                            preview_url = _resolve_completion_preview_url()
                            complete_payload = {
                                "runId": run_id_for_chain,
                                "taskId": task_id_for_chain,
                                "finalResult": final_result,
                                "autoCompleted": True,
                                "summary": run_final.get("summary", "") if run_final else "",
                                "risks": run_final.get("risks", []) if run_final else [],
                                "totalTokens": run_final.get("total_tokens", run_metrics["total_tokens"]) if run_final else run_metrics["total_tokens"],
                                "totalCost": run_final.get("total_cost", run_metrics["total_cost"]) if run_final else run_metrics["total_cost"],
                                "timestamp": int(time.time() * 1000),
                            }
                            if preview_url:
                                complete_payload["previewUrl"] = preview_url
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
                run_metrics = _coalesce_run_metrics(run_id, payload.get("totalTokens"), payload.get("totalCost"))
                preview_url = _resolve_completion_preview_url(payload)
                logger.info(f"[OpenClaw] Run complete: run={run_id} task={task_id} result={final_result}")
                try:
                    if run_id:
                        target_status = "done" if run_success else "failed"
                        _transition_run_if_needed(run_id, target_status)
                        get_run_store().update_run(run_id, {
                            "summary": run_summary,
                            "risks": run_risks,
                            "total_tokens": run_metrics["total_tokens"],
                            "total_cost": run_metrics["total_cost"],
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
                payload["totalTokens"] = run_metrics["total_tokens"]
                payload["totalCost"] = run_metrics["total_cost"]
                if preview_url:
                    payload["previewUrl"] = preview_url
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

                # P0-4: Emit preview_ready for OpenClaw runs if HTML output exists
                # The local Orchestrator does this automatically, but for OpenClaw
                # Direct Mode the orchestrator doesn't handle execution — so we need
                # to scan the output directory after completion and emit preview_ready.
                if run_success and preview_url:
                    try:
                        _task_key, preview_file = latest_preview_artifact()
                        preview_event = {
                            "type": "preview_ready",
                            "preview_url": preview_url,
                            "files": [str(preview_file)] if preview_file else [],
                            "output_dir": str(OUTPUT_DIR),
                            "final": True,
                        }
                        _maybe_auto_sync_delivery_artifacts(preview_event)
                        await broadcast_connector_event(preview_event)
                        logger.info(f"[OpenClaw] Emitted preview_ready: {preview_url}")
                    except Exception as e:
                        logger.warning(f"[OpenClaw] preview_ready scan failed: {e}")

            # ─────────────────────────────────────────
            # Direct Chat Agent
            # ─────────────────────────────────────────
            elif msg_type == "chat_message":
                chat_text = str(msg.get("message", "")).strip()
                # Resolve model: frontend "Auto" sends empty string
                _raw_chat_model = str(msg.get("model", "")).strip()
                if not _raw_chat_model:
                    _raw_chat_model = str(_saved_settings.get("default_model", "")).strip() or "gpt-5.4-mini"
                chat_model = _raw_chat_model
                conv_id = str(msg.get("conversation_id", ""))
                session_id = str(msg.get("session_id", ""))
                chat_history = msg.get("history", [])
                # v6.5 Phase 3: mode defaults to "act" for backward compat.
                chat_mode = str(msg.get("chat_mode") or msg.get("mode") or "act").strip().lower()
                if chat_mode not in CHAT_MODES:
                    chat_mode = "act"

                if not chat_text:
                    await ws.send_json({"type": "chat_error", "conversation_id": conv_id, "error": "Empty message"})
                    continue

                # ACK immediately
                await ws.send_json({"type": "chat_ack", "conversation_id": conv_id})

                # Store user message in session memory
                _store_chat_message_in_session(session_id, "user", chat_text)

                # v6.1.2: on-demand project-memory rehydration so the chat can
                # reference the latest 3D-shooter run (or any ongoing project)
                # even before any run_goal turn populates _SESSION_MEMORY.
                # Previously the digest was only stored during run_goal, so a
                # fresh chat turn showed an empty "Project Memory Digest" block.
                try:
                    _sess_mem_for_chat = _get_session_memory(session_id) if session_id else None
                    if isinstance(_sess_mem_for_chat, dict) and not str(_sess_mem_for_chat.get("project_memory_digest") or "").strip():
                        _related_task = _find_related_task_for_cross_session_memory(
                            chat_text, session_id=session_id,
                        )
                        if _related_task:
                            _fresh_digest = _build_project_memory_digest(_related_task, continuation=False)
                            if _fresh_digest:
                                _sess_mem_for_chat["project_memory_digest"] = _fresh_digest
                                _sess_mem_for_chat["project_memory_task_id"] = str(_related_task.get("id") or "")
                                logger.info(
                                    "[ChatMemory] Rehydrated project_memory_digest for session %s from task %s (%d chars)",
                                    session_id[:12], str(_related_task.get("id"))[:12], len(_fresh_digest),
                                )
                except Exception as _mem_rehydrate_err:
                    logger.warning("chat project-memory rehydration failed: %s", _mem_rehydrate_err)

                # Build messages for LLM
                pipeline_ctx = _build_pipeline_context_for_chat(session_id)
                # v6.4.29 (maintainer): Active Project injection.
                # Without this, users report the chat saying "我没看到你的
                # 游戏前端源码" even though the pipeline just wrote a complete
                # index.html to OUTPUT_DIR. The block tells the agent exactly
                # where the latest artifact is so it can file_ops read it
                # directly instead of asking the user to "mount" a directory.
                # Compact version (≤ 250 chars) for OpenAI-family to avoid
                # relay 403 on large system prompts.
                # v6.3 (maintainer): detect OpenAI-family provider so we
                # can skip the extended "Browser Virtuosity" macro / WASD /
                # mouse_click script section — those automation-style phrases
                # trip OpenAI-route content filters (observed 403 "Your
                # request was blocked" from relay on a 6KB system prompt
                # with gpt-5.4). Core tool contracts are preserved; only the
                # verbose scripting examples are dropped. Kimi / Anthropic
                # /local models still get the full prompt.
                _chat_model_info_preview = MODEL_REGISTRY.get(chat_model, {})
                _chat_provider_preview = str(_chat_model_info_preview.get("provider") or "").lower()
                _openai_family = _chat_provider_preview in ("openai",)
                if _openai_family:
                    system_prompt = (
                        "You are Evermind's Chat Agent — a fast, senior-engineer coding assistant.\n"
                        "Single turn, single conversation: no pipeline, no multi-step planning unless asked.\n\n"
                        "## Core discipline (v6.4 — maintainer)\n"
                        "- Go straight to the answer. No preamble like \"Sure, I'll help you...\".\n"
                        "- Match response length to task complexity. Trivial Q = one line. Code fix = just the diff + one-line why.\n"
                        "- Only use emojis if the user writes with emojis first. Never as UI/icons.\n"
                        "- Respond in the user's language (Chinese if they write Chinese, English if English).\n\n"
                        "## Before you edit code\n"
                        "1. If the user names a file/symbol you haven't seen, call `file_ops search` or `file_ops read` FIRST.\n"
                        "   Never edit a file you haven't read in this turn (unless creating it).\n"
                        "2. If they ask \"where is X\" / \"how does Y work\", use `file_ops search` with a glob — do not guess.\n"
                        "3. Prefer absolute paths. Workspace root is the cwd printed in env context below.\n\n"
                        "## When you edit\n"
                        "- **STRONGLY PREFER `file_ops edit`** (old_string/new_string) over `file_ops write`. "
                        "Writing a full HTML/JS file of 30KB+ easily blows the output-token budget, which truncates "
                        "your tool_call arguments mid-string → JSON parse error → the edit silently fails and you'll "
                        "be forced to retry. For big files ALWAYS use multiple small edits, one feature at a time.\n"
                        "- Use `file_ops edit` with exact `old_string` → `new_string`. old_string must match the file character-for-character, including whitespace and comments. If it won't be unique, include 2-3 surrounding lines.\n"
                        "- Use `file_ops write` ONLY for NEW files (<10KB) or when the user explicitly asks for a full rewrite.\n"
                        "- Never paste a full file back into chat when a 5-line edit will do.\n"
                        "- After editing: one sentence — \"Edited <path>: <what changed>.\" Then stop.\n\n"
                        "## When you answer without editing\n"
                        "- Quote the exact line / function signature you're referencing (with path).\n"
                        "- If you're uncertain, say so in one clause, then give your best guess — don't hedge for a paragraph.\n\n"
                        "## Project rules\n"
                        "If `CLAUDE.md`, `AGENTS.md`, or `.cursorrules` exists in the workspace, read it once at the start of a coding task and honor its conventions (style, commands, branch rules). Do not re-read it every turn.\n\n"
                        "## Destructive actions\n"
                        "Confirm before: deleting files, `rm -rf`, force-push, dropping DB, running migrations. Read-only ops don't need confirmation.\n\n"
                        "## Code quality\n"
                        "- Complete code only — no TODOs, placeholders, stubs.\n"
                        "- Responsive design, semantic HTML5, inline SVG icons (never emoji).\n"
                        "- Balanced braces, null-guarded DOM queries, try/catch async.\n\n"
                        "## Tools\n"
                        "- `file_ops`: read / write / edit / list / search / delete files. Prefer absolute paths.\n"
                        "- `browser`: drive the embedded Chromium. Call `observe` first to enumerate elements (each gets a ref-N index), then `click(ref=N)`, `fill(selector,text)`, `scroll(direction)`, `screenshot()`.\n\n"
                        "## Workflow Composer\n"
                        "When the user asks to design or rearrange the node pipeline, emit a fenced JSON block tagged `evermind-workflow`:\n"
                        "```evermind-workflow\n"
                        "{\"nodes\":[{\"agent\":\"planner\",\"depends_on\":[]},{\"agent\":\"builder\",\"depends_on\":[\"planner\"]}]}\n"
                        "```\n"
                        "Agent keys: planner, analyst, imagegen, spritesheet, assetimport, uidesign, scribe, builder, polisher, merger, reviewer, tester, debugger, deployer, patcher.\n\n"
                        "## Project Memory\n"
                        "`Recent Pipeline Results` / `Current Canvas Workflow` / `Project Memory Digest` below are the user's ground truth — cite them when relevant.\n\n"
                        "## Language\n"
                        "Respond in the same language the user writes in (Chinese/English)."
                        + pipeline_ctx
                        + _build_active_project_context(compact=True, session_id=session_id)
                        + _chat_auto_pre_read_snapshot(chat_text, session_id, kimi_compact=False)
                    )
                else:
                    system_prompt = (
                    "You are the Evermind Chat assistant — a direct conversation mode "
                    "inside the Evermind multi-agent app builder.\n\n"
                    "## Coding Skills\n"
                    "When asked to write or modify code, follow this workflow:\n"
                    "1. Understand: clarify the requirements before writing\n"
                    "2. Plan: outline the approach in 2-3 sentences\n"
                    "3. Execute: write complete, working code\n"
                    "4. Verify: check for common issues before delivering\n\n"
                    "## Code Quality Rules\n"
                    "- Complete code only — no TODOs, placeholders, or stubs\n"
                    "- Responsive design with CSS variables and semantic HTML5\n"
                    "- No emoji as UI icons — use inline SVG\n"
                    "- Balanced braces, null-guarded DOM queries, try/catch async\n\n"
                    "## Browser Virtuosity (v5.8.5) — how to drive the built-in browser\n"
                    "When the user asks you to browse, test, play a web game, fill a form, log in, "
                    "scrape, click buttons, or verify a UI, use the `browser` tool. You are fluent with it.\n"
                    "### Core loop\n"
                    "1. `navigate { url }` then `observe` — the observe result includes a snapshot with "
                    "   interactive items (each has an `index` / `ref`, role, text, bbox) AND an "
                    "   auto-annotated screenshot where every item is boxed with its index number. Use the "
                    "   annotated screenshot + the snapshot text together to ground every click.\n"
                    "2. To click a DOM element, prefer `act { subaction: 'click', ref: 'ref-N' }` or "
                    "   `click { ref: 'ref-N' }`. Never guess CSS selectors when a ref is available.\n"
                    "3. To click a non-DOM thing (canvas games, WebGL viewports, custom UIs) use "
                    "   `mouse_click { x, y }` — pixel-perfect. Pass `canvas: 'canvas'` to treat x/y as "
                    "   canvas-local coords, or use `canvas_click` for the same thing in one step.\n"
                    "### Playing games\n"
                    "- `key_hold { key: 'w', duration_ms: 1200 }` holds WASD to walk. Combine with "
                    "  `mouse_move` to aim then `mouse_click` to shoot.\n"
                    "- `key_down { key }` + `key_up { key }` lets you hold-and-release precisely "
                    "  (e.g. down 'ShiftLeft', move 'w', up 'ShiftLeft' for run-then-walk).\n"
                    "- Use `macro { steps: [...] }` to chain 3-20 actions in one turn (click play → "
                    "  wait 800ms → key_hold w 1500ms → mouse_click aim point → key Space). The macro "
                    "  saves LLM round-trips; each step is a dict with its own `action` field, plus "
                    "  `{action:'wait', ms: N}` for pauses.\n"
                    "### Forms, files, dialogs\n"
                    "- `fill { ref, value }` / `type_text { text }` for inputs. `select { ref }` with "
                    "  no value lists options; then call again with `value` or `label` to choose.\n"
                    "- `upload { ref, files: [path] }` for file inputs.\n"
                    "- `close_popups` dismisses cookie banners / modals / consent dialogs.\n"
                    "### Multi-tab flows\n"
                    "- `new_tab { url }` opens a tab and focuses it. `switch_tab` (no index) lists "
                    "  all tabs; pass `index` to switch. `close_tab` closes current or `index`.\n"
                    "### Finding things\n"
                    "- `find { query }` grep-searches the whole page INCLUDING off-viewport text. "
                    "  Each hit reports `scrollY_needed` so you can scroll straight to it.\n"
                    "- `evaluate { script }` runs arbitrary JS in page context for anything else.\n"
                    "### Visible AI cursor\n"
                    "Every mouse / key action paints a blue glowing cursor + ripple on the page so "
                    "the user sees you driving the browser. This is expected — do NOT try to suppress it.\n"
                    "### Efficiency\n"
                    "Budget: navigate + observe + ≤6 interactions is usually enough. If you're over 10 "
                    "calls on the same goal, step back and read the annotated screenshot more carefully "
                    "instead of guessing more selectors.\n\n"
                    "## Project Memory (v5.8.6)\n"
                    "The `Recent Pipeline Results` / `Current Canvas Workflow` / `Project Memory Digest` "
                    "blocks below ARE the user's project context — treat them as ground truth when "
                    "they ask \"why did the last run fail?\", \"what's on the canvas right now?\", "
                    "or \"build on top of run 2\". Cite the specific subtask / reviewer note when relevant.\n\n"
                    "## Workflow Composer (v5.8.6)\n"
                    "When the user asks you to DESIGN or REARRANGE the node pipeline (e.g. \"plan "
                    "a 3-stage workflow for a game site\", \"add a polisher after merger\", \"skip "
                    "tester this run\"), emit a fenced JSON block tagged `evermind-workflow` with this shape:\n"
                    "```evermind-workflow\n"
                    "{\n"
                    "  \"nodes\": [\n"
                    "    {\"agent\": \"planner\", \"depends_on\": []},\n"
                    "    {\"agent\": \"analyst\", \"depends_on\": [\"planner\"]},\n"
                    "    {\"agent\": \"builder\", \"depends_on\": [\"analyst\"]},\n"
                    "    {\"agent\": \"reviewer\", \"depends_on\": [\"builder\"]},\n"
                    "    {\"agent\": \"deployer\", \"depends_on\": [\"reviewer\"]}\n"
                    "  ]\n"
                    "}\n"
                    "```\n"
                    "Agent keys must be one of: planner, analyst, imagegen, spritesheet, assetimport, "
                    "uidesign, scribe, builder, polisher, merger, reviewer, tester, debugger, deployer. "
                    "The frontend detects the fenced block and renders it on the canvas — the user can "
                    "then click Run to execute it as a live pipeline. For simple Q&A that doesn't "
                    "restructure the workflow, do NOT emit this block.\n\n"
                    "## Tool — file_ops (v5.8.6 chat MVP)\n"
                    "You CAN read/write/edit/list/search/delete files by calling the `file_ops` tool.\n"
                    "Call it directly via OpenAI-standard tool_calls — do NOT use markdown fenced ```browser\n"
                    "or ```file blocks (those are just text, they don't execute). When the user asks you\n"
                    "to read `~/Desktop/something` or create a file, you MUST invoke `file_ops` with the\n"
                    "appropriate action. After reading, summarize the contents; after writing, confirm path.\n"
                    "Path tips: use absolute paths when possible (`/Users/xxx/Desktop/file.html`). If the\n"
                    "user gives a file name only (`测试.html`), try the workspace first, then\n"
                    "`~/Desktop/<name>` as a common default, list the parent dir if unsure.\n"
                    "Actions: `read` (returns content), `write` (path+content), `edit` (old_string→new_string),\n"
                    "`list` (directory listing), `search` (pattern + glob), `delete` (path).\n"
                    "\n"
                    "## Built-in Browser (v6.0)\n"
                    "You CAN drive the embedded Chromium via the `browser` tool. Always `observe()`\n"
                    "first to see interactive elements (each gets a [N] index), then `click(ref=N)`\n"
                    "or `click(selector=...)`. For forms use `fill(selector, text)` + `press(key='Enter')`.\n"
                    "`scroll(direction, amount)` reveals off-screen content. Use `screenshot()` to\n"
                    "confirm visual state when asked. Your actions are overlaid on the browser with\n"
                    "an AI cursor + click ripple so the user sees what you're doing.\n\n"
                    "## Language\n"
                    "Respond in the same language the user writes in.\n"
                    "If the user writes in Chinese, respond in Chinese.\n"
                    "If the user writes in English, respond in English."
                    + pipeline_ctx
                    # v6.4.29/30: full Active Project block for non-OpenAI
                    # providers (Kimi / Anthropic / Gemini handle large
                    # system prompts fine; 403 risk is only relay).
                    + _build_active_project_context(compact=False, session_id=session_id)
                    # v6.4.31/36: auto pre-read trigger-word-gated.
                    # Kimi k2.6 degenerates into prose tool_calls once
                    # system prompt > ~10KB, so keep the snippet tiny for kimi.
                    + _chat_auto_pre_read_snapshot(
                        chat_text, session_id,
                        kimi_compact=("kimi" in str(chat_model or "").lower() or "moonshot" in str(chat_model or "").lower()),
                    )
                    )

                # v6.4.56 ROI-1 (maintainer) — STICKY PROJECT INVARIANTS.
                # Append auto-extracted invariants (3D/第三人称/怪物 etc.) to
                # the very last user message. Inspired by Cline's
                # environment_details pattern + Anthropic long-context tips
                # ("queries at the end improve quality up to 30%"). We do NOT
                # put this in system_prompt because:
                #   (1) system gets cached & distant after many turns;
                #   (2) Aider format_chat_chunks proves "user-tail reminder"
                #       beats "system-head reminder" for near-term attention.
                # The extract is cheap regex over the FIRST user message in
                # session memory, so it's immune to compaction and guaranteed
                # to re-anchor 3D-third-person-has-monsters on every turn.
                def _v656_extract_invariants(_sess_id: str, _latest_user: str) -> Dict[str, Any]:
                    import re as _re_inv
                    _first_user = _latest_user
                    try:
                        _sess_mem = _SESSION_MEMORY.get(_sess_id) or {} if _sess_id else {}
                        _past_msgs = _sess_mem.get("chat_messages") or []
                        for _pm in _past_msgs:
                            if isinstance(_pm, dict) and _pm.get("role") == "user":
                                _c_pm = str(_pm.get("content") or "")
                                if _c_pm.strip():
                                    _first_user = _c_pm
                                    break
                    except Exception:
                        pass
                    _patterns = {
                        "3D 三维": r"3\s*d|三维",
                        "2D 二维": r"2\s*d(?![a-z])|二维",
                        "第一人称/FPS": r"第一人称|fps|first[- ]?person",
                        "第三人称/TPS": r"第三人称|tps|third[- ]?person",
                        "有怪物/敌人": r"怪物|敌人|monster|enemy|zombie|boss",
                        "多人联机": r"多人|联机|multiplayer|co-?op",
                        "移动端": r"手机|移动端|mobile",
                        "子弹轨迹": r"子弹(?:轨迹|弹道)|bullet\s*trail|tracer",
                        "准星": r"准星|crosshair|reticle",
                        "武器建模": r"武器(?:建模|模型)|枪(?:械|支)建模|weapon\s*model",
                        "关卡/地图": r"关卡|地图|level|map|stage",
                        "射击/shooting": r"射击|shoot|射击游戏|shooter",
                        "PixiJS/Three.js": r"three\.?js|pixi\.?js|babylon\.?js",
                    }
                    _hits = [_label for _label, _pat in _patterns.items()
                             if _re_inv.search(_pat, _first_user, _re_inv.I)]
                    return {
                        "original_goal": _first_user[:400],
                        "must_preserve": _hits,
                    }

                _v656_inv = _v656_extract_invariants(session_id, chat_text)
                _v656_block = (
                    "\n\n<project_invariants>\n"
                    f"original_goal: {_v656_inv['original_goal']}\n"
                    f"must_preserve: {', '.join(_v656_inv['must_preserve']) or '(none auto-detected — honor user original text above)'}\n"
                    "RULE: every file_ops.edit/write MUST honor must_preserve. "
                    "If a change would violate (e.g. user asked 3D third-person, "
                    "do NOT refactor to first-person), ASK first. Cite which "
                    "invariant each edit preserves in your response.\n"
                    "</project_invariants>"
                )
                # v6.5 Phase 3: Plan/Act/Debug mode suffix + AGENTS.md/memories.
                try:
                    _mode_cfg = CHAT_MODES.get(chat_mode) or CHAT_MODES["act"]
                    system_prompt = str(system_prompt) + str(_mode_cfg.get("sys_suffix") or "")
                    _agents_block = _load_agents_md_and_memories(OUTPUT_DIR)
                    if _agents_block:
                        system_prompt = system_prompt + "\n\n" + _agents_block
                    logger.info(
                        "chat_worker: mode=%s agents_md_block=%d chars",
                        chat_mode, len(_agents_block),
                    )
                except Exception as _mode_err:
                    logger.warning("chat_worker: mode/AGENTS.md injection failed: %s", _mode_err)

                llm_messages = [{"role": "system", "content": system_prompt}]
                # Add conversation history
                if isinstance(chat_history, list):
                    for h in chat_history[-40:]:
                        if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
                            llm_messages.append({"role": h["role"], "content": str(h.get("content", ""))})
                # v6.4.56: append invariants block to the CURRENT user message
                llm_messages.append({"role": "user", "content": chat_text + _v656_block})
                try:
                    logger.info(
                        "chat_worker: invariants must_preserve=%s goal_len=%d",
                        ",".join(_v656_inv["must_preserve"]) or "(none)",
                        len(_v656_inv["original_goal"]),
                    )
                except Exception:
                    pass

                # Stream response in background thread
                import threading
                import queue as queue_mod

                token_queue: queue_mod.Queue = queue_mod.Queue()
                # v6.4.43 (maintainer): HOIST _cancel_requested so
                # the nested _chat_worker can see it via closure. Before
                # this, the flag was created AFTER worker.start(), so
                # chat_stop could flip it but the worker's tight stream
                # loop had no way to notice — Stop button didn't stop
                # during LLM streaming, which is the slowest case and the
                # only one the user desperately needs to abort.
                _cancel_requested = {"value": False}
                _chat_cancel_handles[client_id] = _cancel_requested
                _chat_queue_handles[client_id] = token_queue

                def _chat_worker():
                    # v6.4.43 fix: declare nonlocal so the v6.4.43-B history
                    # compaction (`llm_messages = _v643_compact_history(...)`)
                    # doesn't create a new local and shadow the outer list
                    # — that crash was UnboundLocalError on the first read
                    # of llm_messages inside _chat_worker.
                    nonlocal llm_messages
                    try:
                        from openai import OpenAI
                        # v6.1.1: same <think> stripper as builder/compat path —
                        # chat agent must not leak reasoning tokens into user-
                        # visible output either.
                        from ai_bridge import ThinkStripper, strip_think_tags_full
                        _chat_think_strip = str(os.getenv("EVERMIND_STRIP_THINK", "1")).strip().lower() not in ("0", "false", "off", "no")
                        _chat_think_stripper = ThinkStripper() if _chat_think_strip else None

                        # ── Model-aware routing via MODEL_REGISTRY ──
                        _model_info = MODEL_REGISTRY.get(chat_model, {})
                        _provider = str(_model_info.get("provider") or "openai").lower()

                        # Resolve API key for this provider
                        _env_key = PROVIDER_ENV_KEY_MAP.get(_provider, "OPENAI_API_KEY")
                        api_key = os.getenv(_env_key, "")

                        # v6.3 (maintainer) HOTFIX: resolve base URL with
                        # env PRIORITY over MODEL_REGISTRY.api_base — previously
                        # it was the opposite, so a user setting a new
                        # OPENAI_API_BASE via UI got silently overridden by the
                        # import-time hardcoded relay.com in the registry,
                        # producing "Invalid token" 401s when the key was for a
                        # different relay (e.g. relay). This now matches
                        # ai_bridge._execute_openai_compatible_chat (line ~16546)
                        # so UI-saved base URLs work immediately across every
                        # LLM code path.
                        _base_env_map = {
                            "openai": "OPENAI_API_BASE", "anthropic": "ANTHROPIC_API_BASE",
                            "google": "GEMINI_API_BASE", "deepseek": "DEEPSEEK_API_BASE",
                            "kimi": "KIMI_API_BASE", "qwen": "QWEN_API_BASE",
                            "zhipu": "ZHIPU_API_BASE", "doubao": "DOUBAO_API_BASE",
                            "yi": "YI_API_BASE", "minimax": "MINIMAX_API_BASE",
                        }
                        _env_base = os.getenv(_base_env_map.get(_provider, ""), "").strip()
                        base_url = _env_base or str(_model_info.get("api_base") or "").strip()
                        # v6.3 (maintainer): diagnostic log so a failed
                        # chat turn can always be traced to the exact base_url
                        # that was used. Previous chat errors were swallowed
                        # inside _chat_worker's bare except, leaving no trail.
                        logger.info(
                            "chat_worker: model=%s provider=%s base_url=%s key_present=%s",
                            chat_model, _provider, base_url or "(none)",
                            bool(api_key),
                        )

                        # Resolve actual model ID (strip litellm prefix like "openai/")
                        _litellm_id = str(_model_info.get("litellm_id") or chat_model)
                        api_model_id = _litellm_id.split("/", 1)[1] if "/" in _litellm_id else _litellm_id

                        # ── Anthropic native path (no OpenAI-compatible gateway) ──
                        if _provider == "anthropic" and not base_url:
                            try:
                                from anthropic import Anthropic
                                _anth_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
                                anth_client = Anthropic(api_key=_anth_key)
                                _sys = llm_messages[0]["content"] if llm_messages and llm_messages[0].get("role") == "system" else ""
                                _msgs = [m for m in llm_messages if m.get("role") != "system"]
                                with anth_client.messages.stream(
                                    model=api_model_id, messages=_msgs, system=_sys,
                                    max_tokens=4096, temperature=0.7,
                                ) as stream:
                                    full_content = ""
                                    for text in stream.text_stream:
                                        full_content += text
                                        token_queue.put(("token", text))
                                token_queue.put(("done", full_content))
                                return
                            except Exception as e:
                                token_queue.put(("error", str(e)))
                                return

                        # ── OpenAI-compatible path (covers openai/kimi/deepseek/qwen/etc) ──
                        # v6.4.47 (maintainer): force httpx-level
                        # timeouts by constructing our OWN httpx.Client.
                        # The openai SDK's per-call `timeout=` parameter and
                        # our external watchdog both failed to break out of
                        # a recv() blocked on relay (observed at iter=6
                        # and iter=7, 5+ minutes hung). By handing the SDK
                        # a pre-configured httpx.Client with explicit
                        # connect/read timeouts, the socket will raise
                        # httpx.ReadTimeout on any single read that stays
                        # silent longer than `read`. This is enforced at
                        # the BSD socket level, so no watchdog, no thread
                        # scheduling hazards, no SDK-version quirks.
                        import httpx as _httpx_v647
                        _chat_http_client = _httpx_v647.Client(
                            timeout=_httpx_v647.Timeout(
                                connect=10.0,
                                read=45.0,    # max silence per chunk
                                write=10.0,
                                pool=None,
                            ),
                            limits=_httpx_v647.Limits(
                                max_connections=20,
                                max_keepalive_connections=10,
                            ),
                        )
                        client_kwargs: Dict[str, Any] = {"http_client": _chat_http_client}
                        if api_key:
                            client_kwargs["api_key"] = api_key
                        if base_url:
                            client_kwargs["base_url"] = base_url
                        _extra_headers = _model_info.get("extra_headers")
                        if _extra_headers:
                            client_kwargs["default_headers"] = _extra_headers
                        # v6.3 (maintainer) HOTFIX: some Chinese relays
                        # (relay, likely others) do anti-passthrough
                        # fingerprinting on the exact string "User-Agent:
                        # OpenAI/Python" and return 403 "Your request was
                        # blocked." when they see it. Override the UA to a
                        # neutral value so relay-hosted models stay reachable.
                        # The official Python SDK exposes this via
                        # `default_headers`. When the user has NOT supplied
                        # custom extra_headers, stamp our own UA. When they
                        # did supply headers (rare), honour theirs but still
                        # ensure User-Agent is safe.
                        _safe_ua = "evermind-chat/6.3 httpx/openai-compat"
                        if "default_headers" in client_kwargs:
                            _existing = dict(client_kwargs["default_headers"] or {})
                            _existing.setdefault("User-Agent", _safe_ua)
                            client_kwargs["default_headers"] = _existing
                        else:
                            client_kwargs["default_headers"] = {"User-Agent": _safe_ua}
                        client = OpenAI(**client_kwargs)

                        # v5.8.6: chat agent tool calling — file_ops.
                        # v6.0: add `browser` tool so the chat agent can drive
                        # the embedded Chromium. Wired to the shared
                        # BrowserPlugin via run_coroutine_threadsafe so state
                        # persists across navigate/click/screenshot within a
                        # single chat turn AND across turns.
                        _chat_tools = [{
                            "type": "function",
                            "function": {
                                "name": "file_ops",
                                "description": "Read, write, list, edit, search, or delete files on the user's local workspace. Use this whenever the user asks you to read/create/modify a file. Paths may be absolute (e.g. /Users/<you>/Desktop/test.html) or relative to the workspace root.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "action": {"type": "string", "enum": ["read", "write", "edit", "search", "list", "delete"]},
                                        "path": {"type": "string"},
                                        "content": {"type": "string", "description": "For write action"},
                                        "old_string": {"type": "string", "description": "For edit action"},
                                        "new_string": {"type": "string", "description": "For edit action"},
                                        "replace_all": {"type": "boolean"},
                                        "pattern": {"type": "string", "description": "For search action"},
                                        "glob": {"type": "string"},
                                    },
                                    "required": ["action", "path"],
                                },
                            },
                        }, {
                            "type": "function",
                            "function": {
                                "name": "browser",
                                "description": (
                                    "Drive the embedded Chromium browser. Actions:\n"
                                    " - navigate(url): open a page\n"
                                    " - observe(): return the accessibility tree + interactive elements\n"
                                    " - click(selector=..., ref=N): click by CSS selector or by [N] from observe\n"
                                    " - fill(selector, text): type into an input\n"
                                    " - scroll(direction='down'|'up', amount=px): scroll the page\n"
                                    " - screenshot(): return a screenshot\n"
                                    " - extract(selector): return text/HTML of matched element\n"
                                    " - wait_for(selector, timeout_ms=5000): wait for element\n"
                                    "Use browser for any URL / search / web-form task. Always observe() before clicking to know what's on the page. AI cursor / click ripple shown to user automatically."
                                ),
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "action": {
                                            "type": "string",
                                            "enum": [
                                                "navigate", "observe", "click", "fill", "scroll",
                                                "screenshot", "extract", "wait_for", "press",
                                                "hover", "new_tab", "switch_tab", "close_tab",
                                            ],
                                        },
                                        "url": {"type": "string"},
                                        "selector": {"type": "string"},
                                        "ref": {"type": "integer", "description": "[N] index from prior observe()"},
                                        "text": {"type": "string", "description": "For fill / press"},
                                        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                                        "amount": {"type": "integer", "description": "Scroll amount in px"},
                                        "timeout_ms": {"type": "integer"},
                                        "key": {"type": "string", "description": "For press (e.g. 'Enter', 'Tab')"},
                                        "tab_index": {"type": "integer"},
                                    },
                                    "required": ["action"],
                                },
                            },
                        }]
                        # v6.5 Phase 3: mode-scoped tool whitelist.
                        try:
                            _pre_count = len(_chat_tools)
                            _chat_tools = _filter_chat_tools_by_mode(_chat_tools, chat_mode)
                            if len(_chat_tools) != _pre_count:
                                logger.info(
                                    "chat_worker: mode=%s tool-filter %d → %d",
                                    chat_mode, _pre_count, len(_chat_tools),
                                )
                        except Exception as _filt_err:
                            logger.warning("chat_worker: tool filter failed: %s", _filt_err)
                        # v6.1.2: raised from 6 to 15. A typical GitHub search
                        # flow (navigate → observe → scroll → observe → try-click
                        # → retry with selector → type query → submit → observe
                        # results → click first result → observe) needs 10+ calls.
                        # At 6 the chat agent consistently hit the ceiling mid-task
                        # and bailed silently, which users experienced as "it just
                        # stopped doing anything". Also exposed as env var so power
                        # users can crank higher for long browse sessions.
                        # v6.1.3 (maintainer): Chat agent ceiling raised
                        # 15→80. Google-search-retry-with-Bing style flows need
                        # 15+ rounds easily; users were hitting the cap and
                        # seeing "Google search blocked, let me try Bing" cut
                        # off mid-recovery. 80 gives real headroom; no-activity
                        # watchdog still catches genuine deadlocks.
                        try:
                            _MAX_TOOL_ITERATIONS = int(
                                str(os.getenv("EVERMIND_CHAT_TOOL_ITERATIONS", "80") or "80").strip()
                            )
                        except Exception:
                            _MAX_TOOL_ITERATIONS = 80
                        _MAX_TOOL_ITERATIONS = max(3, min(200, _MAX_TOOL_ITERATIONS))
                        _iter = 0
                        full_content = ""
                        # v6.4.35 (maintainer) — DISABLE thinking for chat
                        # on kimi. Kimi k2.6-code-preview defaults to
                        # thinking=enabled which requires every assistant
                        # tool_call message in history to carry
                        # `reasoning_content`. Our history from localStorage
                        # doesn't, so kimi returns:
                        #   400 "thinking is enabled but reasoning_content is
                        #        missing in assistant tool call message at index N"
                        # Chat is interactive — user wants fast responses, not
                        # 30s of hidden thinking. Force thinking off.
                        _lower_model = str(api_model_id or "").lower()
                        _is_kimi = "kimi" in _lower_model or "moonshot" in _lower_model
                        _is_claude = "claude" in _lower_model or "anthropic" in _lower_model
                        _is_gpt = "gpt" in _lower_model or "o1" in _lower_model or "o3" in _lower_model or "o4" in _lower_model
                        _is_gemini = "gemini" in _lower_model or "google" in _lower_model
                        _is_qwen = "qwen" in _lower_model or "tongyi" in _lower_model or "dashscope" in _lower_model
                        _is_glm = "glm" in _lower_model or "zhipu" in _lower_model or "chatglm" in _lower_model
                        _is_deepseek = "deepseek" in _lower_model
                        _is_doubao = "doubao" in _lower_model or "volcengine" in _lower_model
                        _extra_body: Dict[str, Any] = {}
                        if _is_kimi:
                            _extra_body["thinking"] = {"type": "disabled"}
                        # Also strip any stale reasoning_content from history
                        # messages (older turns may have it; kimi-disabled
                        # mode doesn't care, and dropping it avoids any
                        # accidental re-enable of the strict validator).
                        # v6.4.38 (maintainer): message sanitizer. The
                        # OpenAI SDK 1.x does deep equality/length checks on
                        # message fields and will crash with opaque errors
                        # like "'<' not supported between instances of 'map'
                        # and 'int'" if ANY field is a Python `map`/`filter`
                        # /generator object. Force every message field to a
                        # concrete JSON-serializable type before sending.
                        def _sanitize_chat_messages(msgs: list) -> list:
                            import types as _types
                            out = []
                            for m in (msgs or []):
                                if not isinstance(m, dict):
                                    continue
                                clean: Dict[str, Any] = {}
                                for k, v in m.items():
                                    # Unfold map/filter/generator → list
                                    if isinstance(v, (map, filter, _types.GeneratorType)):
                                        try:
                                            v = list(v)
                                        except Exception:
                                            v = []
                                    # Tool message content MUST be str
                                    if k == "content" and v is not None and not isinstance(v, (str, list)):
                                        try:
                                            v = str(v)
                                        except Exception:
                                            v = ""
                                    # tool_calls items must be dicts
                                    if k == "tool_calls" and isinstance(v, list):
                                        _tcs = []
                                        for tc in v:
                                            if isinstance(tc, (map, filter, _types.GeneratorType)):
                                                try:
                                                    tc = list(tc)
                                                except Exception:
                                                    continue
                                            _tcs.append(tc)
                                        v = _tcs
                                    clean[k] = v
                                # v6.4.41 — KIMI ONLY: if thinking is DISABLED
                                # we strip stale reasoning_content. But if
                                # thinking stays on (future default), kimi
                                # requires every assistant tool_call message
                                # to have a non-empty reasoning_content
                                # field. Provide an empty one so replay of
                                # old history without saved CoT doesn't 400.
                                if (_is_kimi and clean.get("role") == "assistant"
                                        and clean.get("tool_calls")
                                        and not clean.get("reasoning_content")):
                                    clean["reasoning_content"] = ""
                                # Other providers silently ignore the field.
                                out.append(clean)
                            return out
                        # v6.4.44-A (maintainer): ENTRY POISON FILTER.
                        # Kimi/GPT sometimes output "让我尝试读取..." /
                        # "让我继续读取..." / "代码被截断了..." as PROSE —
                        # no real tool_calls fired. That prose lands in
                        # chat_messages; next turn the model sees its own
                        # hallucination and mimics it, degenerating into a
                        # read-loop forever. Before we even start the while
                        # loop, scrub the tail of llm_messages of these
                        # poison assistant messages (those WITHOUT
                        # tool_calls AND containing ≥4 hallucination
                        # markers). Then inject a one-shot system nudge
                        # telling the model: use REAL tool_calls, do NOT
                        # describe the action in prose.
                        _HALLUCINATION_MARKERS = [
                            "让我尝试读取", "让我继续读取", "让我检查",
                            "让我用", "让我查看", "让我搜索", "让我滚动",
                            "让我观察", "让我截图", "让我尝试用",
                            "被截断", "代码被截断", "读取被截断",
                            "仍然被截断", "文件读取被截断",
                            "由于文件读取被截断",
                        ]
                        def _v644_is_poison(_m: Dict[str, Any]) -> bool:
                            if not isinstance(_m, dict) or _m.get("role") != "assistant":
                                return False
                            if _m.get("tool_calls"):
                                return False
                            _c = _m.get("content") or ""
                            if not isinstance(_c, str) or len(_c) < 200:
                                return False
                            _hits = sum(_c.count(_p) for _p in _HALLUCINATION_MARKERS)
                            return _hits >= 4
                        _v644_cleaned = [_m for _m in llm_messages if not _v644_is_poison(_m)]
                        if len(_v644_cleaned) != len(llm_messages):
                            _removed = len(llm_messages) - len(_v644_cleaned)
                            logger.warning(
                                "chat_worker: scrubbed %d hallucination message(s) from history",
                                _removed,
                            )
                            # Insert one-shot nudge AFTER system prompt
                            _nudge = {
                                "role": "system",
                                "content": (
                                    "[NUDGE] 之前的对话里模型多次用自然语言描述"
                                    "\"让我读取...\"\"被截断\" 但没有实际调用工具。"
                                    "本轮必须使用 file_ops / browser 等结构化 tool_calls "
                                    "才能真正读写文件/访问网页。禁止只用文字描述动作。"
                                ),
                            }
                            if len(_v644_cleaned) >= 1 and _v644_cleaned[0].get("role") == "system":
                                llm_messages = [_v644_cleaned[0], _nudge] + _v644_cleaned[1:]
                            else:
                                llm_messages = [_nudge] + _v644_cleaned
                        else:
                            llm_messages = _v644_cleaned
                        # v6.4.44-C: KIMI first-turn tool_choice coercion.
                        # If the user's latest message looks like an execution
                        # task ("优化"/"读"/"看"/"修复"/"改") and the model
                        # is kimi, force tool_choice="required" on iter=1 so
                        # kimi emits a real tool_call instead of prose. Some
                        # kimi endpoints don't support required — we fall
                        # back gracefully.
                        _v644_force_required_first = False
                        if _is_kimi:
                            _last_user = ""
                            for _m in reversed(llm_messages):
                                if isinstance(_m, dict) and _m.get("role") == "user":
                                    _last_user = str(_m.get("content") or "")
                                    break
                            _exec_kw = ("优化", "读", "看", "修", "改", "删",
                                        "写", "创建", "执行", "调试", "部署",
                                        "访问", "打开", "查找", "搜索")
                            if _last_user and any(_k in _last_user for _k in _exec_kw):
                                _v644_force_required_first = True
                                logger.info("chat_worker: kimi first-turn tool_choice=required")
                        # v6.4.41 (maintainer) — PROVIDER-AGNOSTIC
                        # chat hardening. Previous fixes targeted specific
                        # failure modes (kimi thinking, map/int leaks, search
                        # rename). This layer catches the GENERIC pathologies
                        # that every tool-calling LLM exhibits under load:
                        #   1. Degenerate repetition ("好的我现在开始深度优化"
                        #      N 次) — detect ≥3 consecutive identical 40-char
                        #      suffixes in streamed content, abort turn.
                        #   2. Empty-response deadlock — when finish_reason is
                        #      "stop" with no content AND no tool_calls, break
                        #      instead of spinning forever.
                        #   3. Tool-call signature loop — if the same
                        #      (tool_name, args-hash) appears ≥3 consecutive
                        #      iterations, refuse and ask user to rephrase.
                        #   4. Prose tool-call fallback — kimi/qwen sometimes
                        #      emit `to=file_ops.read path` as plain text when
                        #      the function-call API is confused; extract and
                        #      synthesize a real tool_call.
                        #   5. Adaptive tool_choice — first turn "auto", but
                        #      after 40 iterations flip to "none" so the model
                        #      is forced to produce a final answer instead of
                        #      calling more tools.
                        #   6. No-progress watchdog — if 3 iterations in a row
                        #      produce neither new content nor new tool_calls,
                        #      abort.
                        # All logic is provider-agnostic; specific quirks are
                        # handled above (_is_kimi, _is_claude, _is_gpt etc.).
                        import hashlib as _hashlib_v641
                        import re as _re_v641
                        _v641_recent_text_hashes: list = []
                        _v641_recent_tool_sigs: list = []
                        _v641_no_progress = 0
                        _v641_last_content_len = 0
                        _PROSE_TOOL_RE = _re_v641.compile(
                            r"(?:to=|tool=|<tool_call>\s*)(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?:\.(?P<action>[a-zA-Z_][a-zA-Z0-9_]*))?\s*(?P<args>\{[^}]*\})?",
                            _re_v641.MULTILINE,
                        )
                        def _v641_detect_repetition(text: str) -> bool:
                            """True if last 600 chars contain a pattern that
                            repeats ≥3 times — either as consecutive N-char
                            windows OR as a phrase (sentence-split) that
                            recurs ≥4 times anywhere in the tail.

                            v6.4.44-B: added phrase-frequency detection —
                            the consecutive-window check missed kimi's
                            "让我尝试读取..." loop where each sentence
                            repeats the idea with slight wording shifts."""
                            if not text or len(text) < 120:
                                return False
                            tail = text[-600:]
                            # Consecutive N-char windows (v6.4.41-1 original)
                            for _w in (40, 60, 80, 120):
                                if len(tail) < _w * 3:
                                    continue
                                _last = tail[-_w:]
                                _count = 0
                                _pos = len(tail)
                                while _pos >= _w and tail[_pos - _w:_pos] == _last:
                                    _count += 1
                                    _pos -= _w
                                    if _count >= 3:
                                        return True
                            # v6.4.44-B: phrase frequency. Split on CJK
                            # punctuation; count phrases ≥10 chars that
                            # appear ≥4 times in the tail.
                            import re as _re_rep
                            _phrases = _re_rep.split(r"[。！？；\n,，:：]+", tail)
                            _phrase_counts: Dict[str, int] = {}
                            for _ph in _phrases:
                                _ph = _ph.strip()
                                if len(_ph) >= 10:
                                    _phrase_counts[_ph] = _phrase_counts.get(_ph, 0) + 1
                            if _phrase_counts and max(_phrase_counts.values()) >= 4:
                                return True
                            # v6.4.44-B: hallucination marker density — if
                            # the tail has ≥5 of "让我"/"被截断" style markers,
                            # the model is prose-looping even without exact
                            # repetition.
                            _hallu = sum(
                                tail.count(_p) for _p in
                                ("让我尝试", "让我继续", "让我检查", "让我用",
                                 "让我查看", "被截断", "让我搜索")
                            )
                            if _hallu >= 5:
                                return True
                            return False
                        def _v641_tool_signature(tcs_acc: dict) -> str:
                            if not tcs_acc:
                                return ""
                            _parts = []
                            for _i in sorted(tcs_acc.keys()):
                                _e = tcs_acc.get(_i) or {}
                                _fn = (_e.get("function") or {})
                                _parts.append(f"{_fn.get('name', '')}:{_fn.get('arguments', '')[:400]}")
                            return _hashlib_v641.md5("|".join(_parts).encode("utf-8", "ignore")).hexdigest()
                        # v6.4.42: total-turn budget watchdog. If the entire
                        # chat turn exceeds this, break the loop with a user
                        # message. Default 900s (15 min) — long enough for
                        # complex browse-then-code-then-verify flows but
                        # short enough that a stuck coroutine doesn't hang
                        # the chat forever.
                        _v642_turn_budget_sec = 900.0
                        try:
                            _v642_turn_budget_sec = float(os.getenv("EVERMIND_CHAT_TURN_BUDGET_SEC", "900") or 900)
                        except Exception:
                            _v642_turn_budget_sec = 900.0
                        _v642_turn_budget_sec = max(60.0, min(_v642_turn_budget_sec, 3600.0))
                        import time as _time_v642
                        _v642_turn_started = _time_v642.monotonic()
                        # v6.4.48-C (maintainer) — behavioural guardrail.
                        # Observed: kimi/gpt-5.4 sometimes spend 12+ iterations
                        # just reading files, never moving to write/edit,
                        # ending with content_len=0. This is a model-level
                        # laziness/anxiety, not a plumbing bug — prompting
                        # them helps. We track consecutive iterations that
                        # fire ONLY read/list/search tool_calls; at 6 in a
                        # row, inject a one-shot system nudge "读够了,现在
                        # 用 file_ops.write / edit 落地修复". Nudge only
                        # fires once per chat turn so we don't spam.
                        _v648_readonly_streak = 0
                        # v6.4.55-A: reset Cline no-tool-retry counter per
                        # chat turn. Previously stored as a module-level
                        # attribute on _sanitize_chat_messages, which leaked
                        # across turns, causing Apr 23 13:33 session to skip
                        # the nudge because the counter was already ≥ 2 from
                        # a prior turn.
                        _v655_no_tool_retry = 0
                        # v6.4.50-B: nudge can fire MULTIPLE times.
                        # Previously _v648_nudge_sent latched True and
                        # the nudge only ever fired once per chat turn.
                        # Observed in Apr 23 11:13 session: nudge at
                        # iter=6 pushed kimi into browser for 3 iters,
                        # browser screenshot/click failed, kimi fell back
                        # to read loop for iter 12-23 — nudge could not
                        # re-fire because the latch was stuck.
                        _v648_nudge_count = 0
                        _V648_READONLY_NUDGE_AFTER = 6
                        _v648_force_no_tools_next = False
                        # v6.4.43-B: history auto-compaction. Chat with
                        # multiple file reads quickly pushes msg count into
                        # the 30+ range and input tokens over 60K, which
                        # causes gpt-5.x on relay to respond in 4+
                        # minutes or time out entirely. When the history
                        # grows too large, fold the middle block (everything
                        # except system prompt + last 12 msgs) into ONE
                        # compact "[history digest]" message — the model
                        # still sees the conversation arc plus the most
                        # recent 6 turns which are what matter for next step.
                        def _v643_compact_history(msgs: list) -> list:
                            if not isinstance(msgs, list) or len(msgs) < 28:
                                return msgs
                            # Estimate size
                            _total = 0
                            for _m in msgs:
                                _c = _m.get("content") if isinstance(_m, dict) else None
                                if isinstance(_c, str):
                                    _total += len(_c)
                            if _total < 50000 and len(msgs) < 32:
                                return msgs  # no need
                            _head = msgs[:1]  # system
                            # v6.4.56 ROI-2 (maintainer): pin FIRST user
                            # msg as the original goal — NEVER compact it
                            # away. Observed in Apr 23 13:xx session: after 3
                            # compactions, user's "3D 第三人称 怪物" goal was
                            # gone, model drifted to first-person without
                            # monsters. Cline's environment_details + Anthropic
                            # "long-context tips" both say: keep original goal
                            # close to the turn; never summarise it away.
                            _first_user_idx = next(
                                (_i for _i, _m in enumerate(msgs)
                                 if isinstance(_m, dict) and _m.get("role") == "user"),
                                None,
                            )
                            if _first_user_idx is not None and _first_user_idx < len(msgs) - 12:
                                _pinned_goal = [msgs[_first_user_idx]]
                                _middle = msgs[_first_user_idx + 1:-12]
                            else:
                                _pinned_goal = []
                                _middle = msgs[1:-12]
                            _tail = msgs[-12:]  # last ~6 user/assistant pairs
                            if not _middle:
                                return msgs
                            # Build digest
                            _tool_count = 0
                            _files_seen: Dict[str, int] = {}
                            _urls_seen: Dict[str, int] = {}
                            _assistant_snippets: list = []
                            for _m in _middle:
                                if not isinstance(_m, dict):
                                    continue
                                _role = _m.get("role")
                                if _role == "tool":
                                    _tool_count += 1
                                    _c = str(_m.get("content") or "")[:200]
                                    # Extract path / url from tool result JSON
                                    try:
                                        import json as _jj
                                        _d = _jj.loads(_m.get("content") or "{}")
                                        if isinstance(_d, dict):
                                            _p = _d.get("path") or ""
                                            _u = _d.get("url") or ""
                                            if _p:
                                                _files_seen[_p] = _files_seen.get(_p, 0) + 1
                                            if _u:
                                                _urls_seen[_u] = _urls_seen.get(_u, 0) + 1
                                    except Exception:
                                        pass
                                elif _role == "assistant":
                                    _c = _m.get("content") or ""
                                    if isinstance(_c, str) and _c.strip():
                                        _assistant_snippets.append(_c.strip()[:140])
                            _digest_parts = [
                                # v6.4.56: make it clear the digest is a
                                # trace, NOT a replacement for the user's
                                # pinned original goal (which sits immediately
                                # above this digest in the message list).
                                f"[压缩摘要 — 工具调用痕迹,不得覆盖用户原始目标 · {len(_middle)} 条消息 / {_tool_count} 次工具调用]"
                            ]
                            if _files_seen:
                                _top_files = sorted(_files_seen.items(), key=lambda x: -x[1])[:6]
                                _digest_parts.append(
                                    "已访问文件: " + ", ".join(f"{Path(p).name}×{n}" for p, n in _top_files)
                                )
                            if _urls_seen:
                                _top_urls = sorted(_urls_seen.items(), key=lambda x: -x[1])[:4]
                                _digest_parts.append(
                                    "已访问URL: " + ", ".join(f"{u[:60]}×{n}" for u, n in _top_urls)
                                )
                            if _assistant_snippets:
                                _digest_parts.append(
                                    "之前的阶段性回复片段: " + " | ".join(_assistant_snippets[-4:])
                                )
                            _digest = "\n".join(_digest_parts)
                            _result_msgs = _head + _pinned_goal + [{"role": "user", "content": _digest}] + _tail
                            logger.info(
                                "chat_worker: history compacted %d→%d msgs (digest %d chars, goal pinned=%s)",
                                len(msgs), len(_result_msgs), len(_digest),
                                bool(_pinned_goal),
                            )
                            return _result_msgs
                        while _iter < _MAX_TOOL_ITERATIONS:
                            _iter += 1
                            # v6.4.43-B compact history every iteration (fast no-op when small)
                            llm_messages = _v643_compact_history(llm_messages)
                            # v6.4.42 turn budget check
                            _v642_elapsed = _time_v642.monotonic() - _v642_turn_started
                            if _v642_elapsed > _v642_turn_budget_sec:
                                logger.warning(
                                    "chat_worker: turn budget %.0fs exceeded at iter=%d — aborting",
                                    _v642_turn_budget_sec, _iter,
                                )
                                token_queue.put(("token",
                                    f"\n\n[本轮已运行 {_v642_elapsed:.0f}s,超过 {_v642_turn_budget_sec:.0f}s 上限,已终止。可再次发送消息继续。]"))
                                break
                            logger.info(
                                "chat_worker: iter=%d/%d msgs=%d elapsed=%.1fs",
                                _iter, _MAX_TOOL_ITERATIONS, len(llm_messages), _v642_elapsed,
                            )
                            # v6.4.41-5: Adaptive tool_choice. After 40 iters
                            # the agent is very likely stuck in a tool-loop;
                            # force it to write a final answer.
                            _adaptive_tool_choice = "auto"
                            if _iter > 40:
                                _adaptive_tool_choice = "none"
                            # v6.4.44-C: kimi first-turn force required
                            if _iter == 1 and _v644_force_required_first:
                                _adaptive_tool_choice = "required"
                            # v6.4.50-B: after 3+ readonly-nudges, force no tools
                            if _v648_force_no_tools_next:
                                _adaptive_tool_choice = "none"
                                _v648_force_no_tools_next = False
                                logger.info("chat_worker: forcing tool_choice=none this iter to break read-loop")
                            # v6.4.39 (maintainer): don't shadow the
                            # outer-scope `llm_messages` — use a separate
                            # name. Assigning to `llm_messages` inside this
                            # nested `_chat_worker` turns it into a local,
                            # which made line ~8063 (reading llm_messages[0])
                            # raise UnboundLocalError before reaching the
                            # sanitizer. Now we only sanitize for the SDK
                            # call; the mutable llm_messages list is
                            # appended to directly (line ~8371 still works).
                            _sanitized_messages = _sanitize_chat_messages(llm_messages)
                            # v6.4.51 (maintainer) — max_tokens ROOT FIX.
                            # Previously 4096 → any file_ops.write of a
                            # full HTML file (our test case is ~37KB ≈
                            # 10000+ tokens) got truncated. The tool_call
                            # arguments came back as malformed JSON
                            # (unterminated string), json.loads failed, we
                            # returned {"success": False, "error": ...},
                            # kimi saw the error and fell back to read.
                            # This is the REAL cause of the "read loop
                            # never writes" we've been chasing for hours.
                            # kimi k2.6 supports 32k output; gpt-5.x 16k+;
                            # we pick 16384 as the safe default that also
                            # works on smaller relays. Override via env.
                            # v6.4.53-C: raised from 16384 → 32768. The Apr 23
                            # 12:46 session observed kimi attempting a 58KB
                            # HTML write that got truncated at 16384 tokens.
                            # k2.6 / gpt-5.4 / claude sonnet 4.6 all support
                            # 32k+ output. 58KB HTML ≈ 15-18k tokens so 32768
                            # is comfortable for one-shot full rewrites.
                            _chat_max_tokens = 32768
                            try:
                                _chat_max_tokens = int(os.getenv("EVERMIND_CHAT_MAX_TOKENS", "32768"))
                            except Exception:
                                _chat_max_tokens = 32768
                            _chat_max_tokens = max(2048, min(_chat_max_tokens, 131072))
                            _create_kwargs: Dict[str, Any] = {
                                "model": api_model_id,
                                "messages": _sanitized_messages,
                                "stream": True,
                                "temperature": 0.7,
                                "max_tokens": _chat_max_tokens,
                                "tools": _chat_tools,
                                # v6.4.36: explicit tool_choice=auto so kimi
                                # actually uses the function-call API instead
                                # of hallucinating prose-style tool syntax
                                # ("to=file_ops.read 体育彩票天天json").
                                "tool_choice": _adaptive_tool_choice,
                                # v6.4.45 (maintainer) — ROOT FIX.
                                # The v6.4.43-A chunk-gap watchdog NEVER
                                # fires when relay hangs BEFORE any
                                # chunk arrives, because
                                # `client.chat.completions.create(stream=True)`
                                # itself blocks waiting for HTTP response
                                # headers. Until the first byte comes back,
                                # we're stuck in the openai SDK's urllib3
                                # recv() call — the watchdog thread isn't
                                # even started yet (it's defined AFTER
                                # create() returns). Passing `timeout` here
                                # hands the limit to httpx at the socket
                                # level, so a silent upstream raises
                                # ReadTimeout/APITimeoutError in ≤90s and
                                # the error path runs, instead of hanging
                                # for 4+ minutes.
                                "timeout": 90.0,
                            }
                            if _extra_body:
                                _create_kwargs["extra_body"] = _extra_body
                            # v6.4.46 (maintainer) — PRE-CREATE WATCHDOG.
                            # The openai SDK 1.x `timeout` parameter is NOT
                            # reliably enforced in stream=True mode — we saw
                            # relay hang 5+ minutes with timeout=90s set.
                            # The SDK passes timeout into httpx but httpx
                            # seems to only use it for connect, not for
                            # server-to-first-byte. So we add a DEDICATED
                            # pre-create watchdog that fires AT create() time
                            # and brutally closes the underlying httpx client
                            # — which wakes any blocked recv() with a
                            # ConnectionClosedError and lets the chat move on.
                            _v646_create_started = _time_v642.monotonic()
                            _v646_create_done = {"value": False}
                            _V646_CREATE_DEADLINE = 90.0
                            def _v646_create_watchdog():
                                import time as _tv646
                                while not _v646_create_done["value"]:
                                    _tv646.sleep(3.0)
                                    if _v646_create_done["value"]:
                                        return
                                    if _cancel_requested["value"]:
                                        logger.warning("chat_worker: cancel during create() — closing client")
                                        try:
                                            client.close()  # type: ignore[attr-defined]
                                        except Exception:
                                            pass
                                        try:
                                            _inner = getattr(client, "_client", None)
                                            if _inner is not None:
                                                _inner.close()
                                        except Exception:
                                            pass
                                        return
                                    _gap = _time_v642.monotonic() - _v646_create_started
                                    if _gap > _V646_CREATE_DEADLINE:
                                        logger.warning(
                                            "chat_worker: create() stuck %.0fs > %.0fs — closing httpx client",
                                            _gap, _V646_CREATE_DEADLINE,
                                        )
                                        # Try multiple levels of close so the
                                        # blocked socket recv wakes up.
                                        try:
                                            client.close()  # type: ignore[attr-defined]
                                        except Exception:
                                            pass
                                        try:
                                            _inner = getattr(client, "_client", None)
                                            if _inner is not None:
                                                _inner.close()
                                        except Exception:
                                            pass
                                        return
                            _v646_wd = threading.Thread(target=_v646_create_watchdog, daemon=True)
                            _v646_wd.start()
                            # v6.4.53-G (maintainer) — ROOT FIX for
                            # create() blocking. Three previous layers all
                            # failed to interrupt a hung
                            # `client.chat.completions.create(stream=True)`:
                            #   - v6.4.45 SDK timeout= param → ignored in stream
                            #   - v6.4.46 watchdog + client.close() → recv still
                            #     blocked (confirmed by 316s hang on Apr 23)
                            #   - v6.4.47 httpx custom Client read=45 → SSE
                            #     keep-alive bytes may reset the timer
                            # Here we run create() in a dedicated
                            # ThreadPoolExecutor and let the MAIN worker
                            # thread wait with Future.result(timeout=90).
                            # Python's Condition-variable wait is guaranteed
                            # to return within the timeout regardless of what
                            # the underlying TCP socket is doing. On timeout
                            # the inner thread is leaked (daemon, will die
                            # with process), but the worker recovers and
                            # reports a clean error to the user.
                            import concurrent.futures as _cf_v653g
                            _v653g_executor = _cf_v653g.ThreadPoolExecutor(
                                max_workers=1,
                                thread_name_prefix="chat_create",
                            )
                            try:
                                _v653g_future = _v653g_executor.submit(
                                    client.chat.completions.create,
                                    **_create_kwargs,
                                )
                                try:
                                    stream = _v653g_future.result(timeout=90.0)
                                except _cf_v653g.TimeoutError:
                                    logger.warning(
                                        "chat_worker: create() stuck > 90s — forcing httpx client close (v6.4.53-G)"
                                    )
                                    try:
                                        _chat_http_client.close()
                                    except Exception:
                                        pass
                                    try:
                                        _v653g_future.cancel()
                                    except Exception:
                                        pass
                                    raise Exception(
                                        f"模型 {chat_model} 对话建立超时（>90 秒）。中转站可能在排队或该模型暂不可用。"
                                        "建议：1) 切到 kimi-k2.6-code-preview；2) 或 30 秒后重试。"
                                    )
                            finally:
                                _v646_create_done["value"] = True
                                try:
                                    _v653g_executor.shutdown(wait=False, cancel_futures=False)
                                except TypeError:
                                    # Python < 3.9 may not support cancel_futures
                                    try:
                                        _v653g_executor.shutdown(wait=False)
                                    except Exception:
                                        pass
                                except Exception:
                                    pass
                            _assistant_content = ""
                            _tool_calls_acc: Dict[int, Dict[str, Any]] = {}
                            _finish = None
                            # v6.4.48-A (maintainer) — QUEUE-PUMP SSE
                            # idle watchdog. Replaces v6.4.43-A's background
                            # thread + timestamp polling, which had two
                            # problems confirmed by the GitHub research
                            # (openai-python#2319, claude-code#33949, httpx
                            # discussion #2055):
                            #   1. `stream.close()` does NOT unblock a recv()
                            #      already in flight at the TCP layer.
                            #   2. Normal-completion paths didn't signal the
                            #      watchdog to exit → thread leaked, later
                            #      spammed stale "stream gap 120s" WARNs.
                            # The queue-pump pattern puts a producer thread
                            # between the SDK stream and the consumer, and
                            # enforces idle-timeout via `queue.get(timeout)`.
                            # The consumer (our chat_worker) is never blocked
                            # in recv() — only in queue.get() which IS
                            # interruptable. On idle, we abandon the pump
                            # (daemon) and close the httpx client; next
                            # iter starts with a fresh pump + fresh queue.
                            # This is exactly how claude-code issue #33949
                            # ended up being fixed upstream.
                            import queue as _queue_v648
                            _V648_CHUNK_IDLE_LIMIT = 45.0  # matches httpx read=45
                            _chunk_q: "_queue_v648.Queue" = _queue_v648.Queue(maxsize=1024)
                            def _v648_stream_pump():
                                """v6.4.54 (maintainer) — FILTER empty chunks.
                                Previously we put EVERY chunk (including SSE
                                keep-alive noise / empty deltas) into the
                                queue. Consumer's queue.get(timeout=45) then
                                never fired, because something was always
                                arriving in the queue — even though there
                                was zero real progress. Observed at iter=5
                                Apr 23 13:19: 107s silent, no WARN.
                                Now we only put chunks that carry actual
                                content, tool_call fragments, reasoning,
                                or a finish_reason. Heartbeats go straight
                                in the trash. Consumer's 45s timeout is
                                now meaningful again."""
                                try:
                                    for _pchunk in stream:
                                        # Filter noise: skip chunks with no
                                        # choices / empty delta / nothing useful
                                        _is_meaningful = False
                                        try:
                                            if _pchunk.choices:
                                                _ch0 = _pchunk.choices[0]
                                                _d = getattr(_ch0, "delta", None)
                                                if getattr(_ch0, "finish_reason", None):
                                                    _is_meaningful = True
                                                elif _d is not None:
                                                    if (getattr(_d, "content", None)
                                                            or getattr(_d, "tool_calls", None)
                                                            or getattr(_d, "reasoning_content", None)
                                                            or getattr(_d, "reasoning", None)
                                                            or getattr(_d, "role", None)):
                                                        _is_meaningful = True
                                        except Exception:
                                            _is_meaningful = True  # be permissive on unknown shape
                                        if _is_meaningful:
                                            _chunk_q.put(("chunk", _pchunk))
                                        # heartbeat: drop silently so consumer can detect idle
                                        if _cancel_requested["value"]:
                                            break
                                except Exception as _pump_exc:
                                    try:
                                        _chunk_q.put(("error", _pump_exc))
                                    except Exception:
                                        pass
                                finally:
                                    try:
                                        _chunk_q.put(("end", None))
                                    except Exception:
                                        pass
                            _v648_pump = threading.Thread(target=_v648_stream_pump, daemon=True)
                            _v648_pump.start()
                            _v648_stream_aborted = False
                            while True:
                                # v6.4.43-E (retained): Stop button秒停
                                if _cancel_requested["value"]:
                                    logger.info("chat_worker: Stop requested — closing stream (v6.4.48)")
                                    try:
                                        stream.close()  # type: ignore[attr-defined]
                                    except Exception:
                                        pass
                                    try:
                                        _chat_http_client.close()
                                    except Exception:
                                        pass
                                    _v648_stream_aborted = True
                                    break
                                try:
                                    _q_kind, _q_payload = _chunk_q.get(timeout=_V648_CHUNK_IDLE_LIMIT)
                                except _queue_v648.Empty:
                                    logger.warning(
                                        "chat_worker: SSE idle %.0fs — closing httpx client (v6.4.48)",
                                        _V648_CHUNK_IDLE_LIMIT,
                                    )
                                    try:
                                        stream.close()  # type: ignore[attr-defined]
                                    except Exception:
                                        pass
                                    try:
                                        _chat_http_client.close()
                                    except Exception:
                                        pass
                                    _finish = "length"
                                    _assistant_content += (
                                        f"\n[SSE 空闲 >{int(_V648_CHUNK_IDLE_LIMIT)} 秒,已终止本轮。中转站响应异常,请重试或切换模型。]"
                                    )
                                    _v648_stream_aborted = True
                                    break
                                if _q_kind == "end":
                                    break
                                if _q_kind == "error":
                                    raise _q_payload
                                chunk = _q_payload
                                if not chunk.choices:
                                    continue
                                delta = chunk.choices[0].delta
                                _finish = chunk.choices[0].finish_reason or _finish
                                # v6.1.1: capture reasoning_content into stripper
                                # log buffer so the model can think internally
                                # without its CoT reaching the user.
                                # v6.4.29 (maintainer): ALSO stream
                                # reasoning_content to the UI as a separate
                                # event type so the frontend can show it in a
                                # collapsible "thinking" bubble (Claude Web /
                                # Gemini / o1 pattern). The reasoning no longer
                                # reaches the MAIN content stream, but it
                                # becomes visible, optional, and foldable.
                                _rc = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                                if _rc and _chat_think_stripper is not None:
                                    _chat_think_stripper.reasoning_parts.append(str(_rc))
                                if _rc:
                                    # Dedicated reasoning stream → UI folds it
                                    token_queue.put(("reasoning_delta", str(_rc)))
                                if delta and delta.content:
                                    _raw = delta.content
                                    _clean = _chat_think_stripper.feed(_raw) if _chat_think_stripper is not None else _raw
                                    if _clean:
                                        # Main content started → signal UI to
                                        # auto-collapse the reasoning bubble.
                                        # Idempotent: frontend only acts on the
                                        # first reasoning_done per message.
                                        if not getattr(_chat_think_stripper, "_reasoning_done_sent", False):
                                            token_queue.put(("reasoning_done", ""))
                                            try:
                                                _chat_think_stripper._reasoning_done_sent = True  # type: ignore[attr-defined]
                                            except Exception:
                                                pass
                                        _assistant_content += _clean
                                        token_queue.put(("token", _clean))
                                        # v6.4.41-1: streaming n-gram
                                        # repetition breaker. Checks every
                                        # ~40 tokens to keep cost low.
                                        if len(_assistant_content) > 200 and len(_assistant_content) % 40 < len(_clean):
                                            if _v641_detect_repetition(_assistant_content):
                                                try:
                                                    stream.close()  # type: ignore[attr-defined]
                                                except Exception:
                                                    pass
                                                token_queue.put(("token", "\n\n[检测到模型输出重复 3 次以上，已自动中断本轮。请换个问法或提供更具体的目标。]"))
                                                _finish = "length"  # sentinel to break outer loop
                                                _assistant_content += "\n[auto-aborted: repetition]"
                                                break
                                if delta and delta.tool_calls:
                                    for tc in delta.tool_calls:
                                        idx = tc.index
                                        entry = _tool_calls_acc.setdefault(idx, {
                                            "id": "", "function": {"name": "", "arguments": ""}
                                        })
                                        if tc.id:
                                            entry["id"] = tc.id
                                        if tc.function:
                                            if tc.function.name:
                                                entry["function"]["name"] = tc.function.name
                                            if tc.function.arguments:
                                                entry["function"]["arguments"] += tc.function.arguments
                            # v6.1.1: flush stripper buffer, post-process catch
                            # v6.4.35 — preserve reasoning_content BEFORE
                            # stripper reset so assistant_msg can carry it.
                            _captured_reasoning = ""
                            if _chat_think_stripper is not None:
                                try:
                                    _captured_reasoning = "".join(_chat_think_stripper.reasoning_parts or [])
                                except Exception:
                                    _captured_reasoning = ""
                                _tail = _chat_think_stripper.flush()
                                if _tail:
                                    _assistant_content += _tail
                                    token_queue.put(("token", _tail))
                                if _assistant_content:
                                    _cleaned_final = strip_think_tags_full(_assistant_content)
                                    if _cleaned_final != _assistant_content:
                                        _assistant_content = _cleaned_final
                                _chat_think_stripper = ThinkStripper()  # reset for next turn
                            if _assistant_content:
                                full_content += _assistant_content
                            # v6.4.53-E (maintainer): detect max_tokens
                            # truncation. If the LLM call ended with
                            # finish_reason="length" AND we have an incomplete
                            # tool_call (args mid-string), inject a system
                            # nudge so the NEXT iter tells the model about
                            # the cap. This short-circuits the Apr 23 12:46
                            # pattern: kimi sends 58KB write, get cut off,
                            # retries same strategy 3 more times. Now we
                            # pre-emptively tell it to use edit instead.
                            if _finish == "length" and _tool_calls_acc:
                                _any_malformed = False
                                for _tidx, _tentry in _tool_calls_acc.items():
                                    _targs = (_tentry.get("function") or {}).get("arguments") or ""
                                    if _targs and not _targs.rstrip().endswith("}"):
                                        _any_malformed = True
                                        break
                                if _any_malformed:
                                    logger.warning(
                                        "chat_worker: finish=length + malformed tool_call args → nudging for edit (v6.4.53-E)"
                                    )
                                    llm_messages.append({
                                        "role": "system",
                                        "content": (
                                            "[v6.4.53-E NUDGE] Your previous tool_call was TRUNCATED by the "
                                            f"max_tokens cap ({_chat_max_tokens}). This means you tried to write "
                                            "too much in one call. REMEDY on next turn:\n"
                                            "  (A) Use file_ops.edit with a SMALL old_string → new_string diff, "
                                            "not a full rewrite.\n"
                                            "  (B) If you must rewrite, split the file into 3-5 smaller write "
                                            "calls, one feature at a time.\n"
                                            "Do NOT retry the same oversized call — it will fail again."
                                        ),
                                    })
                            # v6.4.41-2: empty-response deadlock breaker.
                            # When finish_reason == "stop" and both the
                            # content and tool_calls are empty, we'd loop
                            # forever asking the model what it wants. Break.
                            if (not _tool_calls_acc) and (not _assistant_content):
                                token_queue.put(("token", "[模型返回空响应，已终止本轮。]"))
                                break
                            # v6.4.41-1b: if repetition breaker tripped,
                            # exit the outer loop too — don't re-prompt.
                            if _finish == "length" and "auto-aborted: repetition" in _assistant_content:
                                break
                            # v6.4.41-4: prose tool_call fallback. Some
                            # models emit `to=file_ops.read {"path":"x"}`
                            # as plain text instead of a real tool_call.
                            # If we have no structured tool_calls but the
                            # content looks like a prose tool call, build
                            # a synthetic tool_call.
                            if (not _tool_calls_acc) and _assistant_content:
                                _prose_match = _PROSE_TOOL_RE.search(_assistant_content)
                                if _prose_match:
                                    _pn = _prose_match.group("name") or ""
                                    _pa = _prose_match.group("action") or ""
                                    _pargs_raw = _prose_match.group("args") or "{}"
                                    if _pn in {"file_ops", "browser"}:
                                        try:
                                            _pargs_obj = _json.loads(_pargs_raw)
                                        except Exception:
                                            _pargs_obj = {}
                                        if _pa and "action" not in _pargs_obj:
                                            _pargs_obj["action"] = _pa
                                        _tool_calls_acc[0] = {
                                            "id": f"prose_{int(time.time()*1000)}",
                                            "function": {
                                                "name": _pn,
                                                "arguments": _json.dumps(_pargs_obj, ensure_ascii=False),
                                            },
                                        }
                                        token_queue.put(("token", "\n[检测到散文形式的工具调用,已自动转为结构化调用]"))
                            # No tool calls → done (or nudge and retry)
                            if not _tool_calls_acc:
                                # v6.4.55-A (maintainer) — CLINE NUDGE.
                                # Observed Apr 23 13:23: kimi read 2 files,
                                # output "让我先完整读取代码..." (50 chars),
                                # finish=stop, worker complete content_len=50
                                # but NO real changes made. From Cline's
                                # recursivelyMakeClineRequests: when the
                                # model completes without calling a tool on
                                # an execution task, inject a synthetic user
                                # message demanding tool use, then CONTINUE
                                # the loop. We don't latch forever — gate
                                # with _v655_no_tool_retry count (max 2).
                                # v6.4.55-A fix: use nonlocal-closure counter
                                # (declared outside while) instead of module-
                                # level attribute. See the per-turn reset
                                # above near _v648 / _v648_nudge_count init.
                                # Detect "execution task" keywords (same logic
                                # as v6.4.44-C).
                                # v6.7e (maintainer): tightened keyword list
                                # to avoid false-positive triggers on conversational
                                # questions ("请介绍" contained "介" → matched).
                                # Require an explicit action verb targeting file
                                # artifacts, not casual prose.
                                _exec_task = False
                                try:
                                    for _m in reversed(llm_messages):
                                        if isinstance(_m, dict) and _m.get("role") == "user":
                                            _lu = str(_m.get("content") or "")
                                            # Require explicit "修改代码/改代码/写代码/
                                            # 优化 X.html/部署 X/实现 Y" phrasings; generic
                                            # "介绍/列出" no longer triggers.
                                            _action_phrases = (
                                                "修改代码", "改代码", "写代码", "部署",
                                                "优化 ", "优化@", "优化index", "优化/",
                                                "调试", "修 bug", "修bug", "fix this",
                                                "edit the", "modify the", "implement",
                                                "重构", "删除代码", "apply patch",
                                            )
                                            if any(_k in _lu for _k in _action_phrases):
                                                _exec_task = True
                                            break
                                except Exception:
                                    pass
                                if (_exec_task and len(full_content.strip()) > 0
                                        and _v655_no_tool_retry < 2
                                        and _iter < _MAX_TOOL_ITERATIONS - 2):
                                    _v655_no_tool_retry += 1
                                    _cline_nudge = {
                                        "role": "user",
                                        "content": (
                                            "[system nudge] 你刚才只输出了文字没有调用任何工具。"
                                            "但这个任务明确要求你修改代码(优化/修/改/部署等)。"
                                            "你必须使用 file_ops.edit 或 file_ops.write 做出"
                                            "实际代码更改,不要再只做描述。如果还需要更多信息才能"
                                            "开始改,请先用 file_ops.read 读取具体文件段落。"
                                            "禁止只回复文字就结束。"
                                        ),
                                    }
                                    llm_messages.append(_cline_nudge)
                                    logger.warning(
                                        "chat_worker: Cline nudge — model finished with no tool_call "
                                        "(content_len=%d, retry %d/2)",
                                        len(full_content), _v655_no_tool_retry,
                                    )
                                    # v6.7e (maintainer): do NOT leak the
                                    # nudge signal to the user-visible token
                                    # stream. Observed 2026-04-24 chat_smoke:
                                    # "[系统提示:请执行实际代码修改...]" was appearing
                                    # 3× in the rendered answer on benign
                                    # "介绍你自己" prompts. The retry signal belongs
                                    # only in the server logs + llm_messages; do not
                                    # pollute the user's reply transcript.
                                    continue  # go to next iter in while
                                # v6.4.41-6: no-progress watchdog. Even if
                                # the turn had content, if we've been
                                # looping without new content OR new
                                # tool_calls for 3 rounds, stop.
                                if _v641_last_content_len == len(full_content):
                                    _v641_no_progress += 1
                                else:
                                    _v641_no_progress = 0
                                    _v641_last_content_len = len(full_content)
                                if _v641_no_progress >= 3:
                                    token_queue.put(("token", "\n[已连续 3 轮无新进展，终止当前任务。]"))
                                    break
                                break
                            # v6.4.41-3: tool-signature loop detection.
                            # If the same (name, args) fires 3 iterations
                            # in a row, the model is stuck. Abort.
                            _v641_sig = _v641_tool_signature(_tool_calls_acc)
                            _v641_recent_tool_sigs.append(_v641_sig)
                            if len(_v641_recent_tool_sigs) > 3:
                                _v641_recent_tool_sigs.pop(0)
                            if (len(_v641_recent_tool_sigs) >= 3 and
                                    len(set(_v641_recent_tool_sigs)) == 1 and
                                    _v641_sig):
                                token_queue.put(("token", "\n[检测到同一工具调用连续 3 次，可能进入死循环，已终止。请换个问法。]"))
                                break
                            # Append assistant tool_call message
                            # v6.4.35: include reasoning_content when kimi had
                            # thinking on, so next turn passes kimi's strict
                            # tool_call validation.
                            assistant_msg: Dict[str, Any] = {"role": "assistant", "content": _assistant_content or None}
                            if _captured_reasoning:
                                assistant_msg["reasoning_content"] = _captured_reasoning
                            tool_calls_list = []
                            for i in sorted(_tool_calls_acc.keys()):
                                tc = _tool_calls_acc[i]
                                if tc["function"].get("name"):
                                    tool_calls_list.append({
                                        "id": tc["id"] or f"call_{i}",
                                        "type": "function",
                                        "function": tc["function"],
                                    })
                            if tool_calls_list:
                                assistant_msg["tool_calls"] = tool_calls_list
                            llm_messages.append(assistant_msg)
                            # Execute each tool call, append tool result
                            for tc in tool_calls_list:
                                fn_name = tc["function"]["name"]
                                fn_args = tc["function"]["arguments"]
                                # v6.4.32 (maintainer): stream tool
                                # trace as a DEDICATED event so the UI can
                                # fold the raw args/result into a compact
                                # "[file_ops read index.html]" badge instead
                                # of dumping 20KB of HTML into chat. The
                                # legacy "[tool: ...]" token line is gone.
                                _tc_id = tc.get("id") or f"tc_{int(time.time()*1000)}"
                                try:
                                    _args_preview = str(fn_args)[:300]
                                except Exception:
                                    _args_preview = ""
                                token_queue.put(("tool_call_start", {
                                    "id": _tc_id,
                                    "name": fn_name,
                                    "args_preview": _args_preview,
                                    "args": fn_args,
                                }))
                                tool_output = ""
                                # v6.4.42: tool execution trace
                                _v642_tool_started = _time_v642.monotonic()
                                logger.info(
                                    "chat_tool ▶ %s args_len=%d iter=%d",
                                    fn_name, len(fn_args or ""), _iter,
                                )
                                # v6.4.49-B: large-args dump for debugging.
                                # The Apr 23 10:55 session had args_len=11607
                                # that we couldn't post-mortem — dump to
                                # /tmp so we can analyse why kimi/gpt would
                                # send such a big tool_call arguments body.
                                if len(fn_args or "") > 3000:
                                    try:
                                        import time as _tld
                                        _dump_path = f"/tmp/evermind_chat_large_args_{int(_tld.time())}_{fn_name}_iter{_iter}.json"
                                        with open(_dump_path, "w", encoding="utf-8") as _dfh:
                                            _dfh.write(fn_args or "")
                                        logger.info(
                                            "chat_tool large-args dumped: %s (%dB)",
                                            _dump_path, len(fn_args or ""),
                                        )
                                    except Exception:
                                        pass
                                try:
                                    import json as _json
                                    _v651_truncated = False
                                    try:
                                        args = _json.loads(fn_args or "{}")
                                    except _json.JSONDecodeError as _je:
                                        # v6.4.51: truncated tool_call
                                        # arguments (max_tokens hit mid
                                        # content). Set a flag and fall
                                        # through to a friendly error —
                                        # DON'T run the tool dispatch.
                                        _args_size = len(fn_args or "")
                                        logger.warning(
                                            "chat_tool: arguments JSON truncated (%dB) — %s",
                                            _args_size, str(_je)[:100],
                                        )
                                        args = {}
                                        _v651_truncated = True
                                        tool_output = _json.dumps({
                                            "success": False,
                                            "error": (
                                                f"Your tool_call arguments were TRUNCATED by max_tokens "
                                                f"({_args_size} bytes, unterminated JSON). "
                                                "SWITCH STRATEGY: use file_ops.edit with a small "
                                                "old_string + new_string instead of rewriting the whole file, "
                                                "OR split the write into multiple smaller chunks."
                                            ),
                                            "truncated_args_bytes": _args_size,
                                        }, ensure_ascii=False)
                                    if _v651_truncated:
                                        # skip tool execution entirely; tool_output already
                                        # contains the v6.4.51 truncation error message
                                        # v6.4.53-A (maintainer): explicitly reset
                                        # _result to None so the downstream preview
                                        # builder doesn't pick up stale _result from the
                                        # previous iter's successful read (causing
                                        # "success=True preview=37922B" on a truncated
                                        # write as observed at iter=18 Apr 23 12:46).
                                        _result = None
                                        _r_trim = None
                                    elif fn_name == "file_ops":
                                        # v5.8.6: direct sync impl (no async
                                        # plugin bridging from a worker thread).
                                        # Uses same actions as plugins.file_ops
                                        # so behavior matches pipeline mode.
                                        _a = str(args.get("action", "")).strip().lower()
                                        _p = str(args.get("path", "")).strip()
                                        # Expand ~ and relative to workspace
                                        if _p.startswith("~"):
                                            _p = str(Path(_p).expanduser())
                                        _result: Dict[str, Any] = {"success": False}
                                        if _a == "read":
                                            try:
                                                with open(_p, "r", encoding="utf-8", errors="replace") as _fh:
                                                    _content = _fh.read()
                                                # v6.4.43-C: dedupe re-reads of
                                                # unchanged files via content
                                                # hash cached per session.
                                                # v6.4.52-D: ALSO dedupe across
                                                # paths — if iter=3 reads
                                                # index.html.bak2 and its
                                                # content matches a file we
                                                # already returned (e.g. .bak
                                                # or .bak2 are byte-identical
                                                # copies of index.html), treat
                                                # it as a duplicate-read and
                                                # save the tokens. Track
                                                # BOTH path→hash AND hash→
                                                # known-path so we can report
                                                # "same as /x/y".
                                                import hashlib as _hashlib_v643
                                                _content_hash = _hashlib_v643.md5(_content.encode("utf-8", "ignore")).hexdigest()
                                                _session_mem_v643 = _SESSION_MEMORY.setdefault(session_id, {})
                                                _fread_cache = _session_mem_v643.setdefault("_file_read_hash", {})
                                                _hash_to_path = _session_mem_v643.setdefault("_hash_to_path", {})
                                                _was_unchanged = (_fread_cache.get(_p) == _content_hash)
                                                _duplicate_of = None
                                                if not _was_unchanged and _content_hash in _hash_to_path:
                                                    _duplicate_of = _hash_to_path.get(_content_hash)
                                                _fread_cache[_p] = _content_hash
                                                _hash_to_path.setdefault(_content_hash, _p)
                                                # v6.4.43-D: smart pagination.
                                                # Support offset/limit line
                                                # params. For huge files
                                                # without params, send head +
                                                # tail + "[...N lines omitted]"
                                                # so the model gets structural
                                                # context without eating 60KB
                                                # of token budget.
                                                _lines_all = _content.splitlines(keepends=True)
                                                _total_lines = len(_lines_all)
                                                _offset = int(args.get("offset") or 0)
                                                _limit = int(args.get("limit") or 0)
                                                _omitted = 0
                                                if _offset > 0 or _limit > 0:
                                                    _end = _total_lines if _limit <= 0 else min(_total_lines, _offset + _limit)
                                                    _slice = _lines_all[_offset:_end]
                                                    _view_content = "".join(_slice)
                                                    _view_note = f"[窗口 行{_offset + 1}–{_end} / 共{_total_lines}行]"
                                                elif _total_lines > 400 and len(_content) > 20000:
                                                    # Default head+tail window
                                                    _head_lines = _lines_all[:200]
                                                    _tail_lines = _lines_all[-100:]
                                                    _omitted = _total_lines - 300
                                                    _view_content = (
                                                        "".join(_head_lines)
                                                        + f"\n... [省略 {_omitted} 行,共 {_total_lines} 行,使用 offset/limit 读中段] ...\n"
                                                        + "".join(_tail_lines)
                                                    )
                                                    _view_note = f"[head200+tail100 共 {_total_lines} 行]"
                                                else:
                                                    _view_content = _content[:20000]
                                                    _view_note = ""
                                                if _was_unchanged:
                                                    # Content unchanged since last read → tiny result
                                                    _result = {
                                                        "success": True,
                                                        "path": _p,
                                                        "unchanged": True,
                                                        "size": len(_content),
                                                        "line_count": _total_lines,
                                                        "content": f"[unchanged since last read · {_total_lines} 行 · {len(_content)}B]",
                                                    }
                                                elif _duplicate_of and _duplicate_of != _p:
                                                    # v6.4.52-D: cross-path duplicate
                                                    _result = {
                                                        "success": True,
                                                        "path": _p,
                                                        "duplicate_of": _duplicate_of,
                                                        "size": len(_content),
                                                        "line_count": _total_lines,
                                                        "content": (
                                                            f"[此文件内容与 {_duplicate_of} 完全相同 (hash match) · "
                                                            f"{_total_lines} 行 · {len(_content)}B]"
                                                        ),
                                                    }
                                                else:
                                                    _result = {
                                                        "success": True,
                                                        "path": _p,
                                                        "content": _view_content,
                                                        "size": len(_content),
                                                        "line_count": _total_lines,
                                                    }
                                                    if _view_note:
                                                        _result["view"] = _view_note
                                                    if _omitted:
                                                        _result["lines_omitted"] = _omitted
                                                # v6.4.30: remember this file for next turn's prompt.
                                                _record_focused_file_in_session(session_id, _p)
                                            except Exception as _e:
                                                _result = {"success": False, "error": f"read failed: {_e}"}
                                        elif _a == "write":
                                            try:
                                                Path(_p).parent.mkdir(parents=True, exist_ok=True)
                                                _c = str(args.get("content", ""))
                                                # v6.4.42: count +N/-N lines
                                                _old_lines = 0
                                                try:
                                                    if Path(_p).exists():
                                                        with open(_p, "r", encoding="utf-8", errors="replace") as _fh_old:
                                                            _old_lines = _fh_old.read().count("\n") + (0 if _fh_old else 0)
                                                except Exception:
                                                    _old_lines = 0
                                                _new_lines = _c.count("\n") + (1 if _c and not _c.endswith("\n") else 0)
                                                with open(_p, "w", encoding="utf-8") as _fh:
                                                    _fh.write(_c)
                                                _added = max(0, _new_lines - _old_lines)
                                                _removed = max(0, _old_lines - _new_lines)
                                                _result = {
                                                    "success": True,
                                                    "path": _p,
                                                    "written": len(_c),
                                                    "lines_total": _new_lines,
                                                    "lines_added": _added,
                                                    "lines_removed": _removed,
                                                    "was_new": _old_lines == 0,
                                                }
                                                _record_focused_file_in_session(session_id, _p)
                                            except Exception as _e:
                                                _result = {"success": False, "error": f"write failed: {_e}"}
                                        elif _a == "list":
                                            try:
                                                _entries = []
                                                for _entry in os.scandir(_p or "."):
                                                    _entries.append({"name": _entry.name, "is_dir": _entry.is_dir(), "size": _entry.stat().st_size if _entry.is_file() else 0})
                                                _result = {"success": True, "path": _p, "entries": _entries[:200]}
                                            except Exception as _e:
                                                _result = {"success": False, "error": f"list failed: {_e}"}
                                        elif _a == "edit":
                                            try:
                                                _old = str(args.get("old_string", ""))
                                                _new = str(args.get("new_string", ""))
                                                with open(_p, "r", encoding="utf-8", errors="replace") as _fh:
                                                    _c = _fh.read()
                                                # v6.4.53-F (maintainer): lenient
                                                # matching. Exact substring match is the
                                                # fast path. If that fails, try a small
                                                # set of safe normalisations before
                                                # reporting "old_string not found". Most
                                                # mismatches we observed (iter=11/15/19
                                                # etc.) were one of:
                                                #   - model used \n, file has \r\n
                                                #   - model trimmed trailing whitespace
                                                #     on each line, file kept it
                                                #   - leading indent 4 spaces vs tab
                                                # Each attempt runs a search that, if
                                                # successful, rewrites _old to the
                                                # actual matching string so replace()
                                                # below hits.
                                                _edit_lenient_hit = False
                                                if _old and _old not in _c:
                                                    _try_candidates = []
                                                    # 1) CRLF ↔ LF
                                                    _try_candidates.append(_old.replace("\n", "\r\n"))
                                                    _try_candidates.append(_old.replace("\r\n", "\n"))
                                                    # 2) Normalize trailing WS per line
                                                    _trim_lines = "\n".join(
                                                        _l.rstrip() for _l in _old.splitlines()
                                                    )
                                                    if _old.endswith("\n"):
                                                        _trim_lines += "\n"
                                                    _try_candidates.append(_trim_lines)
                                                    # 3) Tab ↔ 4 spaces (light)
                                                    _try_candidates.append(_old.replace("\t", "    "))
                                                    _try_candidates.append(_old.replace("    ", "\t"))
                                                    for _cand in _try_candidates:
                                                        if _cand and _cand != _old and _cand in _c:
                                                            logger.info(
                                                                "chat_tool edit: lenient match hit (variant len=%d)",
                                                                len(_cand),
                                                            )
                                                            _old = _cand
                                                            _edit_lenient_hit = True
                                                            break
                                                # v6.4.55-B (maintainer) — Aider-style
                                                # SequenceMatcher fallback. If exact match
                                                # AND lenient-normalization (v6.4.53-F) BOTH
                                                # failed, try fuzzy matching: split the file
                                                # into windows the same size as _old, score
                                                # each with difflib.SequenceMatcher, and if
                                                # best ratio >= 0.8 replace that window.
                                                # This catches the common case where the
                                                # model's old_string is semantically right
                                                # but differs in 1-2 characters (comment,
                                                # whitespace, typo).
                                                if _old and _old not in _c:
                                                    try:
                                                        import difflib as _difflib_v655
                                                        _old_lines_v655 = _old.splitlines(keepends=True)
                                                        if 2 <= len(_old_lines_v655) <= 50:
                                                            _file_lines_v655 = _c.splitlines(keepends=True)
                                                            _window_size = len(_old_lines_v655)
                                                            _best_ratio = 0.0
                                                            _best_i = -1
                                                            _limit_scan = min(len(_file_lines_v655) - _window_size + 1, 5000)
                                                            for _i in range(max(0, _limit_scan)):
                                                                _candidate = "".join(
                                                                    _file_lines_v655[_i:_i + _window_size]
                                                                )
                                                                _r = _difflib_v655.SequenceMatcher(
                                                                    None, _old, _candidate, autojunk=False,
                                                                ).quick_ratio()
                                                                if _r > _best_ratio:
                                                                    _best_ratio = _r
                                                                    _best_i = _i
                                                                    if _r >= 0.98:
                                                                        break
                                                            if _best_ratio >= 0.8 and _best_i >= 0:
                                                                _old = "".join(
                                                                    _file_lines_v655[_best_i:_best_i + _window_size]
                                                                )
                                                                logger.info(
                                                                    "chat_tool edit: fuzzy match hit (ratio=%.2f at line %d)",
                                                                    _best_ratio, _best_i + 1,
                                                                )
                                                    except Exception as _fuzze:
                                                        logger.warning("chat_tool edit fuzzy failed: %s", _fuzze)
                                                if _old and _old in _c:
                                                    _replace_all = bool(args.get("replace_all"))
                                                    _c2 = _c.replace(_old, _new) if _replace_all else _c.replace(_old, _new, 1)
                                                    # v6.4.42: +N/-N lines per edit
                                                    _reps = _c.count(_old) if _replace_all else 1
                                                    _old_nl = _old.count("\n") * _reps
                                                    _new_nl = _new.count("\n") * _reps
                                                    _added = max(0, _new_nl - _old_nl)
                                                    _removed = max(0, _old_nl - _new_nl)
                                                    with open(_p, "w", encoding="utf-8") as _fh:
                                                        _fh.write(_c2)
                                                    _result = {
                                                        "success": True,
                                                        "path": _p,
                                                        "replacements": _reps,
                                                        "lines_added": _added,
                                                        "lines_removed": _removed,
                                                    }
                                                    _record_focused_file_in_session(session_id, _p)
                                                else:
                                                    # v6.4.53-B (maintainer): fuzzy
                                                    # suggestion. 3+ consecutive "old_string
                                                    # not found" iters was the dominant
                                                    # failure mode in the Apr 23 12:48
                                                    # session. Now, when old_string doesn't
                                                    # match, run difflib on the FIRST line
                                                    # of the model's old_string and return
                                                    # up to 3 closest 5-line windows from
                                                    # the file, with line numbers. The model
                                                    # can copy the exact text into its next
                                                    # attempt.
                                                    _hint_lines = []
                                                    try:
                                                        import difflib as _difflib_v653
                                                        _file_lines = _c.splitlines()
                                                        _old_first = ""
                                                        for _ln_ol in _old.splitlines():
                                                            if _ln_ol.strip():
                                                                _old_first = _ln_ol.strip()
                                                                break
                                                        if _old_first and _file_lines:
                                                            _candidates = [ _l.strip() for _l in _file_lines ]
                                                            _close = _difflib_v653.get_close_matches(
                                                                _old_first, _candidates,
                                                                n=3, cutoff=0.55,
                                                            )
                                                            for _m in _close:
                                                                try:
                                                                    _idx = _candidates.index(_m)
                                                                except ValueError:
                                                                    continue
                                                                _a_start = max(0, _idx - 2)
                                                                _a_end = min(len(_file_lines), _idx + 4)
                                                                _window = "\n".join(
                                                                    _file_lines[_a_start:_a_end]
                                                                )[:600]
                                                                _hint_lines.append({
                                                                    "near_line": _idx + 1,
                                                                    "context": _window,
                                                                })
                                                    except Exception:
                                                        pass
                                                    _result = {
                                                        "success": False,
                                                        "error": "old_string not found",
                                                        "hint": (
                                                            "Your old_string does NOT match the file byte-for-byte. "
                                                            "Common causes: tab vs 4-space indent, trailing whitespace, "
                                                            "\\n at end missing, backslash-escaped quotes in HTML "
                                                            "attributes. Below are the closest matching windows "
                                                            "in the file — COPY ONE verbatim as your next old_string."
                                                        ),
                                                        "closest_matches": _hint_lines,
                                                    }
                                            except Exception as _e:
                                                _result = {"success": False, "error": f"edit failed: {_e}"}
                                        elif _a == "delete":
                                            try:
                                                if Path(_p).exists():
                                                    os.remove(_p)
                                                    _result = {"success": True, "deleted": _p}
                                                else:
                                                    _result = {"success": False, "error": "not found"}
                                            except Exception as _e:
                                                _result = {"success": False, "error": str(_e)}
                                        elif _a == "search":
                                            # v6.4.37 (maintainer) — real
                                            # search. Previously chat mode
                                            # returned "unknown action: search"
                                            # even though the system prompt told
                                            # the model to use it. Supports a
                                            # text pattern (substring by default,
                                            # regex when pattern contains regex
                                            # metachars and `regex=true`) across
                                            # files under `path` (default
                                            # OUTPUT_DIR). Returns up to 60
                                            # matches with file:line.
                                            try:
                                                _pattern = str(args.get("pattern") or args.get("query") or "").strip()
                                                if not _pattern:
                                                    _result = {"success": False, "error": "search: missing 'pattern' arg"}
                                                else:
                                                    _search_root = Path(_p).expanduser() if _p else OUTPUT_DIR
                                                    if not _search_root.exists():
                                                        _result = {"success": False, "error": f"search: path not found: {_search_root}"}
                                                    else:
                                                        _glob = str(args.get("glob") or args.get("include") or "**/*")
                                                        _use_regex = bool(args.get("regex"))
                                                        _matches: list = []
                                                        _max = int(args.get("max_results") or 60)
                                                        _pat_re = None
                                                        if _use_regex:
                                                            try:
                                                                _pat_re = re.compile(_pattern)
                                                            except Exception as _re_e:
                                                                _result = {"success": False, "error": f"invalid regex: {_re_e}"}
                                                                _pat_re = "error"
                                                        if _pat_re != "error":
                                                            # v6.4.40 (maintainer) RENAMED
                                                            # from _iter → _search_paths. The old
                                                            # name shadowed the outer while-loop
                                                            # counter, making `while _iter <
                                                            # _MAX_TOOL_ITERATIONS:` compare a
                                                            # Path glob iterator to an int and
                                                            # crash with "TypeError: '<' not
                                                            # supported between instances of
                                                            # 'list' and 'int'" after the first
                                                            # search call.
                                                            # v6.4.42 (maintainer): accept
                                                            # absolute globs. Path.glob() rejects
                                                            # absolute patterns in 3.13+ with
                                                            # "Non-relative patterns are
                                                            # unsupported". Fall back to stdlib
                                                            # glob for absolute patterns, or
                                                            # rewrite pattern to relative.
                                                            import glob as _stdglob
                                                            if _search_root.is_dir():
                                                                try:
                                                                    _search_paths = list(_search_root.glob(_glob))
                                                                except (ValueError, NotImplementedError):
                                                                    # Absolute pattern → use stdlib glob
                                                                    if os.path.isabs(_glob):
                                                                        _search_paths = [Path(p) for p in _stdglob.glob(_glob, recursive=True)]
                                                                    else:
                                                                        # Try joining with root as string pattern
                                                                        _combined = str(_search_root / _glob)
                                                                        _search_paths = [Path(p) for p in _stdglob.glob(_combined, recursive=True)]
                                                            else:
                                                                _search_paths = [_search_root]
                                                            for _fp in _search_paths:
                                                                if len(_matches) >= _max:
                                                                    break
                                                                try:
                                                                    if not _fp.is_file():
                                                                        continue
                                                                    if _fp.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".mp3", ".mp4", ".wav", ".ogg"}:
                                                                        continue
                                                                    if _fp.stat().st_size > 2_000_000:
                                                                        continue
                                                                    _txt = _fp.read_text(encoding="utf-8", errors="replace")
                                                                except Exception:
                                                                    continue
                                                                for _ln, _line in enumerate(_txt.splitlines(), 1):
                                                                    hit = bool(_pat_re.search(_line)) if _pat_re else (_pattern in _line)
                                                                    if hit:
                                                                        _matches.append({
                                                                            "file": str(_fp),
                                                                            "line": _ln,
                                                                            "text": _line.strip()[:200],
                                                                        })
                                                                        if len(_matches) >= _max:
                                                                            break
                                                            _result = {
                                                                "success": True,
                                                                "pattern": _pattern,
                                                                "path": str(_search_root),
                                                                "match_count": len(_matches),
                                                                "matches": _matches,
                                                            }
                                            except Exception as _e:
                                                _result = {"success": False, "error": f"search failed: {_e}"}
                                        else:
                                            _result = {"success": False, "error": f"unknown action: {_a}"}
                                        tool_output = _json.dumps(_result, ensure_ascii=False)[:4000]
                                    elif fn_name == "browser":
                                        # v6.0: chat agent browser tool
                                        _action = str(args.get("action", "")).strip().lower()
                                        if not _action:
                                            tool_output = _json.dumps({"success": False, "error": "browser: missing action"})
                                        else:
                                            _params: Dict[str, Any] = {"action": _action}
                                            for _k in ("url", "selector", "text", "direction",
                                                       "amount", "timeout_ms", "key", "tab_index"):
                                                if _k in args and args[_k] is not None:
                                                    _params[_k] = args[_k]
                                            if "ref" in args and args["ref"] is not None:
                                                _params["ref_index"] = int(args["ref"])
                                            # v6.4.42: per-action sub-timeout so a
                                            # single stuck navigate can't swallow
                                            # the whole chat turn.
                                            _r = _invoke_chat_browser_tool(
                                                _params,
                                                timeout_sec=_BROWSER_SUB_TIMEOUT(_action),
                                            )
                                            # Keep payload small (full screenshot bytes are dropped).
                                            if isinstance(_r, dict):
                                                _r_trim: Dict[str, Any] = {"success": _r.get("success")}
                                                if _r.get("error"):
                                                    _r_trim["error"] = _r["error"]
                                                _data = _r.get("data") or {}
                                                if isinstance(_data, dict):
                                                    # Prefer summary text fields when present
                                                    for _k in ("url", "title", "result_text",
                                                                "extracted", "visible_text",
                                                                "state_hash", "ax_tree_summary"):
                                                        if _k in _data and _data[_k]:
                                                            _v = _data[_k]
                                                            if isinstance(_v, str):
                                                                _r_trim[_k] = _v[:2000]
                                                            else:
                                                                _r_trim[_k] = _v
                                                    # Screenshots become a note, not bytes
                                                    if _data.get("screenshot"):
                                                        _r_trim["screenshot"] = "[screenshot captured and shown to user]"
                                                tool_output = _json.dumps(_r_trim, ensure_ascii=False)[:4000]
                                            else:
                                                tool_output = _json.dumps({"success": False, "error": "unexpected browser result"})
                                    else:
                                        tool_output = _json.dumps({"success": False, "error": f"tool {fn_name} not enabled in chat mode"})
                                except Exception as exc:
                                    tool_output = _json.dumps({"success": False, "error": str(exc)[:200]})
                                # v6.4.32/37: tool result as dedicated event (not token).
                                # Keep a short preview + success flag; full result
                                # stays in llm_messages for the model, never hits
                                # the user-visible chat stream.
                                # v6.4.37 fix: DON'T re-parse tool_output — it's
                                # truncated at 4000 chars and the trailing JSON
                                # is broken, which makes _json.loads fail,
                                # which set _success=False and showed a red ✗
                                # in UI even when the read succeeded.
                                # Use the original `_result` dict (or `_r` for
                                # browser) which is still intact in memory.
                                _r_obj_for_ui = None
                                if fn_name == "file_ops":
                                    # _result is defined in every action branch above
                                    try:
                                        _r_obj_for_ui = _result if isinstance(_result, dict) else None
                                    except NameError:
                                        _r_obj_for_ui = None
                                elif fn_name == "browser":
                                    try:
                                        _r_obj_for_ui = _r_trim if isinstance(_r_trim, dict) else None
                                    except NameError:
                                        _r_obj_for_ui = None
                                if _r_obj_for_ui is None:
                                    try:
                                        _r_obj_for_ui = _json.loads(tool_output) if tool_output else {}
                                    except Exception:
                                        _r_obj_for_ui = {}
                                _r_obj = _r_obj_for_ui
                                _success = bool(_r_obj.get("success", False)) if isinstance(_r_obj, dict) else False
                                # Produce a 1-line preview: prefer path + status, strip big content blobs.
                                _preview_parts = []
                                if isinstance(_r_obj, dict):
                                    _p = _r_obj.get("path") or _r_obj.get("url")
                                    if _p: _preview_parts.append(str(_p)[:80])
                                    if _r_obj.get("size") is not None:
                                        _preview_parts.append(f"{_r_obj.get('size')}B")
                                    # v6.4.42: show +N/-N lines for write/edit
                                    _la = _r_obj.get("lines_added")
                                    _lr = _r_obj.get("lines_removed")
                                    if (_la is not None) or (_lr is not None):
                                        _la_i = int(_la or 0)
                                        _lr_i = int(_lr or 0)
                                        if _la_i or _lr_i or _r_obj.get("was_new"):
                                            _diff_str = f"+{_la_i}/-{_lr_i} 行"
                                            if _r_obj.get("was_new"):
                                                _diff_str = f"新文件 +{_la_i} 行"
                                            _preview_parts.append(_diff_str)
                                    if _r_obj.get("replacements") is not None:
                                        _preview_parts.append(f"{_r_obj.get('replacements')} replaced")
                                    _err = _r_obj.get("error")
                                    if _err: _preview_parts.append(f"error: {str(_err)[:80]}")
                                _preview = " · ".join(_preview_parts) or (tool_output[:120] if tool_output else "")
                                # v6.4.42: log tool outcome
                                _v642_tool_elapsed = _time_v642.monotonic() - _v642_tool_started
                                logger.info(
                                    "chat_tool %s %s success=%s elapsed=%.1fs preview=%s",
                                    "✓" if _success else "✗", fn_name, _success,
                                    _v642_tool_elapsed, _preview[:120],
                                )
                                token_queue.put(("tool_call_result", {
                                    "id": _tc_id,
                                    "name": fn_name,
                                    "success": _success,
                                    "preview": _preview,
                                    "result_truncated": tool_output[:2000],
                                }))
                                # v7.7 (maintainer): some relays (kimi
                                # /coding/v1) occasionally return tool_call
                                # objects with empty `id`. Reuse the same
                                # `_tc_id` synthesised earlier (line ~10234)
                                # so the tool-result frame and the LLM
                                # follow-up message agree on the id.
                                # Without this, kimi 400s next iteration
                                # ("tool_call_id is not found"), killing the
                                # whole chat session.
                                llm_messages.append({
                                    "role": "tool",
                                    "tool_call_id": _tc_id,
                                    "content": tool_output,
                                })
                            # v6.4.48-C: after all tool calls in this iter,
                            # update read-only streak. If every tool_call in
                            # this iter was a read/list/search (no write/edit),
                            # increment; any write/edit resets to 0. After 6
                            # read-only iters, inject a one-shot nudge urging
                            # the model to commit changes.
                            # v6.4.49-B: expanded readonly set. Previously
                            # missed: observe/dom_snapshot/ax_tree/get/
                            # query_selector/stat/exists, which are all
                            # read-only — without these in the set,
                            # iter=3's `browser observe` incorrectly reset
                            # the streak and the read-only nudge never
                            # fired in the Apr 23 10:53 session.
                            _v648_all_readonly_actions = {
                                # file_ops read-ish
                                "read", "list", "search", "grep", "stat",
                                "exists", "glob", "find",
                                # browser read-ish
                                "screenshot", "navigate", "visit", "goto",
                                "observe", "extract_text", "get_html",
                                "get", "query_selector", "dom_snapshot",
                                "ax_tree", "get_text", "get_attribute",
                                "wait_for_selector", "find",
                            }
                            _v648_this_iter_all_readonly = True
                            for _tcl in tool_calls_list:
                                _fname_v648 = _tcl.get("function", {}).get("name", "")
                                try:
                                    _args_v648 = _json.loads(_tcl.get("function", {}).get("arguments", "") or "{}")
                                    _act_v648 = str(_args_v648.get("action", "")).strip().lower()
                                except Exception:
                                    _act_v648 = ""
                                # file_ops with non-readonly action → breaks streak
                                if _fname_v648 == "file_ops" and _act_v648 not in _v648_all_readonly_actions:
                                    _v648_this_iter_all_readonly = False
                                    break
                                # browser with non-readonly action → breaks streak
                                if _fname_v648 == "browser" and _act_v648 not in _v648_all_readonly_actions:
                                    _v648_this_iter_all_readonly = False
                                    break
                            if _v648_this_iter_all_readonly:
                                _v648_readonly_streak += 1
                            else:
                                _v648_readonly_streak = 0
                            if _v648_readonly_streak >= _V648_READONLY_NUDGE_AFTER:
                                _v648_nudge_count += 1
                                if _v648_nudge_count == 1:
                                    _nudge_text = (
                                        f"[NUDGE #1] 你已连续 {_v648_readonly_streak} 轮只在 read/list/observe。"
                                        "你已经有足够信息。**下一轮必须用 file_ops.write 或 file_ops.edit 改代码**,"
                                        "或用 browser.click/fill 操作实际 UI。禁止再 read 同一个文件。"
                                    )
                                elif _v648_nudge_count == 2:
                                    _nudge_text = (
                                        f"[NUDGE #2 — 警告] 你又连续 {_v648_readonly_streak} 轮只在读取。"
                                        "你的工具调用没有推进任务。现在立刻做以下之一:\n"
                                        "(A) 用 file_ops.edit 修改具体代码片段(提供 old_string 和 new_string);\n"
                                        "(B) 用 file_ops.write 重写整个文件;\n"
                                        "(C) 用文字告诉用户你的诊断结论和建议 — 不要再调任何工具。\n"
                                        "如果你想调用但工具失败过(例如 browser.screenshot Unknown action),"
                                        "请绕过它 — 不要重试同一个失败工具。"
                                    )
                                else:  # 第 3 次及以上 — 直接 kill tools
                                    _nudge_text = (
                                        f"[NUDGE #{_v648_nudge_count} — 强制终止工具链] "
                                        "你已 3 次以上陷入只读循环。现在 **禁止** 再调用任何工具,"
                                        "下一轮必须 **只输出纯文字**:总结你已收集的信息 + 给用户一个明确结论 "
                                        "(\"我建议改这里\" 或 \"需要更多信息\")。"
                                    )
                                    _v648_force_no_tools_next = True
                                llm_messages.append({"role": "system", "content": _nudge_text})
                                logger.warning(
                                    "chat_worker: injected read-only nudge #%d after %d iters (force_no_tools_next=%s)",
                                    _v648_nudge_count, _v648_readonly_streak, _v648_force_no_tools_next,
                                )
                                _v648_readonly_streak = 0  # reset so next nudge needs another 6 iters
                            # Loop — let AI consume tool result and respond
                        # v6.1.2: tell the user explicitly when the loop hit its
                        # ceiling. Previously the chat just stopped with no
                        # explanation, which users read as "it broke".
                        if _iter >= _MAX_TOOL_ITERATIONS:
                            _hit_msg = (
                                f"\n\n[达到工具调用上限 {_MAX_TOOL_ITERATIONS} 次未完成任务。"
                                f"你可以再次发送消息（例如\"继续\"）让 AI 接着之前的工具结果做下一步。"
                                f"如果是浏览器操作卡在选择器上，告诉 AI 具体想点哪个按钮/链接的文字更容易成功。]"
                            )
                            token_queue.put(("token", _hit_msg))
                            full_content += _hit_msg
                        # v6.4.49-A (maintainer) — SILENT COMPLETION
                        # RESCUE. Root cause of the "12 iter content_len=0"
                        # we kept seeing: kimi (and sometimes gpt-5.x)
                        # finishes a long tool_call chain with
                        # `finish_reason=stop` and NO text content. The
                        # frontend sees "done" event with empty string,
                        # renders nothing, user thinks Stop failed → user
                        # spams the same question → session bloats with
                        # identical user messages, 0 assistant messages
                        # (exactly what we observed in session_moavbmsq).
                        # Fix: if full_content is empty when we're about
                        # to send "done", make ONE last non-streaming LLM
                        # call with tool_choice="none" + a terse user
                        # prompt asking for a diagnostic summary. Guaranteed
                        # to yield ≥1 line of content in 99% of cases.
                        if not full_content.strip() and _iter >= 1:
                            logger.warning(
                                "chat_worker: SILENT COMPLETION detected "
                                "(iter=%d, 0 chars). Running rescue call.",
                                _iter,
                            )
                            try:
                                _rescue_messages = _sanitize_chat_messages(llm_messages) + [{
                                    "role": "user",
                                    "content": (
                                        "[rescue] 基于你刚才收集的所有工具结果,请直接用中文回答:\n"
                                        "1. 你发现的核心问题是什么?\n"
                                        "2. 你建议的修复方案(具体到哪个文件哪一段代码)?\n"
                                        "3. 下一步你想做什么? "
                                        "(如果需要继续改代码,请告诉我你打算用 file_ops.edit 修改哪些片段 "
                                        "—— 不要再调工具,只说文字。)"
                                    ),
                                }]
                                _rescue_kwargs = {
                                    "model": api_model_id,
                                    "messages": _rescue_messages,
                                    "stream": False,
                                    "temperature": 0.6,
                                    "max_tokens": 2048,
                                    "tool_choice": "none",
                                    "timeout": 60.0,
                                }
                                if _extra_body:
                                    _rescue_kwargs["extra_body"] = _extra_body
                                _rescue_resp = client.chat.completions.create(**_rescue_kwargs)
                                _rescue_text = ""
                                try:
                                    _rescue_text = _rescue_resp.choices[0].message.content or ""
                                except Exception:
                                    _rescue_text = ""
                                if _rescue_text.strip():
                                    # Strip think tags just in case
                                    try:
                                        _rescue_text = strip_think_tags_full(_rescue_text)
                                    except Exception:
                                        pass
                                    token_queue.put(("token", _rescue_text))
                                    full_content += _rescue_text
                                    logger.warning(
                                        "chat_worker: rescue produced %d chars",
                                        len(_rescue_text),
                                    )
                                else:
                                    _fallback_msg = (
                                        "\n[我收集了一些文件信息但没有成功生成文字结论。"
                                        "请重新发送消息并具体说明你想改哪个行为(例如\"把 index.html 里的准星改成可点击\")。]"
                                    )
                                    token_queue.put(("token", _fallback_msg))
                                    full_content += _fallback_msg
                            except Exception as _resc_err:
                                logger.warning(
                                    "chat_worker: rescue call failed: %s",
                                    str(_resc_err)[:200],
                                )
                                _fallback_msg = (
                                    "\n[抱歉,模型这轮没有生成文字回复,请重发消息。"
                                    f"错误:{str(_resc_err)[:120]}]"
                                )
                                token_queue.put(("token", _fallback_msg))
                                full_content += _fallback_msg
                        # v6.4.42: log worker completion
                        _v642_total_elapsed = _time_v642.monotonic() - _v642_turn_started
                        logger.info(
                            "chat_worker ✓ complete iter=%d content_len=%d elapsed=%.1fs model=%s",
                            _iter, len(full_content), _v642_total_elapsed, chat_model,
                        )
                        token_queue.put(("done", full_content))
                    except Exception as e:
                        # v6.3 (maintainer): log chat errors with FULL
                        # detail (http status + response body + message count
                        # + tool count) so a "Your request was blocked"
                        # mystery error from the upstream relay can be
                        # correlated with what we actually sent.
                        # v6.4.38 (maintainer): also log the FULL
                        # Python traceback to the backend log — we had a
                        # TypeError "'<' not supported between instances of
                        # 'map' and 'int'" from deep in the OpenAI SDK with
                        # no frame info, making it unfixable from outside.
                        try:
                            import traceback as _tb
                            _full_tb = _tb.format_exc()
                            if _full_tb:
                                logger.error(
                                    "chat_worker FULL traceback (v6.4.38):\n%s",
                                    _full_tb,
                                )
                        except Exception:
                            pass
                        try:
                            _err_type = type(e).__name__
                            _err_status = getattr(e, "status_code", None) or getattr(e, "http_status", None)
                            _err_body = ""
                            _resp = getattr(e, "response", None)
                            if _resp is not None:
                                try:
                                    _err_body = _resp.text[:800] if hasattr(_resp, "text") else ""
                                except Exception:
                                    pass
                            if not _err_body:
                                _body = getattr(e, "body", None)
                                if _body:
                                    _err_body = str(_body)[:800]
                            _msg_count = len(llm_messages) if isinstance(llm_messages, list) else 0
                            _first_user_chars = 0
                            _sys_chars = 0
                            if isinstance(llm_messages, list):
                                for _m in llm_messages:
                                    if _m.get("role") == "system":
                                        _sys_chars = max(_sys_chars, len(str(_m.get("content") or "")))
                                    elif _m.get("role") == "user" and not _first_user_chars:
                                        _first_user_chars = len(str(_m.get("content") or ""))
                            logger.warning(
                                "chat_worker error: model=%s type=%s status=%s msgs=%d sys_chars=%d user_chars=%d tools=%d err=%s body=%s",
                                chat_model, _err_type, _err_status, _msg_count,
                                _sys_chars, _first_user_chars,
                                len(_chat_tools) if isinstance(_chat_tools, list) else 0,
                                str(e)[:200], _err_body[:400],
                            )
                            # v6.3 (maintainer): dump full request body
                            # to /tmp so mystery content-filter blocks can be
                            # inspected without adding 50KB to the log.
                            try:
                                import time as _t, json as _j
                                _dump_path = f"/tmp/evermind_chat_block_{int(_t.time())}.json"
                                with open(_dump_path, "w", encoding="utf-8") as _fh:
                                    _j.dump({
                                        "model": chat_model,
                                        "error_type": _err_type,
                                        "status": _err_status,
                                        "err": str(e)[:1000],
                                        "messages": llm_messages if isinstance(llm_messages, list) else [],
                                        "tools": _chat_tools if isinstance(_chat_tools, list) else [],
                                    }, _fh, ensure_ascii=False, indent=2, default=str)
                                logger.warning("chat_worker error dump: %s", _dump_path)
                            except Exception as _dump_err:
                                logger.warning("chat_worker dump failed: %s", _dump_err)
                        except Exception:
                            logger.warning("chat_worker error (fallback): model=%s err=%s", chat_model, str(e)[:300])
                        _user_err = str(e)
                        try:
                            _status_hint = _err_status
                        except NameError:
                            _status_hint = None
                        _low = _user_err.lower()
                        _is_block = (_status_hint == 403) or ("your request was blocked" in _low) or ("request was blocked" in _low)
                        if _is_block:
                            _user_err = (
                                f"模型 {chat_model} 被中转站拒绝（403）：余额耗尽或该模型在当前中转不可用。"
                                "请在左上角切换到 kimi-k2.6-code-preview，或在设置里更换 API key/base_url。"
                            )
                        elif _status_hint == 401:
                            # v7.8c (maintainer): long chat sessions occasionally hit
                            # 401 even when the key is valid — relay session timeout / cached
                            # auth in the httpx client. Close the cached client + clear the
                            # OpenAI client cache so the NEXT user turn rebuilds with a fresh
                            # connection and re-reads the current key from settings.
                            try:
                                _chat_http_client.close()
                            except Exception:
                                pass
                            try:
                                # Clear AIBridge's openai_compat client cache; rebuilds on next call.
                                if ai_bridge_instance is not None and hasattr(ai_bridge_instance, "_openai_clients"):
                                    ai_bridge_instance._openai_clients.clear()
                            except Exception:
                                pass
                            _user_err = (
                                f"模型 {chat_model} 鉴权失败（401）。已自动清空连接缓存——下次发送消息时会用新连接重试。"
                                "如果仍然 401，请到设置里检查/更新 API key。"
                            )
                        elif _status_hint in (404, 400) and ("model" in _low or "not found" in _low):
                            _user_err = f"模型 {chat_model} 在当前中转不存在（{_status_hint}）：请切换其他模型。"
                        elif ("timeout" in _low or "timed out" in _low
                              or "read timeout" in _low
                              or "apitimeout" in _low):
                            # v6.4.45: friendlier message for the httpx
                            # 90s read timeout we just added.
                            _user_err = (
                                f"模型 {chat_model} 响应超时（>90 秒无首字节）。中转站拥塞或模型暂时不可用。"
                                "建议: 1) 在左上角切到 kimi-k2.6-code-preview（国产模型更稳）; "
                                "2) 或稍等 30 秒后重发消息。"
                            )
                        token_queue.put(("error", _user_err))

                worker = threading.Thread(target=_chat_worker, daemon=True)
                worker.start()

                # Drain queue and send tokens via WS
                import asyncio as _aio
                full_response = ""
                # v6.4.34 (maintainer) — NO fixed timeout on chat.
                # Chat is user-driven; if the model takes 20 min to finish
                # a complex /fix task, that's fine. User clicks Stop to
                # cancel. We still use a large sanity ceiling (1 hour) so
                # a truly wedged backend thread doesn't leak a WS forever.
                # A heartbeat every 30s keeps WebSocket infrastructure
                # (proxies, load balancers, Electron's own socket layer)
                # happy — pure NO-timeout can be killed by intermediaries
                # after their own 60-90s idle cutoff.
                _chat_idle_sanity_sec = 3600
                _chat_heartbeat_every = 30
                _last_activity = time.time()
                # v6.4.43: _cancel_requested is HOISTED above worker start so
                # chat_worker closure can see it. (Removed the duplicate
                # definition here.)
                async def _heartbeat_pulse():
                    while True:
                        await _aio.sleep(_chat_heartbeat_every)
                        if _cancel_requested["value"]:
                            return
                        if time.time() - _last_activity >= _chat_heartbeat_every - 2:
                            try:
                                await ws.send_json({
                                    "type": "chat_heartbeat",
                                    "conversation_id": conv_id,
                                    "elapsed_sec": int(time.time() - _last_activity),
                                })
                            except Exception:
                                return
                _hb_task = _aio.create_task(_heartbeat_pulse())
                while True:
                    if _cancel_requested["value"]:
                        await ws.send_json({
                            "type": "chat_complete",
                            "conversation_id": conv_id,
                            "content": full_response,
                            "cancelled": True,
                        })
                        break
                    try:
                        kind, data = await _aio.get_event_loop().run_in_executor(
                            None, lambda: token_queue.get(timeout=_chat_idle_sanity_sec)
                        )
                        _last_activity = time.time()
                    except Exception:
                        await ws.send_json({
                            "type": "chat_error",
                            "conversation_id": conv_id,
                            "error": f"Chat sanity ceiling hit (no activity for {_chat_idle_sanity_sec // 60} min). This is very rare; check backend logs.",
                        })
                        break
                    # v6.4.34: explicit poison-pill from chat_stop handler
                    if kind == "cancel":
                        await ws.send_json({
                            "type": "chat_complete",
                            "conversation_id": conv_id,
                            "content": full_response,
                            "cancelled": True,
                        })
                        break

                    if kind == "token":
                        await ws.send_json({"type": "chat_token", "conversation_id": conv_id, "token": data})
                    elif kind == "reasoning_delta":
                        # v6.4.29: stream reasoning to a separate UI bubble
                        # that the frontend can fold. Existence of these events
                        # is optional — older frontends simply ignore them.
                        await ws.send_json({
                            "type": "chat_reasoning_delta",
                            "conversation_id": conv_id,
                            "text": data,
                        })
                    elif kind == "reasoning_done":
                        await ws.send_json({
                            "type": "chat_reasoning_done",
                            "conversation_id": conv_id,
                        })
                    elif kind == "tool_call_start":
                        # v6.4.32: tool-call trace as foldable UI badge.
                        # Older frontends that don't know this event simply
                        # ignore it — they see no tool trace, which is fine
                        # (legacy behavior dumped 20KB to chat; silence is
                        # strictly an improvement).
                        await ws.send_json({
                            "type": "chat_tool_call_start",
                            "conversation_id": conv_id,
                            **(data if isinstance(data, dict) else {"raw": data}),
                        })
                    elif kind == "tool_call_result":
                        await ws.send_json({
                            "type": "chat_tool_call_result",
                            "conversation_id": conv_id,
                            **(data if isinstance(data, dict) else {"raw": data}),
                        })
                    elif kind == "done":
                        full_response = data
                        _store_chat_message_in_session(session_id, "assistant", full_response)
                        # v6.5 Phase 3: auto-memory persistence (best-effort).
                        try:
                            _maybe_persist_chat_memory(OUTPUT_DIR, chat_text, full_response, chat_mode)
                        except Exception as _mem_err:
                            logger.warning("chat_worker: memory persist failed: %s", _mem_err)
                        await ws.send_json({"type": "chat_complete", "conversation_id": conv_id, "content": full_response})
                        break
                    elif kind == "error":
                        await ws.send_json({"type": "chat_error", "conversation_id": conv_id, "error": data})
                        break
                # v6.4.33: stop heartbeat when the chat turn ends.
                try:
                    _hb_task.cancel()
                except Exception:
                    pass
                # v6.4.34: clear cancel handles so the next turn starts clean.
                _chat_cancel_handles.pop(client_id, None)
                _chat_queue_handles.pop(client_id, None)

            elif msg_type == "chat_stop":
                # v6.4.34/36: real cancellation. The worker thread can't be
                # killed from outside, but we poison-pill the token queue +
                # flip the cancel flag so the consumer exits immediately.
                # Worker thread finishes on its own (writes to a dead queue
                # = harmless). Log so we can see whether the stop actually
                # reached this handler.
                handle = _chat_cancel_handles.get(client_id)
                q = _chat_queue_handles.get(client_id)
                logger.info(
                    "chat_stop received: client_id=%s handle_found=%s queue_found=%s",
                    client_id, handle is not None, q is not None,
                )
                if handle is not None:
                    handle["value"] = True
                if q is not None:
                    try:
                        q.put(("cancel", ""))
                    except Exception as _qe:
                        logger.warning("chat_stop: queue put failed: %s", _qe)
                # ACK the stop so frontend doesn't think it was ignored.
                try:
                    await ws.send_json({
                        "type": "chat_stop_ack",
                        "received": True,
                    })
                except Exception:
                    pass

    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected")
    except Exception as e:
        if _is_benign_ws_disconnect_error(e):
            logger.info(f"Client {client_id} disconnected")
        else:
            logger.error(f"Client {client_id} error: {_sanitize_error(str(e))}")
    finally:
        connected_clients.discard(ws)
        # Cancel any remaining tracked tasks
        preserved = 0
        for t in _active_tasks.pop(client_id, []):
            if t in _detached_tasks:
                preserved += 1
                continue
            _cancel_tracked_task(t)
        # v3.1: Only stop executor if no detached tasks are still running.
        # Previously, executor.stop() was called unconditionally, killing
        # background runs even when cancel_on_disconnect=False was set.
        if not _detached_tasks:
            executor.stop()
        else:
            logger.info(
                "Executor kept alive: %s detached task(s) still running",
                len(_detached_tasks),
            )
        if preserved:
            logger.info(
                "Client %s disconnected; preserved %s detached background task(s)",
                client_id,
                preserved,
            )
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
║     Evermind Backend Server             ║
║     Frontend: http://{host}:{port}/       ║
║     WebSocket: ws://{host}:{port}/ws      ║
║     REST API:  http://{host}:{port}/api   ║
║     Plugins:   {len(PluginRegistry.get_all())} loaded                ║
╚══════════════════════════════════════════╝
    """)

    # Use app object directly (not string "server:app") to avoid double
    # module initialization. reload=False ensures no reloader subprocess.
    uvicorn.run(app, host=host, port=port, log_level="info")
