"""
Evermind Backend — Task & Report Persistence
File-based JSON persistence for Kanban tasks and run reports.
Storage: ~/.evermind/tasks.json, ~/.evermind/reports.json
Uses same locking pattern as settings.py.
"""

import json
import logging
import os
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows fallback

logger = logging.getLogger("evermind.task_store")

STORE_DIR = Path.home() / ".evermind"
TASKS_FILE = STORE_DIR / "tasks.json"
REPORTS_FILE = STORE_DIR / "reports.json"

# ─────────────────────────────────────────────
# Valid task statuses and transitions
# ─────────────────────────────────────────────
VALID_STATUSES = {"backlog", "planned", "executing", "review", "selfcheck", "done"}

VALID_TRANSITIONS: Dict[str, List[str]] = {
    "backlog":   ["planned", "executing"],
    "planned":   ["executing", "backlog"],
    "executing": ["review", "selfcheck", "done", "planned"],
    "review":    ["selfcheck", "executing", "done"],  # reject → back to executing
    "selfcheck": ["done", "executing"],       # fail → back to executing
    "done":      ["backlog"],                 # reopen
}

VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
VALID_MODES = {"standard", "pro", "debug", "review"}


def _truncate_text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _normalize_enum(value: Any, valid: set[str], default: str) -> str:
    return value if isinstance(value, str) and value in valid else default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_progress(value: Any, default: int = 0) -> int:
    return max(0, min(100, _coerce_int(value, default)))


def _normalize_string_list(value: Any, *, limit: int = 100, item_limit: int = 500) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    elif value in (None, ""):
        items = []
    else:
        items = [value]

    normalized: List[str] = []
    seen = set()
    for item in items:
        text = _truncate_text(item, item_limit).strip()
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
        if len(normalized) >= limit:
            break
    return normalized


def _normalize_activity_log(value: Any, *, limit: int = 80, message_limit: int = 600) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        value = [] if value in (None, "") else [value]

    items: List[Dict[str, Any]] = []
    seen = set()
    for raw in value:
        if isinstance(raw, dict):
            ts = _coerce_int(raw.get("ts"), int(time.time() * 1000))
            msg = _truncate_text(raw.get("msg", ""), message_limit).strip()
            item_type = _truncate_text(raw.get("type", "info"), 16).strip().lower() or "info"
        else:
            ts = int(time.time() * 1000)
            msg = _truncate_text(raw, message_limit).strip()
            item_type = "info"
        if not msg:
            continue
        if item_type not in {"info", "error", "warn", "ok", "sys"}:
            item_type = "info"
        key = f"{item_type}:{msg}"
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "ts": max(0, ts),
            "msg": msg,
            "type": item_type,
        })
        if len(items) >= limit:
            break
    items.sort(key=lambda item: _coerce_int(item.get("ts"), 0))
    return items[-limit:]


