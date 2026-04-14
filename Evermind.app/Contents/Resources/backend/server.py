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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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
from ai_bridge import AIBridge, MODEL_REGISTRY
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
DESKTOP_BUILD_ID = str(os.getenv("EVERMIND_DESKTOP_BUILD_ID", "") or "").strip()
_backend_lock_handle = None
ORPHANED_RUNNING_RUN_STALE_S = max(15, coerce_int(os.getenv("EVERMIND_ORPHANED_RUNNING_RUN_STALE_S", 45), 45))

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
_detached_tasks: Set[asyncio.Task] = set()  # long-lived tasks that survive sender WS disconnect
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
app.mount("/preview", StaticFiles(directory=str(OUTPUT_DIR), html=True), name="preview")


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


@app.get("/api/models")
async def list_models():
    """List all available AI models."""
    bridge = AIBridge()
    return {"models": bridge.get_available_models()}


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
# Task Board API Endpoints (任务看板 API)
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
    model = data.get("model") or node.get("data", {}).get("model") or node.get("model", "gpt-5.3-codex")

    workspace = os.getenv("WORKSPACE", str(Path.home() / "Desktop"))
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
        patch["image_generation"] = {
            "comfyui_url": str(image_patch.get("comfyui_url", "") or "").strip(),
            "workflow_template": str(image_patch.get("workflow_template", "") or "").strip(),
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

        # Return which models are now available based on configured keys
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


# ─────────────────────────────────────────────
# CLI Backend Endpoints
# ─────────────────────────────────────────────
from cli_backend import get_detector, get_executor, is_cli_mode_enabled, CLI_PROFILES


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
            "zhipu": "zhipu", "doubao": "doubao", "yi": "yi", "minimax": "minimax",
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

    # Realistic prompt (~80 tokens) — exercises real model behavior
    _SPEED_TEST_PROMPT = (
        "You are a helpful coding assistant. A user asks: "
        "'How do I create a basic HTTP server in Python that handles GET and POST requests?' "
        "Reply in 2-3 concise sentences."
    )
    _SPEED_TEST_ITERATIONS = 2  # Balance accuracy vs speed

    def _test_model_sync(name: str) -> tuple:
        """Test a single model with TTFT tracking and multi-iteration."""
        info = MODEL_REGISTRY.get(name, {})
        litellm_id = info.get("litellm_id", name)
        provider = str(info.get("provider") or "").lower()

        # Build request kwargs
        messages = [{"role": "user", "content": _SPEED_TEST_PROMPT}]
        kwargs = {
            "model": litellm_id,
            "messages": messages,
            "max_tokens": 60,
            "timeout": 18,
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

        for _iter in range(_SPEED_TEST_ITERATIONS):
            t0 = _time.monotonic()
            ttft_recorded = False
            try:
                response = _litellm_mod.completion(**kwargs)
                content_parts = []
                for chunk in response:
                    if not ttft_recorded and chunk.choices and chunk.choices[0].delta:
                        c = getattr(chunk.choices[0].delta, "content", None)
                        if c:
                            ttft_samples.append(int((_time.monotonic() - t0) * 1000))
                            ttft_recorded = True
                            content_parts.append(c)
                    elif chunk.choices and chunk.choices[0].delta:
                        c = getattr(chunk.choices[0].delta, "content", None)
                        if c:
                            content_parts.append(c)
                total_ms = int((_time.monotonic() - t0) * 1000)
                reply = "".join(content_parts).strip()
                if reply:
                    total_samples.append(total_ms)
                else:
                    last_error = "empty_reply"
            except Exception as exc:
                last_error = str(exc)[:200]

        if not total_samples:
            return name, {
                "ok": False,
                "latency_ms": 0,
                "ttft_ms": 0,
                "error": last_error or "all_iterations_failed",
                "provider": info.get("provider", ""),
                "iterations": _SPEED_TEST_ITERATIONS,
            }

        # Use best (min) values — represents true capability
        best_total = min(total_samples)
        best_ttft = min(ttft_samples) if ttft_samples else best_total
        median_total = sorted(total_samples)[len(total_samples) // 2]

        return name, {
            "ok": True,
            "latency_ms": best_total,
            "ttft_ms": best_ttft,
            "median_ms": median_total,
            "error": "",
            "provider": info.get("provider", ""),
            "iterations": len(total_samples),
        }

    # Run in batches of 5 — offload to thread pool
    loop = _asyncio.get_event_loop()
    for i in range(0, len(testable), 5):
        batch = testable[i:i+5]
        batch_results = await _asyncio.gather(*[
            loop.run_in_executor(None, _test_model_sync, m)
            for m in batch
        ])
        for name, result in batch_results:
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
        "zhipu": "zhipu", "doubao": "doubao", "yi": "yi", "minimax": "minimax",
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
}
_watchdog_task: Optional[asyncio.Task] = None


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
            for ne_dict in nes.list_node_executions():
                if ne_dict.get("status") != "running":
                    continue
                started = float(ne_dict.get("started_at", 0) or 0)
                if started <= 0:
                    continue
                ne_timeout = _node_timeout_limit_seconds(ne_dict)
                elapsed = now - started
                if elapsed > ne_timeout:
                    ne_id = ne_dict["id"]
                    run_id = str(ne_dict.get("run_id") or "")
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
]


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
    global _watchdog_task
    lock_error = _acquire_backend_runtime_lock()
    if lock_error:
        logger.error(lock_error)
        raise RuntimeError(lock_error)
    _watchdog_task = asyncio.create_task(_timeout_watchdog())
    logger.info("[Watchdog] Timeout watchdog started")
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
    workspace = os.getenv("WORKSPACE", str(Path.home() / "Desktop"))
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
    }

    # Create executor for this client
    ai_bridge = AIBridge(config=config)

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
                if "thinking_depth" in new_config:
                    raw_depth = str(new_config.get("thinking_depth", "deep")).strip().lower()
                    if raw_depth in ("fast", "deep"):
                        config["thinking_depth"] = raw_depth
                        # Propagate to ai_bridge config so both bridge and
                        # orchestrator see the change immediately.
                        if ai_bridge and hasattr(ai_bridge, "config") and isinstance(ai_bridge.config, dict):
                            ai_bridge.config["thinking_depth"] = raw_depth
                        logger.info("thinking_depth updated to '%s' via update_config", raw_depth)
                if "analyst" in new_config:
                    config["analyst"] = _normalize_analyst_settings(
                        new_config.get("analyst")
                    )
                if isinstance(new_config.get("image_generation"), dict):
                    image_cfg = dict(new_config.get("image_generation") or {})
                    config["image_generation"] = {
                        "comfyui_url": str(image_cfg.get("comfyui_url", "") or "").strip(),
                        "workflow_template": str(image_cfg.get("workflow_template", "") or "").strip(),
                    }
                    os.environ["EVERMIND_COMFYUI_URL"] = config["image_generation"]["comfyui_url"]
                    os.environ["EVERMIND_COMFYUI_WORKFLOW_TEMPLATE"] = config["image_generation"]["workflow_template"]
                if isinstance(new_config.get("builder"), dict) and "enable_browser_search" in new_config.get("builder", {}):
                    config["builder_enable_browser"] = coerce_bool(new_config["builder"].get("enable_browser_search"), default=False)
                # v3.0.3: UI language propagation for language-aware reports
                if "ui_language" in new_config:
                    ui_lang = str(new_config.get("ui_language", "en") or "en").strip().lower()[:10]
                    config["ui_language"] = ui_lang if ui_lang in ("en", "zh") else "en"
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
                if difficulty not in ("simple", "standard", "pro"):
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
                if model == "gpt-5.4" and not os.environ.get("OPENAI_API_KEY"):
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