def _normalize_selfcheck_items(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []

    items: List[Dict[str, Any]] = []
    for raw in value[:50]:
        if not isinstance(raw, dict):
            continue
        name = _truncate_text(raw.get("name", ""), 200).strip()
        detail = _truncate_text(raw.get("detail", ""), 1000).strip()
        if not name and not detail:
            continue
        items.append({
            "name": name,
            "passed": bool(raw.get("passed")),
            "detail": detail,
        })
    return items


# ─────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────
@dataclass
class SelfCheckItem:
    name: str = ""
    passed: bool = False
    detail: str = ""


@dataclass
class TaskRecord:
    id: str = ""
    title: str = ""
    description: str = ""
    status: str = "backlog"
    mode: str = "standard"
    owner: str = ""
    progress: int = 0
    priority: str = "medium"
    created_at: float = 0.0
    updated_at: float = 0.0
    version: int = 0
    session_id: str = ""  # Per-session scoping
    run_ids: List[str] = field(default_factory=list)
    related_files: List[str] = field(default_factory=list)
    latest_summary: str = ""
    latest_risk: str = ""
    review_verdict: str = ""
    review_issues: List[str] = field(default_factory=list)
    selfcheck_items: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "TaskRecord":
        """Create TaskRecord from dict, ignoring unknown fields."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**filtered)


@dataclass
class RunReport:
    id: str = ""
    task_id: str = ""
    run_id: str = ""
    created_at: float = 0.0
    goal: str = ""
    difficulty: str = "standard"
    success: bool = False
    total_subtasks: int = 0
    completed: int = 0
    failed: int = 0
    total_retries: int = 0
    duration_seconds: float = 0.0
    preview_url: str = ""
    subtasks: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "RunReport":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**filtered)


# ─────────────────────────────────────────────
# V1 Canonical Vocabulary — Run / NodeExecution
# ─────────────────────────────────────────────
VALID_RUN_STATUSES = {
    "queued", "running", "waiting_review", "waiting_selfcheck",
    "failed", "done", "cancelled",
}

VALID_RUN_TRANSITIONS: Dict[str, List[str]] = {
    "queued":             ["running", "cancelled"],
    "running":            ["waiting_review", "waiting_selfcheck", "failed", "done", "cancelled"],
    "waiting_review":     ["running", "failed", "cancelled"],
    "waiting_selfcheck":  ["running", "failed", "done", "cancelled"],
    "failed":             ["queued"],       # retry creates new run or re-queues
    "done":               [],               # terminal
    "cancelled":          ["queued"],        # reopen
}

VALID_NODE_STATUSES = {
    "queued", "running", "passed", "failed",
    "blocked", "waiting_approval", "skipped", "cancelled",
}

VALID_NODE_TRANSITIONS: Dict[str, List[str]] = {
    "queued":            ["running", "skipped", "cancelled"],
    "running":           ["passed", "failed", "blocked", "waiting_approval", "cancelled"],
    "passed":            ["running"],  # allow same canonical node to be reopened for orchestrator-driven rework
    "failed":            ["running"],  # local canonical retries reopen the same node execution
    "blocked":           ["running", "skipped", "cancelled"],
    "waiting_approval":  ["running", "passed", "failed", "cancelled"],
    "skipped":           [],
    "cancelled":         [],   # terminal; rerun creates new NodeExecution
}

VALID_TRIGGER_SOURCES = {"openclaw", "openclaw_planner", "ui", "api", "retry", "resume"}
VALID_RUNTIMES = {"local", "openclaw"}
MAX_NODE_RETRY_COUNT = 5

VALID_REVIEW_DECISIONS = {"approve", "reject", "needs_fix", "blocked"}
VALID_VALIDATION_STATUSES = {"passed", "failed", "skipped", "blocked"}

VALID_ARTIFACT_TYPES = {
    "changed_files", "diff_summary", "report", "review_result",
    "test_output", "build_output", "run_summary", "risk_report",
    "deployment_notes", "raw_log", "preview_ref",
    "browser_trace", "browser_capture", "state_snapshot",
    "qa_session_capture", "qa_session_video", "qa_session_log",
}


@dataclass
class RunRecord:
    """V1 canonical Run — one execution attempt of a Task."""
    id: str = ""
    task_id: str = ""
    status: str = "queued"
    trigger_source: str = "ui"
    runtime: str = "local"
    workflow_template_id: str = ""
    current_node_execution_id: str = ""
    active_node_execution_ids: List[str] = field(default_factory=list)  # P4: all currently running NE IDs
    started_at: float = 0.0
    ended_at: float = 0.0
    total_tokens: int = 0
    total_cost: float = 0.0
    summary: str = ""
    risks: List[str] = field(default_factory=list)
    node_execution_ids: List[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    version: int = 0
    timeout_seconds: int = 0  # 0 = use default (3600s for run)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "RunRecord":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**filtered)


@dataclass
class NodeExecutionRecord:
    """V1 canonical NodeExecution — one node's execution within a Run."""
    id: str = ""
    run_id: str = ""
    node_key: str = ""
    node_label: str = ""
    retried_from_id: str = ""
    status: str = "queued"
    assigned_model: str = ""
    assigned_provider: str = ""
    input_summary: str = ""
    output_summary: str = ""
    error_message: str = ""
    retry_count: int = 0
    tokens_used: int = 0
    cost: float = 0.0
    started_at: float = 0.0
    ended_at: float = 0.0
    artifact_ids: List[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    progress: int = 0
    phase: str = ""
    loaded_skills: List[str] = field(default_factory=list)
    activity_log: List[Dict[str, Any]] = field(default_factory=list)
    reference_urls: List[str] = field(default_factory=list)
    version: int = 0
    timeout_seconds: int = 0  # 0 = use default (600s for node)
    depends_on_keys: List[str] = field(default_factory=list)  # P3: node_key deps from template

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "NodeExecutionRecord":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**filtered)


@dataclass
class ArtifactRecord:
    """V1 Artifact — output produced by a node execution."""
    id: str = ""
    run_id: str = ""
    node_execution_id: str = ""
    artifact_type: str = "report"
    title: str = ""
    path: str = ""
    content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "ArtifactRecord":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**filtered)


@dataclass
class ReviewDecisionRecord:
    """V1 ReviewDecision — structured review result for a node."""
    id: str = ""
    run_id: str = ""
    node_execution_id: str = ""
    decision: str = "approve"
    issues: List[str] = field(default_factory=list)
    remaining_risks: List[str] = field(default_factory=list)
    next_action: str = ""
    created_at: float = 0.0

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "ReviewDecisionRecord":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**filtered)


@dataclass
class ValidationResultRecord:
    """V1 ValidationResult — selfcheck/validation checklist for a node."""
    id: str = ""
    run_id: str = ""
    node_execution_id: str = ""
    summary_status: str = "passed"
    checklist: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    created_at: float = 0.0

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "ValidationResultRecord":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**filtered)


# ─────────────────────────────────────────────
# File I/O Helpers
# ─────────────────────────────────────────────
_file_lock = threading.Lock()


def _read_json_file(path: Path) -> Any:
    """Read and parse a JSON file, return empty list/dict on failure."""
    try:
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            return json.loads(raw) if raw.strip() else []
    except Exception as e:
        logger.warning(f"Failed to read {path}: {e}")
    return []


def _write_json_file(path: Path, data: Any) -> bool:
    """Write data to JSON file atomically (write to temp, then rename)."""
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        # Atomic write: serialize to a temp file in the same directory,
        # then os.replace() which is atomic on POSIX.
        import tempfile as _tempfile
        fd, tmp_path = _tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=f".{path.stem}_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(path))
        except BaseException:
            # Clean up temp file on any error
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except PermissionError:
            pass
        return True
    except Exception as e:
        logger.error(f"Failed to write {path}: {e}")
        return False


# ─────────────────────────────────────────────
# Task Store
# ─────────────────────────────────────────────
class TaskStore:
    """Thread-safe file-backed task store."""

    def __init__(self):
        self._tasks: Dict[str, TaskRecord] = {}
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            self.load()

    def load(self):
        """Load tasks from disk."""
        with _file_lock:
            raw = _read_json_file(TASKS_FILE)
            self._tasks = {}
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("id"):
                        self._tasks[item["id"]] = TaskRecord.from_dict(item)
            self._loaded = True
            logger.info(f"Loaded {len(self._tasks)} tasks from {TASKS_FILE}")

    def save(self) -> bool:
        """Persist all tasks to disk."""
        with _file_lock:
            data = [t.to_dict() for t in self._tasks.values()]
            ok = _write_json_file(TASKS_FILE, data)
            if ok:
                logger.info(f"Saved {len(data)} tasks to {TASKS_FILE}")
            return ok

    def list_tasks(self, session_id: Optional[str] = None) -> List[Dict]:
        """Return tasks as dicts, optionally filtered by session_id, sorted by updated_at desc."""
        self._ensure_loaded()
        tasks = list(self._tasks.values())
        if session_id:
            tasks = [t for t in tasks if t.session_id == session_id]
        tasks = sorted(tasks, key=lambda t: t.updated_at, reverse=True)
        return [t.to_dict() for t in tasks]

    def get_task(self, task_id: str) -> Optional[Dict]:
        """Get a single task by ID."""
        self._ensure_loaded()
        task = self._tasks.get(task_id)
        return task.to_dict() if task else None

    def create_task(self, data: Dict) -> Dict:
        """Create a new task. Returns the created task dict."""
        self._ensure_loaded()
        now = time.time()
        task = TaskRecord(
            id=data.get("id") or str(uuid.uuid4()),
            title=_truncate_text(data.get("title", "Untitled Task"), 200),
            description=_truncate_text(data.get("description", ""), 2000),
            status=_normalize_enum(data.get("status"), VALID_STATUSES, "backlog"),
            mode=_normalize_enum(data.get("mode"), VALID_MODES, "standard"),
            session_id=_truncate_text(data.get("session_id", ""), 120),
            owner=_truncate_text(data.get("owner", ""), 100),
            progress=_clamp_progress(data.get("progress", 0)),
            priority=_normalize_enum(data.get("priority"), VALID_PRIORITIES, "medium"),
            created_at=now,
            updated_at=now,
            run_ids=_normalize_string_list(data.get("run_ids"), limit=200, item_limit=120),
            related_files=_normalize_string_list(data.get("related_files"), limit=200, item_limit=2000),
            latest_summary=_truncate_text(data.get("latest_summary", ""), 1000),
            latest_risk=_truncate_text(data.get("latest_risk", ""), 500),
            review_verdict=_truncate_text(data.get("review_verdict", ""), 40).lower(),
            review_issues=_normalize_string_list(data.get("review_issues"), limit=50, item_limit=500),
            selfcheck_items=_normalize_selfcheck_items(data.get("selfcheck_items")),
        )
        self._tasks[task.id] = task
        self.save()
        return task.to_dict()

    def update_task(self, task_id: str, data: Dict) -> Optional[Dict]:
        """Update task fields. Returns updated task or None if not found."""
        self._ensure_loaded()
        task = self._tasks.get(task_id)
        if not task:
            return None

        if "title" in data:
            task.title = _truncate_text(data["title"], 200)
        if "description" in data:
            task.description = _truncate_text(data["description"], 2000)
        if "mode" in data:
            task.mode = _normalize_enum(data["mode"], VALID_MODES, task.mode)
        if "owner" in data:
            task.owner = _truncate_text(data["owner"], 100)
        if "progress" in data:
            task.progress = _clamp_progress(data["progress"], task.progress)
        if "priority" in data:
            task.priority = _normalize_enum(data["priority"], VALID_PRIORITIES, task.priority)
        if "related_files" in data:
            task.related_files = _normalize_string_list(data["related_files"], limit=200, item_limit=2000)
        if "latest_summary" in data:
            task.latest_summary = _truncate_text(data["latest_summary"], 1000)
        if "latest_risk" in data:
            task.latest_risk = _truncate_text(data["latest_risk"], 500)
        if "review_verdict" in data:
            task.review_verdict = _truncate_text(data["review_verdict"], 40).lower()
        if "review_issues" in data:
            task.review_issues = _normalize_string_list(data["review_issues"], limit=50, item_limit=500)
        if "selfcheck_items" in data:
            task.selfcheck_items = _normalize_selfcheck_items(data["selfcheck_items"])

        # Append run_ids if provided (don't replace)
        if "run_ids" in data:
            existing = set(task.run_ids)
            for rid in _normalize_string_list(data["run_ids"], limit=200, item_limit=120):
                if rid not in existing:
                    task.run_ids.append(rid)
                    existing.add(rid)

        task.updated_at = time.time()
        task.version += 1
        self.save()
        return task.to_dict()

    def transition_task(self, task_id: str, new_status: str) -> Dict:
        """
        Transition task to new status with validation.
        Returns {success, task, error}.
        """
        self._ensure_loaded()
        task = self._tasks.get(task_id)
        if not task:
            return {"success": False, "error": f"Task {task_id} not found"}

        if new_status not in VALID_STATUSES:
            return {"success": False, "error": f"Invalid status: {new_status}"}

        allowed = VALID_TRANSITIONS.get(task.status, [])
        if new_status not in allowed:
            return {
                "success": False,
                "error": f"Cannot transition from '{task.status}' to '{new_status}'. Allowed: {allowed}",
            }

        # Update progress based on status
        progress_map = {
            "backlog": 0, "planned": 10, "executing": 30,
            "review": 60, "selfcheck": 80, "done": 100,
        }

        task.status = new_status
        task.progress = progress_map.get(new_status, task.progress)
        task.updated_at = time.time()
        task.version += 1
        self.save()
        return {"success": True, "task": task.to_dict()}

    def delete_task(self, task_id: str) -> bool:
        """Delete a task by ID."""
        self._ensure_loaded()
        if task_id in self._tasks:
            del self._tasks[task_id]
            self.save()
            return True
        return False

    def delete_tasks_by_session(self, session_id: str) -> int:
        """Delete all tasks belonging to a session. Returns count deleted."""
        self._ensure_loaded()
        to_delete = [tid for tid, t in self._tasks.items() if t.session_id == session_id]
        if not to_delete:
            return 0
        for tid in to_delete:
            del self._tasks[tid]
        self.save()
        logger.info(f"Deleted {len(to_delete)} tasks for session {session_id}")
        return len(to_delete)

    def link_run(self, task_id: str, run_id: str, summary: str = "", risk: str = "", files: Optional[List[str]] = None) -> Optional[Dict]:
        """Link a run report to a task and update summary/risk/files."""
        self._ensure_loaded()
        task = self._tasks.get(task_id)
        if not task:
            return None

        changed = False

        normalized_run_id = _truncate_text(run_id, 120).strip()
        if normalized_run_id and normalized_run_id not in task.run_ids:
            task.run_ids.append(normalized_run_id)
            changed = True
        if summary:
            normalized_summary = _truncate_text(summary, 1000)
            if normalized_summary != task.latest_summary:
                task.latest_summary = normalized_summary
                changed = True
        if risk:
            normalized_risk = _truncate_text(risk, 500)
            if normalized_risk != task.latest_risk:
                task.latest_risk = normalized_risk
                changed = True
        if files:
            existing = set(task.related_files)
            for f in _normalize_string_list(files, limit=200, item_limit=2000):
                if f not in existing:
                    task.related_files.append(f)
                    existing.add(f)
                    changed = True
        if changed:
            task.updated_at = time.time()
            task.version += 1
            self.save()
        return task.to_dict()

    # ─────────────────────────────────────────────
    # P0-2: Unified Task Projection from Run Events
    # ─────────────────────────────────────────────

    def project_task_from_run(
        self,
        task_id: str,
        *,
        run_status: Optional[str] = None,
        review_verdict: Optional[str] = None,
        review_issues: Optional[List[str]] = None,
        remaining_risks: Optional[List[str]] = None,
        selfcheck_items: Optional[List[Dict[str, Any]]] = None,
        summary: Optional[str] = None,
        run_id: str = "",
    ) -> Optional[Dict]:
        """
        Unified projection: apply run lifecycle events onto the Task record.

        Called by OpenClaw connector handlers instead of ad-hoc inline logic.
        Handles:
          - run_status → task status transition (following state machine)
          - review_verdict/issues → task.review_verdict / review_issues
          - selfcheck_items → task.selfcheck_items
          - summary/risks → task.latest_summary / latest_risk
          - run_id → append to task.run_ids

        Returns the updated task dict, or None if task not found.
        Safe: silently skips invalid state transitions.
        """
        self._ensure_loaded()
        task = self._tasks.get(task_id)
        if not task:
            logger.warning(f"project_task_from_run: task {task_id} not found")
            return None

        changed = False

        # 1. Link run_id if provided
        if run_id and run_id not in task.run_ids:
            task.run_ids.append(run_id)
            changed = True

        # 2. Project run_status → task status
        if run_status:
            # Mapping: run terminal status → target task status
            # waiting_review → review
            # waiting_selfcheck → selfcheck
            # done (success) → done
            # failed → planned (run stopped; task is ready for another attempt)
            # cancelled → backlog (manual/system stop)
            task_status_map = {
                "waiting_review": "review",
                "waiting_selfcheck": "selfcheck",
                "done": "done",
                "failed": "planned",
                "cancelled": "backlog",
            }
            target_task_status = task_status_map.get(run_status, "")
            if target_task_status and target_task_status in VALID_STATUSES:
                allowed = VALID_TRANSITIONS.get(task.status, [])
                if target_task_status == task.status:
                    pass
                elif run_status == "failed":
                    task.status = target_task_status
                    task.progress = 10
                    changed = True
                elif run_status == "cancelled" and task.status == "executing" and target_task_status == "backlog":
                    task.status = "backlog"
                    task.progress = 0
                    changed = True
                elif target_task_status in allowed:
                    progress_map = {
                        "backlog": 0, "planned": 10, "executing": 30,
                        "review": 60, "selfcheck": 80, "done": 100,
                    }
                    task.status = target_task_status
                    task.progress = progress_map.get(target_task_status, task.progress)
                    changed = True
                else:
                    logger.debug(
                        f"project_task_from_run: skipping transition "
                        f"'{task.status}' → '{target_task_status}' (not allowed)"
                    )

        # 3. Project review verdict + issues
        if review_verdict is not None:
            normalized = _truncate_text(review_verdict, 40).lower()
            if normalized != task.review_verdict:
                task.review_verdict = normalized
                changed = True
        if review_issues is not None:
            normalized_issues = _normalize_string_list(review_issues, limit=50, item_limit=500)
            if normalized_issues != task.review_issues:
                task.review_issues = normalized_issues
                changed = True
        if remaining_risks is not None:
            next_risk = _truncate_text(remaining_risks[0], 500) if remaining_risks else ""
            if next_risk != task.latest_risk:
                task.latest_risk = next_risk
                changed = True

        # 4. Project selfcheck items
        if selfcheck_items is not None:
            normalized_items = _normalize_selfcheck_items(selfcheck_items)
            if normalized_items != task.selfcheck_items:
                task.selfcheck_items = normalized_items
                changed = True

        # 5. Project summary
        if summary is not None:
            normalized_summary = _truncate_text(summary, 1000)
            if normalized_summary != task.latest_summary:
                task.latest_summary = normalized_summary
                changed = True

        if changed:
            task.updated_at = time.time()
            task.version += 1
            self.save()

        return task.to_dict()


# ─────────────────────────────────────────────
# Report Store
# ─────────────────────────────────────────────
class ReportStore:
    """Thread-safe file-backed report store."""

    MAX_REPORTS = 200

    def __init__(self):
        self._reports: Dict[str, RunReport] = {}
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            self.load()

    def load(self):
        with _file_lock:
            raw = _read_json_file(REPORTS_FILE)
            self._reports = {}
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("id"):
                        self._reports[item["id"]] = RunReport.from_dict(item)
            self._loaded = True
            logger.info(f"Loaded {len(self._reports)} reports from {REPORTS_FILE}")

    def save(self) -> bool:
        with _file_lock:
            # Keep only most recent MAX_REPORTS
            sorted_reports = sorted(
                self._reports.values(),
                key=lambda r: r.created_at,
                reverse=True,
            )[:self.MAX_REPORTS]
            self._reports = {r.id: r for r in sorted_reports}
            data = [r.to_dict() for r in sorted_reports]
            return _write_json_file(REPORTS_FILE, data)

    def list_reports(self, task_id: Optional[str] = None) -> List[Dict]:
        self._ensure_loaded()
        reports = sorted(self._reports.values(), key=lambda r: r.created_at, reverse=True)
        if task_id:
            reports = [r for r in reports if r.task_id == task_id]
        return [r.to_dict() for r in reports]

    def get_report(self, report_id: str) -> Optional[Dict]:
        self._ensure_loaded()
        report = self._reports.get(report_id)
        return report.to_dict() if report else None

    def save_report(self, data: Dict) -> Dict:
        """Save or update a run report. Returns the saved report dict."""
        self._ensure_loaded()
        report = RunReport.from_dict(data)
        if not report.id:
            report.id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        if not report.created_at:
            report.created_at = time.time()
        self._reports[report.id] = report
        self.save()
        return report.to_dict()

    def delete_report(self, report_id: str) -> bool:
        self._ensure_loaded()
        if report_id in self._reports:
            del self._reports[report_id]
            self.save()
            return True
        return False


# ─────────────────────────────────────────────
# Run Store
# ─────────────────────────────────────────────
RUNS_FILE = STORE_DIR / "runs.json"
NODE_EXECUTIONS_FILE = STORE_DIR / "node_executions.json"
ARTIFACTS_FILE = STORE_DIR / "artifacts.json"


class RunStore:
    """Thread-safe file-backed Run lifecycle store."""

    MAX_RUNS = 500

    def __init__(self):
        self._runs: Dict[str, RunRecord] = {}
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            self.load()

    def load(self):
        with _file_lock:
            raw = _read_json_file(RUNS_FILE)
            self._runs = {}
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("id"):
                        self._runs[item["id"]] = RunRecord.from_dict(item)
            self._loaded = True
            logger.info(f"Loaded {len(self._runs)} runs from {RUNS_FILE}")

    def save(self) -> bool:
        with _file_lock:
            sorted_runs = sorted(
                self._runs.values(), key=lambda r: r.created_at, reverse=True,
            )[:self.MAX_RUNS]
            self._runs = {r.id: r for r in sorted_runs}
            data = [r.to_dict() for r in sorted_runs]
            return _write_json_file(RUNS_FILE, data)

    def create_run(self, data: Dict) -> Dict:
        """Create a new Run for a Task."""
        self._ensure_loaded()
        now = time.time()
        run = RunRecord(
            id=data.get("id") or f"run_{uuid.uuid4().hex[:12]}",
            task_id=_truncate_text(data.get("task_id", ""), 120),
            status=_normalize_enum(data.get("status"), VALID_RUN_STATUSES, "queued"),
            trigger_source=_normalize_enum(data.get("trigger_source", "ui"), VALID_TRIGGER_SOURCES, "ui"),
            runtime=_normalize_enum(data.get("runtime", "local"), VALID_RUNTIMES, "local"),
            workflow_template_id=_truncate_text(data.get("workflow_template_id", ""), 120),
            current_node_execution_id="",
            started_at=0.0,
            ended_at=0.0,
            total_tokens=_coerce_int(data.get("total_tokens", 0)),
            total_cost=float(data.get("total_cost", 0.0) or 0.0),
            summary=_truncate_text(data.get("summary", ""), 2000),
            risks=_normalize_string_list(data.get("risks"), limit=50, item_limit=500),
            node_execution_ids=[],
            created_at=now,
            updated_at=now,
            version=1,
            timeout_seconds=max(0, _coerce_int(data.get("timeout_seconds", 0))),
        )
        self._runs[run.id] = run
        self.save()
        return run.to_dict()

    def get_run(self, run_id: str) -> Optional[Dict]:
        self._ensure_loaded()
        run = self._runs.get(run_id)
        return run.to_dict() if run else None

    def list_runs(self, task_id: Optional[str] = None) -> List[Dict]:
        self._ensure_loaded()
        runs = sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)
        if task_id:
            runs = [r for r in runs if r.task_id == task_id]
        return [r.to_dict() for r in runs]

    def transition_run(self, run_id: str, new_status: str) -> Dict:
        """Transition run status with validation."""
        self._ensure_loaded()
        run = self._runs.get(run_id)
        if not run:
            return {"success": False, "error": f"Run {run_id} not found"}
        if new_status not in VALID_RUN_STATUSES:
            return {"success": False, "error": f"Invalid run status: {new_status}"}
        allowed = VALID_RUN_TRANSITIONS.get(run.status, [])
        if new_status not in allowed:
            return {
                "success": False,
                "error": f"Cannot transition run from '{run.status}' to '{new_status}'. Allowed: {allowed}",
            }
        now = time.time()
        run.status = new_status
        if new_status == "queued":
            # Re-queue means the run is pending again, so clear prior terminal timestamps.
            run.started_at = 0.0
            run.ended_at = 0.0
        if new_status == "running" and run.started_at == 0.0:
            run.started_at = now
            run.ended_at = 0.0
        if new_status in ("done", "failed", "cancelled"):
            run.ended_at = now
        run.updated_at = now
        run.version += 1
        self.save()
        return {"success": True, "run": run.to_dict()}

    def update_run(self, run_id: str, data: Dict) -> Optional[Dict]:
        """Update mutable run fields (not status — use transition_run)."""
        self._ensure_loaded()
        run = self._runs.get(run_id)
        if not run:
            return None
        if "current_node_execution_id" in data:
            run.current_node_execution_id = _truncate_text(data["current_node_execution_id"], 120)
        if "active_node_execution_ids" in data:
            run.active_node_execution_ids = _normalize_string_list(
                data["active_node_execution_ids"], limit=50, item_limit=120
            )
        if "total_tokens" in data:
            run.total_tokens = _coerce_int(data["total_tokens"])
        if "total_cost" in data:
            run.total_cost = float(data.get("total_cost", 0.0) or 0.0)
        if "summary" in data:
            run.summary = _truncate_text(data["summary"], 2000)
        if "risks" in data:
            run.risks = _normalize_string_list(data["risks"], limit=50, item_limit=500)
        if "timeout_seconds" in data:
            run.timeout_seconds = max(0, _coerce_int(data["timeout_seconds"]))
        if "started_at" in data:
            run.started_at = max(0.0, _coerce_float(data["started_at"], run.started_at))
        if "ended_at" in data:
            run.ended_at = max(0.0, _coerce_float(data["ended_at"], run.ended_at))
        if "created_at" in data:
            run.created_at = max(0.0, _coerce_float(data["created_at"], run.created_at))
        # Append node_execution_ids
        if "node_execution_ids" in data:
            existing = set(run.node_execution_ids)
            for nid in _normalize_string_list(data["node_execution_ids"], limit=100, item_limit=120):
                if nid not in existing:
                    run.node_execution_ids.append(nid)
                    existing.add(nid)
        run.updated_at = max(0.0, _coerce_float(data.get("updated_at"), time.time()))
        run.version += 1
        self.save()
        return run.to_dict()

    def delete_run(self, run_id: str) -> bool:
        self._ensure_loaded()
        if run_id in self._runs:
            del self._runs[run_id]
            self.save()
            return True
        return False


class NodeExecutionStore:
    """Thread-safe file-backed NodeExecution store."""

    MAX_ITEMS = 2000

    def __init__(self):
        self._nodes: Dict[str, NodeExecutionRecord] = {}
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            self.load()

    def load(self):
        with _file_lock:
            raw = _read_json_file(NODE_EXECUTIONS_FILE)
            self._nodes = {}
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("id"):
                        self._nodes[item["id"]] = NodeExecutionRecord.from_dict(item)
            self._loaded = True
            logger.info(f"Loaded {len(self._nodes)} node executions from {NODE_EXECUTIONS_FILE}")

    def save(self) -> bool:
        with _file_lock:
            sorted_items = sorted(
                self._nodes.values(), key=lambda n: n.created_at, reverse=True,
            )[:self.MAX_ITEMS]
            self._nodes = {n.id: n for n in sorted_items}
            data = [n.to_dict() for n in sorted_items]
            return _write_json_file(NODE_EXECUTIONS_FILE, data)

    def create_node_execution(self, data: Dict) -> Dict:
        self._ensure_loaded()
        now = time.time()
        node = NodeExecutionRecord(
            id=data.get("id") or f"nodeexec_{uuid.uuid4().hex[:12]}",
            run_id=_truncate_text(data.get("run_id", ""), 120),
            node_key=_truncate_text(data.get("node_key", ""), 100),
            node_label=_truncate_text(data.get("node_label", ""), 200),
            retried_from_id=_truncate_text(data.get("retried_from_id", ""), 120),
            status=_normalize_enum(data.get("status"), VALID_NODE_STATUSES, "queued"),
            assigned_model=_truncate_text(data.get("assigned_model", ""), 100),
            assigned_provider=_truncate_text(data.get("assigned_provider", ""), 100),
            input_summary=_truncate_text(data.get("input_summary", ""), 2000),
            output_summary="",
            error_message="",
            retry_count=_coerce_int(data.get("retry_count", 0)),
            tokens_used=0,
            cost=0.0,
            started_at=0.0,
            ended_at=0.0,
            artifact_ids=[],
            created_at=now,
            updated_at=now,
            progress=_clamp_progress(data.get("progress", 0)),
            phase=_truncate_text(data.get("phase", ""), 120),
            loaded_skills=_normalize_string_list(data.get("loaded_skills"), limit=20, item_limit=80),
            activity_log=_normalize_activity_log(data.get("activity_log"), limit=80),
            reference_urls=_normalize_string_list(data.get("reference_urls"), limit=20, item_limit=500),
            version=1,
            timeout_seconds=max(0, _coerce_int(data.get("timeout_seconds", 0))),
            depends_on_keys=_normalize_string_list(data.get("depends_on_keys"), limit=20, item_limit=100),
        )
        self._nodes[node.id] = node
        self.save()
        return node.to_dict()

    def get_node_execution(self, node_id: str) -> Optional[Dict]:
        self._ensure_loaded()
        node = self._nodes.get(node_id)
        return node.to_dict() if node else None

    def list_node_executions(self, run_id: Optional[str] = None) -> List[Dict]:
        self._ensure_loaded()
        items = sorted(self._nodes.values(), key=lambda n: n.created_at)
        if run_id:
            items = [n for n in items if n.run_id == run_id]
        return [n.to_dict() for n in items]

    def transition_node(self, node_id: str, new_status: str) -> Dict:
        """Transition node execution status with validation."""
        self._ensure_loaded()
        node = self._nodes.get(node_id)
        if not node:
            return {"success": False, "error": f"NodeExecution {node_id} not found"}
        if new_status not in VALID_NODE_STATUSES:
            return {"success": False, "error": f"Invalid node status: {new_status}"}
        allowed = VALID_NODE_TRANSITIONS.get(node.status, [])
        if new_status not in allowed:
            return {
                "success": False,
                "error": f"Cannot transition node from '{node.status}' to '{new_status}'. Allowed: {allowed}",
            }
        now = time.time()
        previous_status = node.status
        node.status = new_status
        if new_status == "running":
            if previous_status in ("passed", "failed"):
                node.retry_count = min(MAX_NODE_RETRY_COUNT, max(0, node.retry_count) + 1)
            if previous_status in ("passed", "failed", "skipped", "cancelled"):
                node.started_at = now
                node.ended_at = 0.0
                node.progress = 5
                node.error_message = ""
            elif node.started_at == 0.0:
                node.started_at = now
                node.ended_at = 0.0
                node.progress = max(node.progress, 5)
        if new_status in ("passed", "failed", "skipped", "cancelled"):
            node.ended_at = now
            node.progress = 100
        node.updated_at = now
        node.version += 1
        self.save()
        return {"success": True, "node_execution": node.to_dict()}

    def update_node_execution(self, node_id: str, data: Dict) -> Optional[Dict]:
        """Update mutable node fields."""
        self._ensure_loaded()
        node = self._nodes.get(node_id)
        if not node:
            return None
        if "assigned_model" in data:
            node.assigned_model = _truncate_text(data["assigned_model"], 100)
        if "assigned_provider" in data:
            node.assigned_provider = _truncate_text(data["assigned_provider"], 100)
        if "input_summary" in data:
            node.input_summary = _truncate_text(data["input_summary"], 2000)
        if "output_summary" in data:
            node.output_summary = _truncate_text(data["output_summary"], 2000)
        if "error_message" in data:
            node.error_message = _truncate_text(data["error_message"], 2000)
        if "tokens_used" in data:
            node.tokens_used = _coerce_int(data["tokens_used"])
        if "cost" in data:
            node.cost = float(data.get("cost", 0.0) or 0.0)
        if "progress" in data:
            node.progress = _clamp_progress(data["progress"], node.progress)
        if "phase" in data:
            node.phase = _truncate_text(data["phase"], 120)
        if "loaded_skills" in data:
            node.loaded_skills = _normalize_string_list(data["loaded_skills"], limit=20, item_limit=80)
        if "activity_log" in data:
            node.activity_log = _normalize_activity_log(data["activity_log"], limit=80)
        if "activity_log_append" in data:
            merged_log = list(node.activity_log) + _normalize_activity_log(data["activity_log_append"], limit=40)
            node.activity_log = _normalize_activity_log(merged_log, limit=80)
        if "reference_urls" in data:
            node.reference_urls = _normalize_string_list(data["reference_urls"], limit=20, item_limit=500)
        if "reference_urls_append" in data:
            node.reference_urls = _normalize_string_list(
                list(node.reference_urls) + _normalize_string_list(data["reference_urls_append"], limit=20, item_limit=500),
                limit=20,
                item_limit=500,
            )
        if "retry_count" in data:
            node.retry_count = _coerce_int(data["retry_count"])
        if "retried_from_id" in data:
            node.retried_from_id = _truncate_text(data["retried_from_id"], 120)
        if "timeout_seconds" in data:
            node.timeout_seconds = max(0, _coerce_int(data["timeout_seconds"]))
        if "started_at" in data:
            node.started_at = max(0.0, _coerce_float(data["started_at"], node.started_at))
        if "ended_at" in data:
            node.ended_at = max(0.0, _coerce_float(data["ended_at"], node.ended_at))
        if "created_at" in data:
            node.created_at = max(0.0, _coerce_float(data["created_at"], node.created_at))
        if "artifact_ids" in data:
            existing = set(node.artifact_ids)
            for aid in _normalize_string_list(data["artifact_ids"], limit=100, item_limit=120):
                if aid not in existing:
                    node.artifact_ids.append(aid)
                    existing.add(aid)
        if "depends_on_keys" in data:
            node.depends_on_keys = _normalize_string_list(data["depends_on_keys"], limit=20, item_limit=100)
        node.updated_at = max(0.0, _coerce_float(data.get("updated_at"), time.time()))
        node.version += 1
        self.save()
        return node.to_dict()

    def cancel_run_nodes(self, run_id: str) -> int:
        """Cancel all non-terminal nodes for a run. Returns count cancelled."""
        self._ensure_loaded()
        terminal = {"passed", "failed", "skipped", "cancelled"}
        now = time.time()
        count = 0
        for node in self._nodes.values():
            if node.run_id == run_id and node.status not in terminal:
                allowed = VALID_NODE_TRANSITIONS.get(node.status, [])
                if "cancelled" in allowed:
                    node.status = "cancelled"
                    node.ended_at = now
                    node.updated_at = now
                    node.version += 1
                    count += 1
        if count > 0:
            self.save()
        return count


class ArtifactStore:
    """Thread-safe file-backed Artifact store."""

    MAX_ITEMS = 1000

    def __init__(self):
        self._artifacts: Dict[str, ArtifactRecord] = {}
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            self.load()

    def load(self):
        with _file_lock:
            raw = _read_json_file(ARTIFACTS_FILE)
            self._artifacts = {}
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("id"):
                        self._artifacts[item["id"]] = ArtifactRecord.from_dict(item)
            self._loaded = True

    def save(self) -> bool:
        with _file_lock:
            sorted_items = sorted(
                self._artifacts.values(), key=lambda a: a.created_at, reverse=True,
            )[:self.MAX_ITEMS]
            self._artifacts = {a.id: a for a in sorted_items}
            data = [a.to_dict() for a in sorted_items]
            return _write_json_file(ARTIFACTS_FILE, data)

    def save_artifact(self, data: Dict) -> Dict:
        self._ensure_loaded()
        now = time.time()
        artifact = ArtifactRecord(
            id=data.get("id") or f"artifact_{uuid.uuid4().hex[:12]}",
            run_id=_truncate_text(data.get("run_id", ""), 120),
            node_execution_id=_truncate_text(data.get("node_execution_id", ""), 120),
            artifact_type=_normalize_enum(data.get("artifact_type", data.get("type", "")), VALID_ARTIFACT_TYPES, "report"),
            title=_truncate_text(data.get("title", ""), 200),
            path=_truncate_text(data.get("path", ""), 2000),
            content=_truncate_text(data.get("content", ""), 50000),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
            created_at=float(data.get("created_at", now) or now),
        )
        self._artifacts[artifact.id] = artifact
        self.save()
        return artifact.to_dict()

    def get_artifact(self, artifact_id: str) -> Optional[Dict]:
        self._ensure_loaded()
        a = self._artifacts.get(artifact_id)
        return a.to_dict() if a else None

    def list_artifacts(self, run_id: Optional[str] = None, node_execution_id: Optional[str] = None) -> List[Dict]:
        self._ensure_loaded()
        items = sorted(self._artifacts.values(), key=lambda a: a.created_at, reverse=True)
        if run_id:
            items = [a for a in items if a.run_id == run_id]
        if node_execution_id:
            items = [a for a in items if a.node_execution_id == node_execution_id]
        return [a.to_dict() for a in items]

    def delete_artifact(self, artifact_id: str) -> bool:
        self._ensure_loaded()
        if artifact_id in self._artifacts:
            del self._artifacts[artifact_id]
            self.save()
            return True
        return False


# ─────────────────────────────────────────────
# Global Singletons
# ─────────────────────────────────────────────
_task_store: Optional[TaskStore] = None
_report_store: Optional[ReportStore] = None
_run_store: Optional[RunStore] = None
_node_execution_store: Optional[NodeExecutionStore] = None
_artifact_store: Optional[ArtifactStore] = None


def get_task_store() -> TaskStore:
    global _task_store
    if _task_store is None:
        _task_store = TaskStore()
    return _task_store


def get_report_store() -> ReportStore:
    global _report_store
    if _report_store is None:
        _report_store = ReportStore()
    return _report_store


def get_run_store() -> RunStore:
    global _run_store
    if _run_store is None:
        _run_store = RunStore()
    return _run_store


def get_node_execution_store() -> NodeExecutionStore:
    global _node_execution_store
    if _node_execution_store is None:
        _node_execution_store = NodeExecutionStore()
    return _node_execution_store


def get_artifact_store() -> ArtifactStore:
    global _artifact_store
    if _artifact_store is None:
        _artifact_store = ArtifactStore()
    return _artifact_store
