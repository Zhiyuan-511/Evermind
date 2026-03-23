"""
Evermind Backend — Autonomous Orchestrator
The brain of the multi-agent system.

Flow: User Goal → Plan → Distribute → Execute → Test → Retry/Complete

Inspired by:
  - Dify workflow engine (visual DAG execution)
  - CrewAI (role-based agent collaboration)
  - OpenAI Agents SDK (handoffs + guardrails)
  - Cursor/Antigravity (code → test → fix loop)
"""

import asyncio
import json
from html_postprocess import postprocess_html
import logging
import os
import re
import shutil
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

from plugins.base import PluginRegistry, get_default_plugins_for_node, is_image_generation_available
import task_classifier
from task_store import get_node_execution_store, get_run_store
from preview_validation import (
    build_preview_url_for_file,
    is_partial_html_artifact,
    latest_preview_artifact,
    validate_html_file,
    validate_preview,
)
from repo_map import build_repo_context

# Output directory for generated files
OUTPUT_DIR = Path(os.getenv("EVERMIND_OUTPUT_DIR", "/tmp/evermind_output"))
PREVIEW_PORT = os.getenv("PORT", "8765")
MIN_COMMERCIAL_HTML_BYTES = int(os.getenv("EVERMIND_MIN_HTML_BYTES", "1200"))
MIN_COMMERCIAL_CSS_RULES = int(os.getenv("EVERMIND_MIN_CSS_RULES", "10"))
MIN_SEMANTIC_BLOCKS = int(os.getenv("EVERMIND_MIN_SEMANTIC_BLOCKS", "4"))
MAX_EMOJI_GLYPHS = int(os.getenv("EVERMIND_MAX_EMOJI_GLYPHS", "0"))
MAX_DEP_CONTEXT_CHARS = int(os.getenv("EVERMIND_DEP_CONTEXT_CHARS", "900"))

# Regex to match ```lang\n...``` code blocks
_CODE_BLOCK_RE = re.compile(r'```(\w+)?\n(.*?)```', re.DOTALL)
_URL_RE = re.compile(r'https?://[^\s<>"\')]+', re.IGNORECASE)

# Language → filename mapping for code extraction
_LANG_FILENAME = {
    'python': 'main.py', 'py': 'main.py',
    'javascript': 'index.js', 'js': 'index.js',
    'typescript': 'index.ts', 'ts': 'index.ts',
    'html': 'index.html',
    'css': 'styles.css',
    'java': 'Main.java',
    'go': 'main.go',
    'rust': 'main.rs',
    'c': 'main.c', 'cpp': 'main.cpp',
    'shell': 'script.sh', 'bash': 'script.sh', 'sh': 'script.sh',
    'json': 'data.json',
    'yaml': 'config.yaml', 'yml': 'config.yaml',
    'sql': 'schema.sql',
    'markdown': 'README.md', 'md': 'README.md',
}

logger = logging.getLogger("evermind.orchestrator")


class TaskStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
    TESTING = "testing"
    FAILED = "failed"
    RETRYING = "retrying"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class SubTask:
    id: str
    agent_type: str  # builder, tester, reviewer, deployer, etc.
    description: str
    depends_on: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    output: str = ""
    error: str = ""
    retries: int = 0
    max_retries: int = 3
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0
    started_at: float = 0
    ended_at: float = 0
    last_partial_output: str = ""


@dataclass
class Plan:
    goal: str
    subtasks: List[SubTask] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    current_phase: int = 0
    total_retries: int = 0
    max_total_retries: int = 10
    created_at: float = field(default_factory=time.time)
    difficulty: str = "standard"


class Orchestrator:
    """
    Autonomous multi-agent orchestrator.

    User sends a goal → Orchestrator:
    1. PLAN: AI breaks goal into subtasks with agent assignments
    2. DISTRIBUTE: Resolve dependencies, find ready subtasks
    3. EXECUTE: Run subtasks through appropriate agents (Builder, Tester, etc.)
    4. TEST: Run tester agent to verify results
    5. RETRY: If tests fail, feed errors back to builder, retry
    6. COMPLETE: All subtasks done → report results
    """

    def __init__(self, ai_bridge, executor, on_event: Callable = None):
        self.ai_bridge = ai_bridge
        self.executor = executor
        self.on_event = on_event
        self.active_plan: Optional[Plan] = None
        self._cancel = False
        self._run_started_at: float = 0.0
        self.difficulty: str = "standard"
        # Reviewer rejection guard: configurable max rejections per run.
        self._reviewer_requeues: int = 0
        # P0-1: Canonical task/run bridge — set via run(canonical_context=...)
        self._canonical_ctx: Optional[Dict[str, Any]] = None
        # Maps orchestrator subtask ID → canonical NE ID (built after plan creation)
        self._subtask_ne_map: Dict[str, str] = {}

    async def emit(self, event_type: str, data: Dict):
        if self.on_event:
            await self.on_event({"type": event_type, **data})

    # ── P0-1: Canonical NE bridge helpers ──────────
    def _ne_id_for_subtask(self, subtask_id: str) -> Optional[str]:
        """Return canonical NE ID for a subtask, or None if no bridge."""
        return self._subtask_ne_map.get(subtask_id)

    def _runtime_server_module(self):
        """Resolve the live server module.

        When the backend is launched as `python server.py`, the running module is
        `__main__`, while `import server` creates a second module instance with an
        empty `connected_clients` set. That silently drops live WS broadcasts.
        """
        for module_name in ("__main__", "server"):
            module = sys.modules.get(module_name)
            if module and hasattr(module, "_broadcast_ws_event") and hasattr(module, "_transition_node_if_needed"):
                return module
        return None

    def _canonical_progress_for_status(self, status: str, explicit: Optional[int] = None) -> int:
        if explicit is not None:
            try:
                return max(0, min(100, int(explicit)))
            except Exception:
                pass
        normalized = str(status or "").strip().lower()
        if normalized == "running":
            return 5
        if normalized in {"passed", "failed", "cancelled", "skipped"}:
            return 100
        return 0

    def _append_ne_activity(
        self,
        subtask_id: str,
        message: str,
        *,
        entry_type: str = "info",
        timestamp_ms: Optional[int] = None,
    ) -> None:
        """Persist a human-readable activity entry for node detail/history hydration."""
        ne_id = self._ne_id_for_subtask(subtask_id)
        if not ne_id:
            return
        text = str(message or "").strip()
        if not text:
            return
        try:
            get_node_execution_store().update_node_execution(
                ne_id,
                {
                    "activity_log_append": [{
                        "ts": int(timestamp_ms or time.time() * 1000),
                        "msg": text[:600],
                        "type": str(entry_type or "info")[:16],
                    }],
                },
            )
        except Exception:
            pass

    def _update_ne_context(
        self,
        subtask_id: str,
        *,
        loaded_skills: Optional[List[str]] = None,
        reference_urls: Optional[List[str]] = None,
    ) -> None:
        """Persist richer canonical context for a node without changing its status."""
        ne_id = self._ne_id_for_subtask(subtask_id)
        if not ne_id:
            return
        update_data: Dict[str, Any] = {}
        if loaded_skills is not None:
            update_data["loaded_skills"] = loaded_skills
        if reference_urls is not None:
            update_data["reference_urls"] = reference_urls
        if not update_data:
            return
        try:
            get_node_execution_store().update_node_execution(ne_id, update_data)
        except Exception:
            pass

    def _browser_action_log_line(self, action: Dict[str, Any]) -> Optional[str]:
        action_name = str(action.get("action") or "").strip().lower()
        if not action_name:
            return None
        subaction = str(action.get("subaction") or action.get("intent") or "").strip().lower()
        effective_action = "snapshot" if action_name == "observe" else (subaction if action_name == "act" and subaction else action_name)
        target = str(action.get("target") or "").strip()
        url = str(action.get("url") or "").strip()
        state_changed = bool(action.get("state_changed"))
        keys_count = int(action.get("keys_count", 0) or 0)
        snapshot_ref_count = int(action.get("snapshot_ref_count", 0) or 0)
        observation = str(action.get("observation") or "").strip()
        snapshot_ref_bits: List[str] = []
        for item in (action.get("snapshot_refs_preview") or [])[:4]:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref", "") or "").strip()
            if not ref:
                continue
            label = str(item.get("label", "") or "").strip()
            role = str(item.get("role", "") or "").strip()
            descriptor = ref
            if label:
                descriptor += f" {label[:32]}"
            if role:
                descriptor += f" [{role[:18]}]"
            snapshot_ref_bits.append(descriptor)

        if action_name == "navigate":
            location = url or target
            return f"浏览器步骤: 打开 {location}" if location else "浏览器步骤: 打开预览页面"
        if action_name == "observe":
            suffix = ""
            if snapshot_ref_bits:
                suffix = f"；可交互引用 {', '.join(snapshot_ref_bits)}"
                if snapshot_ref_count > len(snapshot_ref_bits):
                    suffix += f" 等 {snapshot_ref_count} 个"
            if observation:
                suffix += f"；观察摘要 {observation[:220]}"
            return f"浏览器步骤: 观察当前界面并整理可操作元素{suffix}"
        if effective_action == "snapshot":
            if snapshot_ref_bits:
                suffix = f"；可交互引用 {', '.join(snapshot_ref_bits)}"
                if snapshot_ref_count > len(snapshot_ref_bits):
                    suffix += f" 等 {snapshot_ref_count} 个"
                return f"浏览器步骤: 截图并检查当前界面状态{suffix}"
            return "浏览器步骤: 截图并检查当前界面状态"
        if action_name == "extract":
            suffix = f"：{observation[:220]}" if observation else ""
            return f"浏览器步骤: 提取页面关键信息{suffix}"
        if effective_action == "scroll":
            return "浏览器步骤: 向下滚动页面并检查后续内容"
        if action_name == "click" or effective_action == "click":
            detail = target or url or "页面控件"
            suffix = "，并确认界面发生变化" if state_changed else ""
            prefix = "浏览器步骤: 根据观察结果执行点击" if action_name == "act" else "浏览器步骤: 点击"
            return f"{prefix} {detail}{suffix}"
        if action_name == "fill" or effective_action == "fill":
            detail = target or "表单字段"
            prefix = "浏览器步骤: 根据观察结果填写" if action_name == "act" else "浏览器步骤: 填写"
            return f"{prefix} {detail}"
        if effective_action in {"press", "press_sequence"}:
            suffix = f"（共 {keys_count} 个按键）" if keys_count > 0 else ""
            prefix = "浏览器步骤: 根据观察结果发送键盘输入验证交互" if action_name == "act" else "浏览器步骤: 发送键盘输入验证交互"
            return f"{prefix}{suffix}"
        if action_name == "wait_for" or effective_action == "wait_for":
            return "浏览器步骤: 等待页面状态稳定后再次验证"
        return f"浏览器步骤: 执行 {action_name}"

    async def _sync_ne_status(self, subtask_id: str, status: str, **extra):
        """Update canonical NodeExecution status if bridge active."""
        ne_id = self._ne_id_for_subtask(subtask_id)
        ctx = self._canonical_ctx or {}
        run_id = str(ctx.get("run_id", "") or "")
        task_id = str(ctx.get("task_id", "") or "")
        if not ne_id or not ctx:
            return
        try:
            server_module = self._runtime_server_module()
            if server_module is None:
                logger.warning("[Canonical] Live server module unavailable; skipping WS broadcast for %s", ne_id)
                return

            nes = get_node_execution_store()
            server_module._transition_node_if_needed(ne_id, status)
            update_data: Dict[str, Any] = {}
            update_data["progress"] = self._canonical_progress_for_status(
                status,
                explicit=extra.get("progress"),
            )
            if extra.get("phase"):
                update_data["phase"] = str(extra["phase"])[:120]
            # Update output_summary if provided
            if extra.get("output_summary"):
                update_data["output_summary"] = str(extra["output_summary"])[:2000]
            if extra.get("input_summary"):
                update_data["input_summary"] = str(extra["input_summary"])[:2000]
            if extra.get("error_message"):
                update_data["error_message"] = str(extra["error_message"])[:2000]
            if extra.get("tokens_used") and int(extra["tokens_used"]) > 0:
                update_data["tokens_used"] = int(extra["tokens_used"])
            if extra.get("cost") and float(extra["cost"]) > 0:
                update_data["cost"] = float(extra["cost"])
            if extra.get("loaded_skills") is not None:
                update_data["loaded_skills"] = extra.get("loaded_skills")
            if extra.get("reference_urls") is not None:
                update_data["reference_urls"] = extra.get("reference_urls")
            if extra.get("activity_log") is not None:
                update_data["activity_log"] = extra.get("activity_log")
            if extra.get("activity_log_append") is not None:
                update_data["activity_log_append"] = extra.get("activity_log_append")
            if update_data:
                nes.update_node_execution(ne_id, update_data)

            ne_snapshot = nes.get_node_execution(ne_id) or {}
            run_snapshot = get_run_store().get_run(run_id) if run_id else None
            payload = {
                "runId": run_id,
                "taskId": task_id,
                "nodeExecutionId": ne_id,
                "nodeKey": ne_snapshot.get("node_key", ""),
                "nodeLabel": ne_snapshot.get("node_label", ""),
                "status": ne_snapshot.get("status", status),
                "assignedModel": ne_snapshot.get("assigned_model", ""),
                "assignedProvider": ne_snapshot.get("assigned_provider", ""),
                "retryCount": ne_snapshot.get("retry_count", 0),
                "tokensUsed": ne_snapshot.get("tokens_used", 0),
                "cost": ne_snapshot.get("cost", 0.0),
                "inputSummary": ne_snapshot.get("input_summary", ""),
                "outputSummary": ne_snapshot.get("output_summary", ""),
                "errorMessage": ne_snapshot.get("error_message", ""),
                "artifactIds": ne_snapshot.get("artifact_ids", []),
                "startedAt": ne_snapshot.get("started_at", 0),
                "endedAt": ne_snapshot.get("ended_at", 0),
                "createdAt": ne_snapshot.get("created_at", 0),
                "progress": ne_snapshot.get("progress", update_data.get("progress", 0)),
                "phase": ne_snapshot.get("phase", ""),
                "loadedSkills": ne_snapshot.get("loaded_skills", []),
                "activityLog": ne_snapshot.get("activity_log", []),
                "referenceUrls": ne_snapshot.get("reference_urls", []),
                "timestamp": int(time.time() * 1000),
                "_neVersion": ne_snapshot.get("version", 0),
            }
            if run_snapshot:
                payload["activeNodeExecutionIds"] = run_snapshot.get("active_node_execution_ids", [])
                payload["_runVersion"] = run_snapshot.get("version", 0)
            await server_module._broadcast_ws_event({"type": "openclaw_node_update", "payload": payload})
        except Exception as e:
            logger.warning(f"[Canonical] Failed to sync NE {ne_id} to {status}: {e}")

    async def _emit_ne_progress(self, subtask_id: str, progress: int = 0, phase: str = "", partial_output: str = ""):
        """Emit openclaw_node_progress for canonical NE if bridge active."""
        ne_id = self._ne_id_for_subtask(subtask_id)
        ctx = self._canonical_ctx
        if not ne_id or not ctx:
            return
        try:
            server_module = self._runtime_server_module()
            if server_module is None:
                logger.warning("[Canonical] Live server module unavailable; skipping progress broadcast for %s", ne_id)
                return
            ne_store = get_node_execution_store()
            update_data: Dict[str, Any] = {}
            if partial_output:
                update_data["output_summary"] = partial_output[:2000]
            if phase:
                update_data["phase"] = phase[:120]
            update_data["progress"] = self._canonical_progress_for_status("running", explicit=progress)
            ne_store.update_node_execution(ne_id, update_data)
            ne_snapshot = ne_store.get_node_execution(ne_id) or {}
            payload = {
                "type": "openclaw_node_progress",
                "payload": {
                    "runId": ctx.get("run_id", ""),
                    "nodeExecutionId": ne_id,
                    "progress": ne_snapshot.get("progress", self._canonical_progress_for_status("running", explicit=progress)),
                    "phase": phase,
                    "partialOutput": partial_output[:500] if partial_output else "",
                    "loadedSkills": ne_snapshot.get("loaded_skills", []),
                    "activityLog": ne_snapshot.get("activity_log", []),
                    "referenceUrls": ne_snapshot.get("reference_urls", []),
                    "timestamp": int(time.time() * 1000),
                    "_neVersion": ne_snapshot.get("version", 0),
                },
            }
            run_snapshot = get_run_store().get_run(str(ctx.get("run_id", "") or ""))
            if run_snapshot:
                payload["payload"]["_runVersion"] = run_snapshot.get("version", 0)
            await server_module._broadcast_ws_event(payload)
        except Exception as e:
            logger.warning(f"[Canonical] Failed to emit progress for NE {ne_id}: {e}")

    # ── Model strategy ─────────────────────────
    # Priority lists per difficulty: first available key wins.
    _FAST_MODELS = ["kimi-coding", "deepseek-v3", "gemini-2.0-flash", "qwen-max"]
    _STRONG_MODELS = ["gpt-5.4", "claude-4-sonnet", "gemini-2.5-pro", "o3", "kimi-coding"]
    _DOWNGRADE_CHAIN = ["gpt-5.4", "claude-4-sonnet", "kimi-coding", "deepseek-v3", "gemini-2.0-flash", "qwen-max"]

    _MODEL_KEY_MAP: Dict[str, str] = {
        "gpt-5.4": "OPENAI_API_KEY", "gpt-4.1": "OPENAI_API_KEY", "gpt-4o": "OPENAI_API_KEY", "o3": "OPENAI_API_KEY",
        "claude-4-sonnet": "ANTHROPIC_API_KEY", "claude-4-opus": "ANTHROPIC_API_KEY", "claude-3.5-sonnet": "ANTHROPIC_API_KEY",
        "gemini-2.5-pro": "GEMINI_API_KEY", "gemini-2.0-flash": "GEMINI_API_KEY",
        "deepseek-v3": "DEEPSEEK_API_KEY", "deepseek-r1": "DEEPSEEK_API_KEY",
        "kimi-coding": "KIMI_API_KEY", "kimi-k2.5": "KIMI_API_KEY", "kimi": "KIMI_API_KEY",
        "qwen-max": "QWEN_API_KEY",
    }

    def _has_key_for(self, model_name: str) -> bool:
        env_var = self._MODEL_KEY_MAP.get(model_name)
        return bool(env_var and os.environ.get(env_var))

    def _first_available(self, candidates: List[str], fallback: str) -> str:
        for m in candidates:
            if self._has_key_for(m):
                return m
        return fallback

    def _model_for_difficulty(self, difficulty: str, default_model: str) -> str:
        """Select the optimal model based on difficulty level and available API keys."""
        if difficulty == "simple":
            return self._first_available(self._FAST_MODELS, default_model)
        elif difficulty == "pro":
            return self._first_available(self._STRONG_MODELS, default_model)
        # standard — use whatever the user or auto-detect chose
        return default_model

    def _downgrade_model(self, current_model: str) -> str:
        """Return the next model in the downgrade chain for failure auto-recovery."""
        try:
            idx = self._DOWNGRADE_CHAIN.index(current_model)
        except ValueError:
            idx = -1
        # Try each model after current in the chain
        for m in self._DOWNGRADE_CHAIN[idx + 1:]:
            if self._has_key_for(m):
                return m
        return current_model  # no alternative available

    def _configured_max_retries(self) -> int:
        """Runtime-configurable retry cap per subtask."""
        raw = None
        cfg = getattr(self.ai_bridge, "config", None)
        if isinstance(cfg, dict):
            raw = cfg.get("max_retries")
        if raw is None:
            raw = os.getenv("EVERMIND_MAX_RETRIES", "3")
        try:
            value = int(raw)
        except Exception:
            value = 3
        return max(1, min(value, 8))

    def _configured_tester_smoke(self) -> bool:
        """Runtime-configurable smoke toggle for deterministic tester gate."""
        cfg = getattr(self.ai_bridge, "config", None)
        if isinstance(cfg, dict):
            if "tester_run_smoke" in cfg:
                return str(cfg.get("tester_run_smoke")).strip().lower() in ("1", "true", "yes", "on")
            quality_cfg = cfg.get("quality")
            if isinstance(quality_cfg, dict) and "tester_run_smoke" in quality_cfg:
                return str(quality_cfg.get("tester_run_smoke")).strip().lower() in ("1", "true", "yes", "on")
        return str(os.getenv("EVERMIND_TESTER_RUN_SMOKE", "1")).strip().lower() in ("1", "true", "yes", "on")

    def _configured_reviewer_smoke(self) -> bool:
        """Runtime-configurable smoke toggle for reviewer deterministic gate."""
        cfg = getattr(self.ai_bridge, "config", None)
        if isinstance(cfg, dict):
            if "reviewer_run_smoke" in cfg:
                return str(cfg.get("reviewer_run_smoke")).strip().lower() in ("1", "true", "yes", "on")
            quality_cfg = cfg.get("quality")
            if isinstance(quality_cfg, dict) and "reviewer_run_smoke" in quality_cfg:
                return str(quality_cfg.get("reviewer_run_smoke")).strip().lower() in ("1", "true", "yes", "on")
        return self._configured_tester_smoke()

    def _configured_subtask_timeout(self, agent_type: str) -> int:
        """
        Hard upper bound for one subtask execution — max 15 minutes (900s).
        """
        cfg = getattr(self.ai_bridge, "config", None)
        # Timeouts tuned per role: builder needs more time,
        # planner must finish fast (lightweight spec output only).
        # All capped at 900s (15 min) absolute max.
        defaults = {"builder": 900, "planner": 120, "analyst": 480, "reviewer": 420}
        default_timeout = defaults.get(agent_type, 360)
        raw = None
        if isinstance(cfg, dict):
            per_agent_key = f"{agent_type}_timeout_sec"
            raw = cfg.get(per_agent_key)
            if raw is None:
                raw = cfg.get("subtask_timeout_sec")
        if raw is None:
            per_agent_env = f"EVERMIND_{agent_type.upper()}_TIMEOUT_SEC"
            raw = os.getenv(per_agent_env)
        if raw is None:
            raw = os.getenv("EVERMIND_SUBTASK_TIMEOUT_SEC", str(default_timeout))
        try:
            value = int(raw)
        except Exception:
            value = default_timeout
        return max(60, min(value, 900))

    def _configured_progress_heartbeat(self) -> int:
        raw = os.getenv("EVERMIND_PROGRESS_HEARTBEAT_SEC", "20")
        try:
            value = int(raw)
        except Exception:
            value = 20
        return max(5, min(value, 120))

    def _configured_max_reviewer_rejections(self) -> int:
        """Max times reviewer can reject builder and trigger re-run. Default: 2."""
        cfg = getattr(self.ai_bridge, "config", None)
        raw = None
        if isinstance(cfg, dict):
            raw = cfg.get("reviewer_max_rejections")
        if raw is None:
            raw = os.getenv("EVERMIND_REVIEWER_MAX_REJECTIONS", "2")
        try:
            return max(0, min(int(raw), 5))
        except Exception:
            return 2

    def _apply_retry_policy(self, plan: Plan):
        retries = self._configured_max_retries()
        for st in plan.subtasks:
            # Planner NEVER retries — it has a deterministic fallback skeleton
            if getattr(st, "agent_type", "") == "planner":
                st.max_retries = 0
            else:
                st.max_retries = retries
        plan.max_total_retries = max(plan.max_total_retries, retries * max(len(plan.subtasks), 1))

    # ── §FIX-2: Role-specific task descriptions for custom plan nodes ──

    def _custom_node_task_desc(self, agent_type: str, node_label: str, goal: str) -> str:
        """Return a focused task description for each agent type in a custom plan."""
        if agent_type == "builder":
            return f"{node_label}: {self._builder_task_description(goal)}"
        if agent_type == "planner":
            return (
                f"{node_label}: Produce a lightweight execution skeleton for: {goal[:200]}. "
                "Output ONLY valid JSON with: "
                '{"architecture": "2-3 sentence summary", '
                '"sections": ["section1", "section2", ...], '
                '"modules": ["module1", "module2", ...], '
                '"execution_order": ["step1 -> step2 -> ..."], '
                '"key_dependencies": ["dep1", "dep2"]}. '
                "NO code. NO marketing copy. Max 400 words. Finish FAST."
            )
        if agent_type == "analyst":
            return f"{node_label}: {task_classifier.analyst_description(goal)}"
        if agent_type == "reviewer":
            return f"{node_label}: {self._reviewer_task_description(goal, pro=False)}"
        if agent_type == "tester":
            profile = task_classifier.classify(goal)
            return f"{node_label}: {profile.tester_hint}"
        if agent_type == "deployer":
            return (
                f"{node_label}: List generated files in /tmp/evermind_output/ and provide "
                "local preview URL http://127.0.0.1:8765/preview/. "
                'Output: {{"status": "deployed", "preview_url": "...", "files": [...]}}.'
            )
        if agent_type == "debugger":
            return (
                f"{node_label}: Fix issues found by reviewer/tester for: {goal[:200]}. "
                "Use file_ops read to check /tmp/evermind_output/index.html, then fix and write back."
            )
        if agent_type == "scribe":
            return (
                f"{node_label}: Create structured documentation or narrative output for: {goal[:200]}. "
                "Prioritize clear sections, examples, checklists, and concise explanations."
            )
        if agent_type == "uidesign":
            return (
                f"{node_label}: Produce UI design direction for: {goal[:200]}. "
                "Define layout hierarchy, component behavior, motion intent, and visual system decisions."
            )
        if agent_type == "imagegen":
            return (
                f"{node_label}: Produce game/web image assets or prompt packs for: {goal[:200]}. "
                "If the comfyui plugin is available, check it first and use it when the pipeline is configured. "
                "Otherwise return concrete prompts, negative prompts, style-lock notes, shot variants, and fallback illustration guidance."
            )
        if agent_type == "spritesheet":
            return (
                f"{node_label}: Plan sprite sheet assets for: {goal[:200]}. "
                "Define asset families, animation states, palette constraints, export layout, frame counts, and replacement rules for builder integration."
            )
        if agent_type == "assetimport":
            return (
                f"{node_label}: Organize the asset pipeline for: {goal[:200]}. "
                "Return naming rules, folder structure, manifest fields, runtime mapping, and integration notes."
            )
        # Fallback — generic but still goal-specific
        return f"{node_label}: Contribute to: {goal[:200]}"

    def _builder_slot_index(self, plan: Plan, subtask_id: str) -> int:
        builders = [st for st in plan.subtasks if st.agent_type == "builder"]
        for idx, st in enumerate(builders, start=1):
            if st.id == subtask_id:
                return idx
        return 1

    def _extract_tagged_section(self, text: str, tag: str) -> str:
        pattern = re.compile(
            rf"<{re.escape(tag)}>\s*(.*?)\s*</{re.escape(tag)}>",
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(str(text or ""))
        if not match:
            return ""
        return re.sub(r"\n{3,}", "\n\n", match.group(1).strip())

    def _expected_analyst_handoff_tags(self, plan: Plan) -> List[str]:
        tags = ["reference_sites", "design_direction", "non_negotiables", "deliverables_contract", "risk_register"]
        builder_count = len([st for st in plan.subtasks if st.agent_type == "builder"])
        if builder_count >= 1:
            tags.append("builder_1_handoff")
        if builder_count >= 2:
            tags.append("builder_2_handoff")
        if any(st.agent_type == "reviewer" for st in plan.subtasks):
            tags.append("reviewer_handoff")
        if any(st.agent_type == "tester" for st in plan.subtasks):
            tags.append("tester_handoff")
        if any(st.agent_type == "debugger" for st in plan.subtasks):
            tags.append("debugger_handoff")
        return tags

    def _validate_analyst_handoff(self, text: str, plan: Plan) -> List[str]:
        missing: List[str] = []
        for tag in self._expected_analyst_handoff_tags(plan):
            if not self._extract_tagged_section(text, tag):
                missing.append(tag)
        return missing

    def _build_analyst_handoff_context(self, plan: Plan, subtask: SubTask, analyst_output: str) -> str:
        sections: List[tuple[str, str]] = []
        shared_tags = [
            ("References", "reference_sites"),
            ("Design Direction", "design_direction"),
            ("Non-Negotiables", "non_negotiables"),
            ("Deliverables Contract", "deliverables_contract"),
            ("Risk Register", "risk_register"),
        ]
        for label, tag in shared_tags:
            content = self._extract_tagged_section(analyst_output, tag)
            if content:
                sections.append((label, content))

        role_tags: List[tuple[str, str]] = []
        if subtask.agent_type == "builder":
            builder_slot = self._builder_slot_index(plan, subtask.id)
            role_tags.append((f"Builder {builder_slot} Handoff", f"builder_{builder_slot}_handoff"))
            if builder_slot > 1:
                role_tags.append(("Builder 1 Reference", "builder_1_handoff"))
        elif subtask.agent_type == "reviewer":
            role_tags.append(("Reviewer Handoff", "reviewer_handoff"))
        elif subtask.agent_type == "tester":
            role_tags.append(("Tester Handoff", "tester_handoff"))
        elif subtask.agent_type == "debugger":
            role_tags.append(("Debugger Handoff", "debugger_handoff"))

        for label, tag in role_tags:
            content = self._extract_tagged_section(analyst_output, tag)
            if content:
                sections.append((label, content))

        if not sections:
            fallback = str(analyst_output or "").strip()
            if not fallback:
                return ""
            return (
                "[Analyst Execution Contract]\n"
                "Use the upstream analyst report below as a mandatory execution brief.\n"
                f"{fallback[:1800]}"
            )

        rendered = []
        for label, content in sections:
            rendered.append(f"{label}:\n{content}")
        return (
            "[Analyst Execution Contract — MANDATORY]\n"
            "The upstream analyst already optimized the downstream brief. "
            "Treat every item below as execution criteria, not optional inspiration.\n\n"
            + "\n\n".join(rendered)
        )

    def _build_skill_contract_block(self, agent_type: str, loaded_skills: List[str]) -> str:
        names = [str(name).strip() for name in (loaded_skills or []) if str(name).strip()]
        if not names:
            return ""
        agent_label = agent_type or "node"
        return (
            "[Installed Skills — Mandatory Execution Contract]\n"
            f"This {agent_label} node has the following installed skills loaded:\n"
            + "\n".join(f"- {name}" for name in names)
            + "\n"
            "You must actively apply these skills as acceptance criteria. "
            "Do not ignore them, mention them superficially, or contradict them.\n"
            "Before finishing, verify your output reflects the loaded skills and the no-emoji quality bar.\n"
        )

    def _format_reviewer_rework_brief(self, reviewer_output: str) -> str:
        raw = str(reviewer_output or "").strip()
        if not raw:
            return "Reviewer rejected the output but did not provide usable detail."
        try:
            json_start = raw.find("{")
            json_end = raw.rfind("}") + 1
            parsed = json.loads(raw[json_start:json_end]) if json_start >= 0 and json_end > json_start else {}
        except Exception:
            parsed = {}

        if not isinstance(parsed, dict) or not parsed:
            return raw[:1200]

        verdict = str(parsed.get("verdict", "REJECTED")).strip() or "REJECTED"
        scores = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
        issues = parsed.get("blocking_issues") or parsed.get("issues") or []
        required_changes = parsed.get("required_changes") or parsed.get("improvements") or []
        acceptance = parsed.get("acceptance_criteria") or []
        missing_deliverables = parsed.get("missing_deliverables") or []
        ship_readiness = parsed.get("ship_readiness")
        blank_sections = parsed.get("blank_sections_found")
        lines = [f"Verdict: {verdict}"]
        if ship_readiness not in (None, ""):
            lines.append(f"Ship readiness: {ship_readiness}")
        if scores:
            score_bits = [f"{k}={v}" for k, v in list(scores.items())[:8]]
            if score_bits:
                lines.append("Scores: " + ", ".join(score_bits))
        if blank_sections not in (None, ""):
            lines.append(f"Blank sections found: {blank_sections}")
        if isinstance(issues, list) and issues:
            lines.append("Blocking issues:")
            lines.extend(f"- {str(item)[:220]}" for item in issues[:8])
        if isinstance(missing_deliverables, list) and missing_deliverables:
            lines.append("Missing deliverables:")
            lines.extend(f"- {str(item)[:220]}" for item in missing_deliverables[:8])
        if isinstance(required_changes, list) and required_changes:
            lines.append("Required changes:")
            lines.extend(f"- {str(item)[:220]}" for item in required_changes[:8])
        if isinstance(acceptance, list) and acceptance:
            lines.append("Acceptance criteria:")
            lines.extend(f"- {str(item)[:220]}" for item in acceptance[:8])
        return "\n".join(lines)[:1800]

    def _build_reviewer_forced_rejection(
        self,
        *,
        interaction_error: str = "",
        preview_gate: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Convert deterministic review failures into a structured REJECTED payload."""
        scores: Dict[str, int] = {
            "layout": 4,
            "color": 4,
            "typography": 4,
            "animation": 3,
            "responsive": 4,
            "functionality": 3,
            "completeness": 3,
            "originality": 4,
        }
        issues: List[str] = []
        blocking_issues: List[str] = []
        missing_deliverables: List[str] = []
        required_changes: List[str] = []
        acceptance_criteria: List[str] = []
        strengths: List[str] = []
        interactions_tested: List[str] = []
        blank_sections_found = 0

        def add_unique(items: List[str], value: Any) -> None:
            text = str(value or "").strip()
            if not text or text in items:
                return
            items.append(text[:280])

        if interaction_error:
            add_unique(issues, interaction_error)
            add_unique(blocking_issues, interaction_error)
            lower = interaction_error.lower()
            if "click or fill" in lower or "interactive element" in lower:
                add_unique(missing_deliverables, "Visible working CTA / form / interactive control")
                add_unique(required_changes, "Add at least one visible primary interactive element that does something meaningful.")
                add_unique(
                    acceptance_criteria,
                    "Reviewer can observe a visible CTA/form control and interact with it successfully.",
                )
                scores["functionality"] = min(scores["functionality"], 4)
                scores["completeness"] = min(scores["completeness"], 4)
            if "post-click state" in lower or "visible state" in lower or "state changed" in lower:
                add_unique(
                    required_changes,
                    "Make at least one primary interaction visibly change the page state after click/fill.",
                )
                add_unique(
                    acceptance_criteria,
                    "After interaction, reviewer can confirm changed text, panel state, modal, section switch, or success message.",
                )
                scores["functionality"] = min(scores["functionality"], 3)
            if "scroll the page" in lower:
                add_unique(missing_deliverables, "Below-the-fold content")
                add_unique(
                    required_changes,
                    "Add enough real content/sections so the page remains meaningful after scrolling.",
                )
                scores["completeness"] = min(scores["completeness"], 3)
            if "snapshot" in lower:
                add_unique(
                    required_changes,
                    "Ensure visible content renders in the first viewport so browser snapshot inspection is possible.",
                )

        gate = preview_gate if isinstance(preview_gate, dict) else {}
        smoke = gate.get("smoke") if isinstance(gate.get("smoke"), dict) else {}
        for err in gate.get("errors", []) or []:
            add_unique(issues, err)
        for warn in gate.get("warnings", []) or []:
            add_unique(issues, warn)
        for err in smoke.get("render_errors", []) or []:
            add_unique(issues, err)
            add_unique(blocking_issues, err)
        for err in smoke.get("page_errors", []) or []:
            add_unique(issues, f"Runtime error: {err}")
            add_unique(blocking_issues, "Browser runtime errors prevent correct rendering")
            add_unique(required_changes, "Fix JavaScript/runtime errors that prevent the page from rendering correctly.")
            scores["functionality"] = min(scores["functionality"], 2)

        smoke_status = str(smoke.get("status", "") or "").strip().lower()
        body_text_len = int(smoke.get("body_text_len", 0) or 0)
        render_summary = smoke.get("render_summary") if isinstance(smoke.get("render_summary"), dict) else {}
        readable_text_count = int(render_summary.get("readable_text_count", 0) or 0)
        heading_count = int(render_summary.get("heading_count", 0) or 0)
        interactive_count = int(render_summary.get("interactive_count", 0) or 0)
        image_count = int(render_summary.get("image_count", 0) or 0)
        canvas_count = int(render_summary.get("canvas_count", 0) or 0)

        blank_like = (
            body_text_len <= 20
            or any("blank" in str(item).lower() or "near-empty" in str(item).lower() for item in smoke.get("render_errors", []) or [])
        )
        unreadable_like = any("readable visible text" in str(item).lower() for item in smoke.get("render_errors", []) or [])

        if smoke_status == "fail" and blank_like:
            blank_sections_found = max(blank_sections_found, 2)
            add_unique(blocking_issues, "Preview renders as a blank or near-empty page.")
            add_unique(missing_deliverables, "Visible above-the-fold content")
            add_unique(required_changes, "Fix the render so the first viewport shows real content instead of a blank/white screen.")
            add_unique(
                acceptance_criteria,
                "Preview opens with a visible hero/body section above the fold instead of a blank page.",
            )
            scores["layout"] = min(scores["layout"], 2)
            scores["color"] = min(scores["color"], 2)
            scores["typography"] = min(scores["typography"], 2)
            scores["functionality"] = min(scores["functionality"], 2)
            scores["completeness"] = min(scores["completeness"], 1)
            scores["originality"] = min(scores["originality"], 2)

        if smoke_status == "fail" and unreadable_like:
            add_unique(blocking_issues, "Rendered text is not readable; page may be white-on-white or fully hidden.")
            add_unique(required_changes, "Fix text/background contrast and remove CSS that hides the main content.")
            add_unique(
                acceptance_criteria,
                "Reviewer can read headings/body copy normally in the first viewport without hidden or white-on-white text.",
            )
            scores["color"] = min(scores["color"], 2)
            scores["typography"] = min(scores["typography"], 2)
            scores["completeness"] = min(scores["completeness"], 2)

        if heading_count == 0 and interactive_count == 0 and image_count == 0 and canvas_count == 0:
            add_unique(missing_deliverables, "Meaningful visible sections or UI elements")
            scores["completeness"] = min(scores["completeness"], 2)

        if gate and not gate.get("preview_url"):
            add_unique(blocking_issues, "No preview artifact was available for review.")
            add_unique(missing_deliverables, "Previewable HTML artifact")
            add_unique(required_changes, "Write a valid previewable index.html artifact before handing off to reviewer.")
            scores["functionality"] = min(scores["functionality"], 2)
            scores["completeness"] = min(scores["completeness"], 1)

        if not blocking_issues and issues:
            add_unique(blocking_issues, issues[0])
        if not required_changes:
            add_unique(
                required_changes,
                "Resolve the preview rendering and interaction failures, then resubmit a reviewable product.",
            )
        if not acceptance_criteria:
            add_unique(
                acceptance_criteria,
                "Preview loads with visible readable content, survives scrolling, and at least one primary interaction works.",
            )
        if not strengths:
            add_unique(strengths, "A preview artifact exists and can be iterated on instead of restarting from zero.")
        if interaction_error:
            add_unique(interactions_tested, "Reviewer attempted browser interaction but the product did not satisfy the interaction gate.")
        elif gate.get("preview_url"):
            add_unique(interactions_tested, "Deterministic preview gate inspected the current preview artifact.")

        avg = round(sum(scores.values()) / max(len(scores), 1), 1)
        ship_readiness = min(6, max(1, int(round(avg))))
        payload: Dict[str, Any] = {
            "verdict": "REJECTED",
            "scores": scores,
            "ship_readiness": ship_readiness,
            "average": avg,
            "issues": issues[:8],
            "blocking_issues": blocking_issues[:8],
            "missing_deliverables": missing_deliverables[:8],
            "required_changes": required_changes[:8],
            "acceptance_criteria": acceptance_criteria[:8],
            "strengths": strengths[:5],
        }
        if blank_sections_found:
            payload["blank_sections_found"] = blank_sections_found
        if interactions_tested:
            payload["interactions_tested"] = interactions_tested[:5]
        return json.dumps(payload, ensure_ascii=False)

    # ── §FIX-3: Context-aware heartbeat partial output ──

    def _reviewer_interaction_instructions(self, task_type: str, goal: str) -> str:
        """Generate task-type-specific interaction test instructions for the reviewer."""
        if task_type == "game":
            return (
                "2. MANDATORY INTERACTION TEST (this is a GAME — you MUST play it):\n"
                "   a. Call browser observe first to inspect the visible start/play controls and HUD\n"
                "   b. Use browser act with a semantic target to click the start/play control\n"
                "   c. Use browser act with press_sequence (preferred) or multiple press actions with Arrow keys, WASD, Space, Enter\n"
                "   d. Play for at least 10-15 seconds to verify gameplay works\n"
                "   e. Verify the visible state changed after gameplay input with browser observe or wait_for (screen/hash/HUD/score/player position)\n"
                "   f. Take a screenshot MID-GAMEPLAY (not just the title screen)\n"
                "   g. REJECT if: game doesn't start, controls don't respond, visible state never changes, or browser runtime errors appear"
            )
        if task_type == "website":
            return (
                "2. MANDATORY INTERACTION TEST:\n"
                "   a. Call browser observe first to inspect visible navigation/buttons/forms\n"
                "   b. Use browser act to click key navigation links and buttons\n"
                "   c. Scroll through the entire page\n"
                "   d. Test forms, inputs, or interactive elements with browser act\n"
                "   e. You MUST use wait_for or browser observe after interaction to verify visible state changed\n"
                "   f. A review is invalid if it lacks post-action verification evidence\n"
                "   g. Check hover effects and animations\n"
                "   h. Take screenshots of different sections\n"
                "   i. REJECT if: navigation is broken, buttons don't work, layout is clearly wrong, or browser runtime errors appear"
            )
        if task_type == "dashboard":
            return (
                "2. MANDATORY INTERACTION TEST:\n"
                "   a. Call browser observe first to inspect visible tabs, filters, and controls\n"
                "   b. Use browser act to click tabs, filters, and interactive charts\n"
                "   c. Check data displays and values\n"
                "   d. You MUST use wait_for or browser observe after interaction to verify visible state changed\n"
                "   e. A review is invalid if it lacks post-action verification evidence\n"
                "   f. Test dropdowns or controls\n"
                "   g. REJECT if: charts don't render, filters don't work, data is missing, or browser runtime errors appear"
            )
        # Generic fallback
        return (
            "2. MANDATORY INTERACTION TEST:\n"
            "   a. Call browser observe first to inspect visible buttons/links/inputs\n"
            "   b. Use browser act to click visible buttons and links\n"
            "   c. Test interactive elements (forms, controls, inputs) with browser act\n"
            "   d. You MUST use wait_for or browser observe after interaction to verify the visible state changes\n"
            "   e. REJECT if: main feature doesn't work, post-action verification evidence is missing, product is non-functional, or browser runtime errors appear"
        )

    def _reviewer_task_description(self, goal: str, *, pro: bool = False) -> str:
        """Unified reviewer brief so custom and fallback plans enforce the same interaction standard."""
        profile = task_classifier.classify(goal)
        interaction_instructions = self._reviewer_interaction_instructions(profile.task_type, goal)
        prefix = "STRICT QUALITY GATE (pro mode)" if pro else "Quality gate"
        verdict_rules = (
            "7. Output JSON with verdict: APPROVED only when every major dimension is ship-ready. "
            "If any single dimension is below 5, or the average is below 7, the verdict MUST be REJECTED. "
            "If functionality, content-completeness, or originality is below 6, the verdict MUST be REJECTED. "
            "A REJECTED verdict MUST include a precise remediation brief the builder can execute without guessing."
        )
        task_specific_gate = ""
        if profile.task_type == "game":
            task_specific_gate = (
                "8. GAME-SPECIFIC GATE:\n"
                "   - REJECT if the game is technically running but not meaningfully playable\n"
                "   - REJECT if gameplay feedback, progression, win/lose loop, or restart loop feels incomplete\n"
                "   - REJECT if the visuals are placeholder-grade without a coherent art direction or replacement plan\n"
                "   - Treat weak playability as a ship blocker even if screenshots look acceptable\n"
            )
        # Inject loaded skill names as explicit review checkpoints
        from agent_skills import resolve_skill_names_for_goal
        reviewer_skills = resolve_skill_names_for_goal("reviewer", goal)
        skill_checklist = ""
        skill_step_number = "9" if task_specific_gate else "8"
        if reviewer_skills:
            skill_lines = [f"   - [{s}]" for s in reviewer_skills[:5]]
            skill_checklist = (
                f"{skill_step_number}. SKILL-BASED QUALITY CHECKS — you have the following review skills loaded.\n"
                "   Use them as your scoring standard:\n"
                + "\n".join(skill_lines) + "\n"
                "   Apply each skill's criteria when scoring. If the product fails any skill's standard, REJECT.\n"
            )
        return (
            f"{prefix} for: {goal[:200]}.\n"
            "Use browser to navigate to http://127.0.0.1:8765/preview/ and perform THOROUGH testing:\n"
            "1. Take screenshots of the initial state (hero/first viewport)\n"
            f"{interaction_instructions}\n"
            "3. MANDATORY SCROLL CHECK: Scroll to the BOTTOM of the page slowly.\n"
            "   Take a screenshot after every ~500px of scrolling.\n"
            "   You MUST cover the ENTIRE page from top to bottom, not just the first viewport.\n"
            "4. CONTENT COMPLETENESS CHECK (CRITICAL):\n"
            "   - Are there any sections that are completely BLANK (no text, no images)?\n"
            "   - Are there empty placeholder tags (<h2/>, <p/>, <div> with no content)?\n"
            "   - Are there sections with only background color but no actual content?\n"
            "   - Are there emoji glyphs used as icons, bullets, CTA decorations, or fake illustrations?\n"
            "   - Are the visuals generic, template-like, commercially weak, or clearly below pro quality?\n"
            "   - If more than 1 section is empty/blank, you MUST REJECT immediately.\n"
            "   - Check: do ALL links/buttons actually lead somewhere?\n"
            "   - Are there IMAGES where images are expected (food photos, product images, etc)?\n"
            "   - Are there ANIMATIONS or transitions (hover effects, scroll reveals, etc)?\n"
            "   - Is there a FOOTER with actual content?\n"
            "5. Take screenshots AFTER interaction to prove you tested functionality\n"
            "6. Score: layout/color/typography/animation/responsive/functionality/content-completeness/originality (each 1-10)\n"
            "   A content-completeness score below 5 = AUTOMATIC REJECT\n"
            "   ANY single dimension score below 5 = AUTOMATIC REJECT\n"
            "   Any functionality/content-completeness/originality score below 6 = REJECT\n"
            "   Average score below 7 = REJECT\n"
            "   REJECT if: any core feature doesn't work, sections are blank/empty, no images where expected,\n"
            "   no animations where expected, buttons don't respond, emoji glyphs cheapen the interface,\n"
            "   product is commercially weak, or the result still feels like a generic student project.\n"
            f"{verdict_rules}\n"
            f"{task_specific_gate}"
            f"{skill_checklist}"
            'Output JSON: {{"verdict": "APPROVED/REJECTED", "scores": {{...}}, '
            '"ship_readiness": 1-10, "issues": ["high-level findings"], "blocking_issues": ["ship blockers"], '
            '"missing_deliverables": ["missing artifact / missing section / missing interaction"], '
            '"required_changes": ["exact builder changes"], "acceptance_criteria": ["how re-review will pass"], '
            '"blank_sections_found": <number>, "interactions_tested": ["list what you clicked/tested"], '
            '"strengths": ["what is already good enough to preserve"]}}.'
        )

    def _interaction_gate_error(self, agent_type: str, task_type: str, browser_actions: List[Dict[str, Any]]) -> Optional[str]:
        """Hard interaction gate so reviewer/tester behavior is not prompt-only."""
        successful_actions = [
            item for item in browser_actions
            if item.get("ok")
        ]

        def _normalized_action(item: Dict[str, Any]) -> str:
            action = str(item.get("action") or "").strip().lower()
            subaction = str(item.get("subaction") or item.get("intent") or "").strip().lower()
            if action == "observe":
                return "snapshot"
            if action == "act" and subaction:
                return subaction
            return action

        successful = {
            _normalized_action(item)
            for item in successful_actions
        }
        if not successful:
            return None

        agent_label = agent_type.title()
        max_console_errors = max(int(item.get("console_error_count", 0) or 0) for item in successful_actions)
        max_page_errors = max(int(item.get("page_error_count", 0) or 0) for item in successful_actions)
        max_failed_requests = max(int(item.get("failed_request_count", 0) or 0) for item in successful_actions)
        # Allow up to 3 console errors (favicon 404, polyfill warnings, etc. are common)
        CONSOLE_ERROR_TOLERANCE = 3
        PAGE_ERROR_TOLERANCE = 0
        FAILED_REQUEST_TOLERANCE = 2
        if max_page_errors > PAGE_ERROR_TOLERANCE:
            return (
                f"{agent_label} interaction gate failed: browser session reported "
                f"{max_page_errors} page error(s) (tolerance={PAGE_ERROR_TOLERANCE})."
            )
        if max_console_errors > CONSOLE_ERROR_TOLERANCE:
            return (
                f"{agent_label} interaction gate failed: browser session reported "
                f"{max_console_errors} console error(s) (tolerance={CONSOLE_ERROR_TOLERANCE})."
            )
        if max_failed_requests > FAILED_REQUEST_TOLERANCE:
            return (
                f"{agent_label} interaction gate failed: browser session reported "
                f"{max_failed_requests} failed network request(s) (tolerance={FAILED_REQUEST_TOLERANCE})."
            )

        interactive_actions = {"click", "fill", "press", "press_sequence"}
        verification_actions = {"snapshot", "wait_for"}

        seen_interaction = False
        has_post_interaction_verification = False
        has_post_interaction_state_change = False
        for item in successful_actions:
            action = _normalized_action(item)
            state_changed = bool(item.get("state_changed", False))
            if action in interactive_actions:
                seen_interaction = True
                if state_changed:
                    has_post_interaction_state_change = True
                continue
            if seen_interaction and action in verification_actions:
                has_post_interaction_verification = True
                if state_changed:
                    has_post_interaction_state_change = True

        if task_type == "game":
            if "click" not in successful:
                return f"{agent_label} interaction gate failed: game tasks must click a start/play control before approval."
            key_events = sum(
                max(1, int(item.get("keys_count", 0) or 1))
                for item in successful_actions
                if _normalized_action(item) in {"press", "press_sequence"}
            )
            if key_events < 3:
                return f"{agent_label} interaction gate failed: game tasks must send multiple gameplay key inputs before approval."
            state_hashes = {
                str(item.get("state_hash") or "").strip()
                for item in successful_actions
                if str(item.get("state_hash") or "").strip()
            }
            if len(state_hashes) < 2:
                return f"{agent_label} interaction gate failed: game tasks must prove the visible game state changed after interaction."
            return None

        if task_type == "dashboard":
            if "click" not in successful:
                return f"{agent_label} interaction gate failed: dashboard tasks must click at least one interactive control."
            if "snapshot" not in successful:
                return f"{agent_label} interaction gate failed: dashboard tasks must inspect visible controls via browser snapshot."
            if not has_post_interaction_verification:
                return f"{agent_label} interaction gate failed: dashboard tasks must verify the UI after interaction via snapshot or wait_for."
            if not has_post_interaction_state_change:
                return f"{agent_label} interaction gate failed: dashboard tasks must prove interaction changed the visible state."
            return None

        if task_type == "website":
            if agent_type == "reviewer":
                if "snapshot" not in successful:
                    return f"{agent_label} interaction gate failed: website reviews must inspect the page via browser snapshot."
                if "scroll" not in successful:
                    return f"{agent_label} interaction gate failed: website reviews must scroll the page."
                if "click" not in successful and "fill" not in successful:
                    return f"{agent_label} interaction gate failed: website reviews must click or fill at least one interactive element."
                if not has_post_interaction_verification:
                    return f"{agent_label} interaction gate failed: website reviews must verify the post-click state via snapshot or wait_for."
                if not has_post_interaction_state_change:
                    return f"{agent_label} interaction gate failed: website reviews must prove at least one interaction changed the visible state."
            elif agent_type == "tester":
                if "snapshot" not in successful:
                    return f"{agent_label} interaction gate failed: website tests must inspect the page via browser snapshot."
                if "scroll" not in successful:
                    return f"{agent_label} interaction gate failed: website tests must scroll the page."
                if "click" not in successful and "fill" not in successful:
                    return f"{agent_label} interaction gate failed: website tests must click or fill at least one interactive element."
                if not has_post_interaction_verification:
                    return f"{agent_label} interaction gate failed: website tests must verify the post-click state via snapshot or wait_for."
                if not has_post_interaction_state_change:
                    return f"{agent_label} interaction gate failed: website tests must prove at least one interaction changed the visible state."
            return None

        return None

    def _extract_streaming_hint(self, streaming_text: str) -> str:
        """Extract project-specific progress from raw AI streaming output.
        Returns a short hint like '正在编写 .hero-section 的 CSS 样式' or ''."""
        text = str(streaming_text or "").strip()
        if len(text) < 30:
            return ""
        # Take the last 800 chars of streaming output for current context
        tail = text[-800:]
        lines = [ln.strip() for ln in tail.split("\n") if ln.strip()]
        if not lines:
            return ""

        # Look for file_ops write calls → tells us what file is being saved
        file_match = re.search(r'file_ops.*?write.*?["\']([^"\'/]+\.[a-zA-Z]+)', tail, re.IGNORECASE)
        if file_match:
            return f"正在写入文件 {file_match.group(1)}"

        # Look for HTML section markers like <!-- Hero Section -->
        section_match = re.findall(r'<!--\s*(.{3,40}?)\s*-->', tail)
        if section_match:
            last_section = section_match[-1].strip()
            return f"正在构建 {last_section} 部分"

        # Look for CSS class/id selectors being defined
        css_match = re.findall(r'([.#][a-zA-Z][\w-]{2,30})\s*\{', tail)
        if css_match:
            recent = css_match[-1]
            return f"正在编写 {recent} 的样式"

        # Look for JS function definitions
        func_match = re.findall(r'(?:function\s+|const\s+|let\s+)(\w{3,30})\s*[=(]', tail)
        if func_match:
            return f"正在实现 {func_match[-1]} 逻辑"

        # Look for HTML tags being constructed
        tag_match = re.findall(r'<(section|div|nav|header|footer|main|canvas|form|table|ul|button)\b[^>]*(?:class=["\']([^"\']*))?', tail)
        if tag_match:
            tag_name, cls = tag_match[-1]
            detail = cls.split()[0] if cls else tag_name
            return f"正在构建 <{tag_name}> {detail} 元素"

        return ""

    def _heartbeat_partial_output(self, agent_type: str, elapsed: int, loaded_skills: List[str] = None, task_desc: str = "", streaming_text: str = "") -> str:
        """Return a human-readable partial output for heartbeat progress events.
        Uses streaming AI output to extract project-specific details."""
        skill_text = ""
        if loaded_skills:
            skill_names = ", ".join(str(s).strip() for s in loaded_skills[:4] if str(s).strip())
            if skill_names:
                skill_text = f" | 已加载技能: {skill_names}"
        task_hint = ""
        if task_desc:
            snippet = str(task_desc).strip()[:80]
            task_hint = f" | 任务: {snippet}"

        # Try to extract project-specific progress from streaming output
        streaming_hint = self._extract_streaming_hint(streaming_text)
        if streaming_hint:
            streaming_detail = f" | {streaming_hint}"
        else:
            streaming_detail = ""

        if agent_type == "planner":
            if elapsed < 10:
                return f"正在分析任务需求，理解项目目标{task_hint} ({elapsed}s)"
            if elapsed < 30:
                return f"正在拆解模块和依赖关系，设计执行框架{task_hint} ({elapsed}s)"
            if elapsed < 60:
                return f"正在生成执行骨架，规划各模块分工{task_hint} ({elapsed}s)"
            return f"规划较为复杂，仍在处理中（超时会自动使用备用方案） ({elapsed}s)"
        if agent_type == "analyst":
            if elapsed < 15:
                return f"正在搜索相关参考案例和设计灵感{task_hint} ({elapsed}s)"
            if elapsed < 60:
                return f"正在分析参考网站的配色、布局和交互设计{streaming_detail}{task_hint} ({elapsed}s)"
            if elapsed < 120:
                return f"正在整理设计要点，撰写设计简报{streaming_detail} ({elapsed}s)"
            return f"分析师仍在整理参考资料{streaming_detail} ({elapsed}s)"
        if agent_type == "builder":
            if streaming_hint:
                return f"{streaming_hint}{skill_text} ({elapsed}s)"
            if elapsed < 20:
                return f"正在构建页面结构和核心组件{skill_text}{task_hint} ({elapsed}s)"
            if elapsed < 60:
                return f"正在编写样式和交互逻辑{skill_text}{task_hint} ({elapsed}s)"
            if elapsed < 120:
                return f"正在完善细节，添加动画效果{skill_text}{task_hint} ({elapsed}s)"
            if elapsed < 240:
                return f"代码量较大，仍在生成中{skill_text}{task_hint} ({elapsed}s)"
            return f"构建时间较长，接近完成{skill_text}{task_hint} ({elapsed}s)"
        if agent_type == "reviewer":
            if elapsed < 15:
                return f"正在打开预览页面并截屏{task_hint} ({elapsed}s)"
            if elapsed < 45:
                return f"正在检查布局、配色和响应式效果{streaming_detail} ({elapsed}s)"
            return f"正在撰写审查报告{streaming_detail} ({elapsed}s)"
        if agent_type == "tester":
            if elapsed < 15:
                return f"正在启动浏览器进行自动化测试{task_hint} ({elapsed}s)"
            if elapsed < 45:
                return f"正在检查页面功能和交互是否正常{streaming_detail} ({elapsed}s)"
            return f"正在生成测试报告{streaming_detail} ({elapsed}s)"
        if agent_type == "deployer":
            if elapsed < 15:
                return f"正在准备部署环境... ({elapsed}s)"
            return f"正在部署产物到预览服务器... ({elapsed}s)"
        return f"正在执行中{streaming_detail}... ({elapsed}s)"

    def _humanize_output_summary(self, agent_type: str, raw_output: str, success: bool, files_created: list = None) -> str:
        """Generate a human-readable summary from raw agent output for display in node popup."""
        if not success:
            # Extract useful part of error
            err = raw_output[:200] if raw_output else "未知错误"
            return f"执行失败：{err}"

        if agent_type == "planner":
            return "任务规划完成，已生成执行骨架和模块分工方案。"

        if agent_type == "analyst":
            # Try to extract key info from analyst output
            lines = raw_output.strip().split('\n') if raw_output else []
            # Look for design brief mentions
            summary_parts = ["分析完成"]
            ref_count = sum(1 for l in lines if 'http' in l.lower() or 'www' in l.lower() or '参考' in l)
            if ref_count > 0:
                summary_parts.append(f"找到 {min(ref_count, 5)} 个参考案例")
            if any('color' in l.lower() or '配色' in l or '颜色' in l for l in lines):
                summary_parts.append("提取了配色方案")
            if any('layout' in l.lower() or '布局' in l for l in lines):
                summary_parts.append("分析了布局模式")
            return "，".join(summary_parts) + "。"

        if agent_type == "builder":
            file_info = ""
            if files_created:
                file_names = [f.split('/')[-1] for f in files_created[:3]]
                file_info = f"生成了 {', '.join(file_names)}"
            else:
                file_info = "代码已生成"
            # Try to extract what was built
            lower = raw_output.lower() if raw_output else ""
            features = []
            if 'html' in lower:
                features.append("HTML页面")
            if 'css' in lower or 'style' in lower:
                features.append("样式")
            if 'animation' in lower or '动画' in lower:
                features.append("动画效果")
            if 'responsive' in lower or '响应式' in lower:
                features.append("响应式布局")
            feat_str = "（包含" + "、".join(features[:3]) + "）" if features else ""
            return f"构建完成，{file_info}{feat_str}。"

        if agent_type == "reviewer":
            lower = raw_output.lower() if raw_output else ""
            if 'approved' in lower or '通过' in lower:
                return "审查通过，质量符合标准。"
            elif 'rejected' in lower or '不通过' in lower:
                return "审查未通过，已给出详细整改要求。"
            return "审查完成，已生成评审报告。"

        if agent_type == "tester":
            lower = raw_output.lower() if raw_output else ""
            if 'pass' in lower or '通过' in lower:
                return "测试通过，功能和视觉效果正常。"
            elif 'fail' in lower or '失败' in lower:
                return "测试发现问题，部分功能需要修复。"
            return "测试完成，已生成测试报告。"

        if agent_type == "deployer":
            return "部署完成，产物已发布到预览服务器。"

        return f"{agent_type} 执行完成。"

    def _update_ne_failure_details(
        self,
        subtask_id: str,
        *,
        output_summary: str,
        error_message: str,
        tokens_used: int = 0,
        cost: float = 0.0,
    ) -> None:
        """Update canonical node details for a failed attempt without forcing terminal status."""
        ne_id = self._ne_id_for_subtask(subtask_id)
        if not ne_id:
            return
        try:
            nes = get_node_execution_store()
            update_data: Dict[str, Any] = {
                "output_summary": str(output_summary or "")[:2000],
                "error_message": str(error_message or "")[:500],
            }
            if int(tokens_used or 0) > 0:
                update_data["tokens_used"] = int(tokens_used)
            if float(cost or 0.0) > 0:
                update_data["cost"] = float(cost)
            nes.update_node_execution(ne_id, update_data)
        except Exception:
            pass

    def _normalize_html_artifact(self, html_code: str) -> str:
        """Auto-close truncated HTML so quality gates can validate the saved artifact."""
        normalized = str(html_code or "").strip()
        lower = normalized.lower()
        if "<!doctype" not in lower and "<html" not in lower:
            return normalized

        html_close_idx = lower.rfind("</html>")
        body_close_idx = lower.rfind("</body>")
        mutated = False

        if html_close_idx >= 0 and body_close_idx < 0:
            normalized = normalized[:html_close_idx].rstrip() + "\n</body>\n" + normalized[html_close_idx:]
            mutated = True
        elif html_close_idx < 0:
            if body_close_idx < 0:
                normalized = normalized.rstrip() + "\n</body>"
                mutated = True
            normalized = normalized.rstrip() + "\n</html>"
            mutated = True

        if mutated:
            logger.info(f"Auto-closed truncated HTML ({len(normalized)} chars)")
        return normalized

    def _collect_reference_urls(self, text: str, tool_results: List[Any]) -> List[str]:
        """Collect distinct reference URLs from browser tool results and analyst output."""
        urls: List[str] = []

        def add_url(value: Any) -> None:
            url = str(value or "").strip()
            if not url or not url.lower().startswith(("http://", "https://")):
                return
            cleaned = url.rstrip(".,);]")
            if cleaned not in urls:
                urls.append(cleaned)

        for tr in tool_results or []:
            if not isinstance(tr, dict):
                continue
            data = tr.get("data")
            if isinstance(data, dict):
                add_url(data.get("url"))
            for artifact in tr.get("artifacts") or []:
                if isinstance(artifact, dict):
                    add_url(artifact.get("url"))

        for match in _URL_RE.findall(str(text or "")):
            add_url(match)

        return urls

    # ── §FIX-4: Deterministic planner fallback skeleton ──

    def _planner_fallback_skeleton(self, goal: str) -> str:
        """Generate a minimal plan skeleton WITHOUT any LLM call.
        Used when the planner times out or fails, to prevent blocking the entire chain."""
        try:
            profile = task_classifier.classify(goal)
            skeleton = {
                "architecture": f"Single-page {profile.task_type} built as self-contained index.html",
                "sections": [],
                "modules": [],
                "execution_order": [],
                "key_dependencies": ["index.html"],
            }
            # Populate sections/modules based on task type
            _type_sections = {
                "website": ["header/nav", "hero", "features", "testimonials", "CTA", "footer"],
                "game": ["game-loop", "input-handler", "renderer", "collision", "HUD", "start-screen", "game-over"],
                "dashboard": ["sidebar", "topbar", "stat-cards", "chart-area", "data-table"],
                "tool": ["input-panel", "processing-logic", "output-panel", "copy-button"],
                "presentation": ["slide-framework", "navigation", "slides-content", "PDF-export"],
                "creative": ["canvas-setup", "render-loop", "interaction", "color-system"],
            }
            sections = _type_sections.get(profile.task_type, ["header", "main-content", "footer"])
            skeleton["sections"] = sections
            skeleton["modules"] = [f"{s}-module" for s in sections[:4]]
            skeleton["execution_order"] = [
                "analyze requirements",
                f"build {profile.task_type} structure",
                "add styling and interactions",
                "review and polish",
            ]
            return json.dumps(skeleton, indent=2)
        except Exception as e:
            logger.warning(f"[Planner] Fallback skeleton generation failed: {e}")
            return ""


    def _builder_design_requirements(self, goal: str = "") -> str:
        """Return task-adaptive design requirements based on goal classification."""
        return task_classifier.design_requirements(goal)

    def _builder_task_description(self, goal: str) -> str:
        """Return task-adaptive builder task description."""
        return task_classifier.builder_task_description(goal)

    def _pro_builder_focus(self, goal: str) -> tuple[str, str]:
        """Return parallel builder focus for pro mode. Each builder creates an independent part.
        Website parts save separately; Evermind assembles index.html preview automatically."""
        task_type = task_classifier.classify(goal).task_type
        mapping = {
            "website": (
                "YOUR JOB: Build the TOP HALF — header/nav, hero, trust badges, features grid. "
                "Save ONLY your assigned output to /tmp/evermind_output/index_part1.html via file_ops write. "
                "Do NOT overwrite /tmp/evermind_output/index.html in this step.",
                "YOUR JOB: Build the BOTTOM HALF — testimonials, pricing/CTA, footer. "
                "Save ONLY your assigned output to /tmp/evermind_output/index_part2.html via file_ops write. "
                "Do NOT overwrite /tmp/evermind_output/index.html in this step.",
            ),
            "game": (
                "YOUR JOB: Build core gameplay — game loop, controls, collision, rendering, start screen. "
                "Save to /tmp/evermind_output/index.html via file_ops write.",
                "YOUR JOB: Build game polish — HUD, score display, game-over/restart, sound effects, "
                "visual feedback (particles, shake), mobile touch support. "
                "Read existing /tmp/evermind_output/index.html and ENHANCE it, then write back.",
            ),
            "presentation": (
                "YOUR JOB: Build slide framework + slides 1-5 (title, overview, context, first content). "
                "Include nav controls, progress bar, keyboard bindings, PDF print CSS. "
                "Save to /tmp/evermind_output/index.html via file_ops write.",
                "YOUR JOB: Read existing /tmp/evermind_output/index.html and ADD slides 6-10 "
                "(remaining content, takeaways, Q&A). Keep existing nav/framework intact. Write back.",
            ),
            "dashboard": (
                "YOUR JOB: Build layout shell (sidebar/topbar) + KPI stat cards + main chart area. "
                "Save to /tmp/evermind_output/index.html via file_ops write.",
                "YOUR JOB: Read existing /tmp/evermind_output/index.html and ADD table/filters, "
                "secondary panels, responsive polish. Write back.",
            ),
            "tool": (
                "YOUR JOB: Build core input/output workflow, validation, primary utility function. "
                "Save to /tmp/evermind_output/index.html via file_ops write.",
                "YOUR JOB: Read existing /tmp/evermind_output/index.html and ADD UX polish "
                "(copy/reset buttons, keyboard shortcuts, error states, responsive). Write back.",
            ),
            "creative": (
                "YOUR JOB: Build core render loop, visual concept, interaction model. "
                "Save to /tmp/evermind_output/index.html via file_ops write.",
                "YOUR JOB: Read existing /tmp/evermind_output/index.html and ADD visual richness "
                "(particle effects, color transitions, interaction feedback). Write back.",
            ),
        }
        return mapping.get(task_type, (
            "Build core structure and primary features. Save to /tmp/evermind_output/index.html.",
            "Read existing file, add polish and secondary features. Write back.",
        ))

    def _runtime_config(self) -> Optional[Dict[str, Any]]:
        return getattr(self.ai_bridge, "config", None)

    def _image_generation_available(self) -> bool:
        return is_image_generation_available(config=self._runtime_config())

    def _goal_wants_generated_assets(self, goal: str) -> bool:
        profile = task_classifier.classify(goal)
        text = str(goal or "")
        if profile.task_type != "game":
            return bool(re.search(
                r"(海报|封面|插画|concept art|illustration|poster|角色设定|image asset|image pack)",
                text,
                re.IGNORECASE,
            ))
        return bool(re.search(
            r"(sprite|spritesheet|tileset|pixel|像素|素材|asset|角色|敌人|boss|平台跳跃|platformer|马里奥|mario|"
            r"地图|关卡|特效|vfx|动画帧|frame animation|avatar|character art|enemy art)",
            text,
            re.IGNORECASE,
        ))

    def _asset_pipeline_enabled_for_goal(self, goal: str) -> bool:
        return self._goal_wants_generated_assets(goal) and self._image_generation_available()

    def _builder_asset_pipeline_note(self, goal: str) -> str:
        if not self._goal_wants_generated_assets(goal):
            return ""
        if self._image_generation_available():
            return (
                "Asset pipeline is available. Expect upstream imagegen / spritesheet / assetimport outputs. "
                "Integrate those assets cleanly and keep runtime paths/manifests explicit."
            )
        return (
            "No configured image-generation backend is available for this run. "
            "Do NOT block on raster art generation. Use high-quality SVG / CSS / pixel placeholders, "
            "keep an explicit asset manifest, and make later asset replacement straightforward."
        )

    def _asset_pipeline_descriptions(self, goal: str) -> tuple[str, str, str]:
        return (
            self._custom_node_task_desc("imagegen", "Image Gen", goal)
            + " Save generated assets or prompt-pack artifacts under /tmp/evermind_output/assets/.",
            self._custom_node_task_desc("spritesheet", "Spritesheet", goal)
            + " Produce a concrete frame map / sprite manifest that downstream builders can wire in immediately.",
            self._custom_node_task_desc("assetimport", "Asset Import", goal)
            + " Normalize filenames, manifest keys, runtime lookup paths, and replacement rules for the builders.",
        )

    def _extract_html_title(self, html: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        return re.sub(r"\s+", " ", match.group(1)).strip()

    def _extract_html_body_fragment(self, html: str) -> str:
        text = (html or "").strip()
        body_match = re.search(r"<body[^>]*>(.*?)</body>", text, re.IGNORECASE | re.DOTALL)
        if body_match:
            return body_match.group(1).strip()

        cleaned = re.sub(r"<!doctype[^>]*>", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"<head[^>]*>.*?</head>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r"</?(html|body)[^>]*>", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    def _assemble_parallel_builder_html(self, html_parts: List[tuple[str, str]]) -> str:
        title = ""
        style_blocks: List[str] = []
        script_blocks: List[str] = []
        body_sections: List[str] = []

        for label, html in html_parts:
            if not title:
                title = self._extract_html_title(html)
            style_blocks.extend(
                block.strip()
                for block in re.findall(r"<style[^>]*>.*?</style>", html or "", re.IGNORECASE | re.DOTALL)
                if block.strip()
            )
            script_blocks.extend(
                block.strip()
                for block in re.findall(r"<script[^>]*>.*?</script>", html or "", re.IGNORECASE | re.DOTALL)
                if block.strip()
            )
            body = self._extract_html_body_fragment(html)
            if body:
                body_sections.append(
                    f"<!-- {label} -->\n<div data-evermind-part=\"{label}\">\n{body}\n</div>"
                )

        deduped_styles = "\n\n".join(dict.fromkeys(style_blocks))
        deduped_scripts = "\n\n".join(dict.fromkeys(script_blocks))
        combined_body = "\n\n".join(body_sections).strip() or "<main></main>"

        return (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <meta charset=\"UTF-8\">\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
            f"  <title>{title or 'Evermind Preview'}</title>\n"
            f"{deduped_styles}\n"
            "</head>\n"
            "<body>\n"
            f"{combined_body}\n"
            f"{deduped_scripts}\n"
            "</body>\n"
            "</html>\n"
        )

    def _materialize_parallel_builder_preview(self) -> Optional[Path]:
        part_paths = [OUTPUT_DIR / "index_part1.html", OUTPUT_DIR / "index_part2.html"]
        if not all(path.exists() and path.is_file() for path in part_paths):
            return None

        html_parts: List[tuple[str, str]] = []
        for path in part_paths:
            try:
                html_parts.append((path.name, path.read_text(encoding="utf-8", errors="ignore")))
            except Exception as e:
                logger.warning(f"Failed to read parallel builder part {path}: {e}")
                return None

        root_index = OUTPUT_DIR / "index.html"
        try:
            root_index.write_text(
                self._assemble_parallel_builder_html(html_parts),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to materialize combined preview artifact: {e}")
            return None
        return root_index

    def _select_preview_artifact_for_files(self, files_created: List[str]) -> Optional[Path]:
        html_candidates = [
            Path(f) for f in files_created
            if str(f).lower().endswith((".html", ".htm"))
        ]

        if any(is_partial_html_artifact(path) for path in html_candidates):
            merged = self._materialize_parallel_builder_preview()
            if merged and merged.exists():
                return merged

        final_candidates = [
            path for path in html_candidates
            if path.exists() and path.is_file() and not is_partial_html_artifact(path)
        ]
        if not final_candidates:
            return None
        final_candidates.sort(key=lambda path: (path.name != "index.html", path.name))
        return final_candidates[0]

    def _has_preview_artifact_for_degraded_flow(self) -> bool:
        """Return True only when a real previewable artifact exists.

        This prevents reviewer/deployer/tester from running against website
        part files like index_part1.html before a final index.html exists.
        """
        self._materialize_parallel_builder_preview()
        _task_id, preview_file = latest_preview_artifact(OUTPUT_DIR)
        if not preview_file or not Path(preview_file).exists():
            return False
        if self._run_started_at > 0:
            try:
                return preview_file.stat().st_mtime >= max(self._run_started_at - 2.0, 0.0)
            except Exception:
                return False
        return True

    def _html_quality_report(self, html: str, source: str = "") -> Dict:
        lower = (html or "").lower()
        errors: List[str] = []
        warnings: List[str] = []
        score = 100

        required_tags = [
            ("<!doctype html>", "Missing <!DOCTYPE html>"),
            ("<html", "Missing <html> tag"),
            ("<head", "Missing <head> tag"),
            ("<body", "Missing <body> tag"),
            ("</html>", "Missing </html> closing tag"),
            ("<style", "Missing inline <style> block"),
            ("meta name=\"viewport\"", "Missing mobile viewport meta tag"),
        ]
        for token, message in required_tags:
            if token not in lower:
                errors.append(message)
                score -= 18

        html_bytes = len((html or "").encode("utf-8"))
        if html_bytes < MIN_COMMERCIAL_HTML_BYTES:
            errors.append(f"HTML output too small ({html_bytes} bytes), likely low-quality")
            score -= 25

        style_blocks = re.findall(r"<style[^>]*>(.*?)</style>", html or "", re.IGNORECASE | re.DOTALL)
        css_text = "\n".join(style_blocks)
        css_rules = css_text.count("{")
        if css_rules < MIN_COMMERCIAL_CSS_RULES:
            warnings.append(f"Too few CSS rules ({css_rules}); design may look basic")
            score -= 12

        semantic_hits = sum(1 for t in ("<header", "<main", "<section", "<footer", "<nav") if t in lower)
        if semantic_hits < MIN_SEMANTIC_BLOCKS:
            warnings.append(f"Low semantic structure ({semantic_hits} sections)")
            score -= 10

        # ── Content-completeness gate: detect empty container tags ──
        content_tags = re.findall(
            r'<(section|div|article|main|p|h[1-6])[^>]*>(.*?)</\1>',
            html or "", re.IGNORECASE | re.DOTALL,
        )
        if content_tags:
            total_containers = len(content_tags)
            empty_containers = sum(
                1 for _, inner in content_tags
                if len(re.sub(r'<[^>]+>|\s+', '', inner).strip()) < 3
            )
            empty_ratio = empty_containers / max(total_containers, 1)
            if empty_ratio > 0.4 and total_containers >= 5:
                errors.append(
                    f"Content completeness failure: {empty_containers}/{total_containers} "
                    f"containers are empty ({empty_ratio:.0%}). Page has blank sections."
                )
                score -= 25
            elif empty_ratio > 0.25 and total_containers >= 5:
                warnings.append(
                    f"Low content fill: {empty_containers}/{total_containers} "
                    f"containers are empty ({empty_ratio:.0%})"
                )
                score -= 10

        if "display:flex" not in lower and "display: flex" not in lower and "display:grid" not in lower and "display: grid" not in lower:
            warnings.append("No flex/grid layout detected")
            score -= 8

        if "@media" not in lower:
            warnings.append("No media query detected (weak responsive support)")
            score -= 8

        emoji_hits = len(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", html or ""))
        if emoji_hits > MAX_EMOJI_GLYPHS:
            errors.append(f"Emoji glyphs detected ({emoji_hits}); replace with inline SVG icons")
            score -= 18

        if "<script" not in lower:
            warnings.append("No JavaScript detected (limited interactivity)")
            score -= 6

        if "<img" in lower and "alt=" not in lower:
            warnings.append("Image tag without alt text detected")
            score -= 6

        passed = not errors and score >= 70
        return {
            "pass": passed,
            "score": max(score, 0),
            "errors": errors,
            "warnings": warnings,
            "source": source,
            "bytes": html_bytes,
            "css_rules": css_rules,
        }

    def _strip_emoji_glyphs(self, html: str) -> str:
        if not html:
            return html
        return re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", html)

    def _validate_builder_quality(self, files_created: List[str], output: str) -> Dict:
        html_path = ""
        preview_artifact = self._select_preview_artifact_for_files(files_created)
        if preview_artifact is not None:
            html_path = str(preview_artifact)
        else:
            for f in files_created:
                if f.endswith(".html") or f.endswith(".htm"):
                    html_path = f
                    break

        html = ""
        if html_path and Path(html_path).exists():
            if is_partial_html_artifact(Path(html_path)) and preview_artifact is None:
                return {
                    "pass": True,
                    "score": 78,
                    "errors": [],
                    "warnings": [
                        "Partial builder artifact saved; waiting for sibling builder before strict preview validation",
                    ],
                    "source": html_path,
                }
            html = Path(html_path).read_text(encoding="utf-8", errors="ignore")
            # Auto-fix common quality issues via post-processing
            task_type = getattr(self, '_current_task_type', 'website')
            fixed = postprocess_html(html, task_type=task_type)
            if fixed != html:
                try:
                    Path(html_path).write_text(fixed, encoding="utf-8")
                    html = fixed
                    logger.info(f"Post-processed HTML: auto-fixed quality issues")
                except Exception as e:
                    logger.warning(f"Post-process write failed: {e}")
            report = self._html_quality_report(html, source=html_path)
            # Avoid wasting retries when the only blocker is emoji glyphs.
            only_emoji_blocker = (
                not report.get("pass")
                and bool(report.get("errors"))
                and all("Emoji glyphs detected" in str(err) for err in report.get("errors", []))
            )
            if only_emoji_blocker:
                sanitized = self._strip_emoji_glyphs(html)
                if sanitized != html:
                    try:
                        Path(html_path).write_text(sanitized, encoding="utf-8")
                        report = self._html_quality_report(sanitized, source=html_path)
                        report.setdefault("warnings", []).append(
                            "Auto-sanitized emoji glyphs to satisfy icon policy"
                        )
                    except Exception as e:
                        logger.warning(f"Emoji auto-sanitize failed: {e}")
        else:
            # Fallback to output text in case file write failed.
            report = self._html_quality_report(output or "", source="output_text")
            report["errors"].append("No saved HTML artifact found for builder output")
            report["pass"] = False
        return report

    def _normalize_generated_path(self, path_str: str) -> str:
        """Normalize tool-returned paths to absolute artifact paths under output dir when possible."""
        try:
            raw = Path(path_str).expanduser()
            normalized = raw.resolve() if raw.is_absolute() else (OUTPUT_DIR / raw).resolve()
            return str(normalized)
        except Exception:
            return path_str

    def _prepare_output_dir_for_run(self):
        """
        Remove stale artifacts before each run so preview fallback cannot reopen old outputs.
        """
        clean_enabled = os.getenv("EVERMIND_CLEAN_OUTPUT_ON_RUN", "1").strip().lower() not in ("0", "false", "no")
        if not clean_enabled:
            return
        removed = 0
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            for item in OUTPUT_DIR.iterdir():
                if item.is_dir() and item.name.startswith("task_"):
                    shutil.rmtree(item, ignore_errors=True)
                    removed += 1
                    continue
                if item.is_file() and item.suffix.lower() in (".html", ".htm", ".css", ".js", ".json"):
                    try:
                        item.unlink()
                        removed += 1
                    except Exception:
                        pass
            if removed:
                logger.info(f"Cleaned stale output artifacts before run: removed={removed}")
        except Exception as e:
            logger.warning(f"prepare_output_dir_for_run failed: {e}")

    # ═══════════════════════════════════════════
    # Main entry point
    # ═══════════════════════════════════════════
    async def run(self, goal: str, model: str = "gpt-5.4", conversation_history: Optional[List[Dict]] = None, difficulty: str = "standard", canonical_context: Optional[Dict[str, Any]] = None) -> Dict:
        """
        Execute a user goal autonomously.
        Returns full execution report.
        conversation_history: optional list of recent {role, content} dicts for context continuity.
        difficulty: 'simple', 'standard', or 'pro' — controls number of workflow nodes.
        canonical_context: optional dict with {task_id, run_id, node_executions: [{id, node_key}]}
            When provided, orchestrator bridges into canonical task/run/NE system.
        """
        self._cancel = False
        self._run_started_at = time.time()
        history = conversation_history or []
        difficulty = difficulty if difficulty in ("simple", "standard", "pro") else "standard"
        self.difficulty = difficulty
        self._reviewer_requeues = 0
        # P0-1: Store canonical context for NE bridging
        self._canonical_ctx = canonical_context
        self._subtask_ne_map = {}

        # Auto-select optimal model for this difficulty level
        model = self._model_for_difficulty(difficulty, model)
        # Classify task type for post-processing and template selection
        self._current_task_type = task_classifier.classify(goal).task_type
        logger.info(f"Orchestrator starting [{difficulty}] type={self._current_task_type} model={model}: {goal[:80]}... (history: {len(history)} msgs)")

        # ── Ensure output directory exists and is writable ──
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._prepare_output_dir_for_run()
        # Add output_dir to AI bridge's allowed_dirs so file_ops plugin can write there
        if hasattr(self.ai_bridge, 'config') and isinstance(self.ai_bridge.config, dict):
            allowed = self.ai_bridge.config.get('allowed_dirs', ['/tmp'])
            if str(OUTPUT_DIR) not in allowed:
                allowed.append(str(OUTPUT_DIR))
                self.ai_bridge.config['allowed_dirs'] = allowed
            self.ai_bridge.config['output_dir'] = str(OUTPUT_DIR)

        await self.emit("orchestrator_start", {"goal": goal, "difficulty": difficulty})

        try:
            # ── Phase 1: PLAN ──
            # Check if we have a custom plan from OpenClaw — if so, skip AI planning
            # and directly use the pre-defined node executions as subtasks.
            use_custom_plan = (
                canonical_context
                and canonical_context.get("is_custom_plan")
                and canonical_context.get("node_executions")
            )

            if use_custom_plan:
                # Convert canonical NE definitions into SubTask instances
                ne_list = canonical_context["node_executions"]
                custom_subtasks = []
                # Map node_key to recognized agent types
                _KEY_TO_AGENT = {
                    "planner": "planner",  # §FIX-1: dedicated planner executor
                    "analyst": "analyst",
                    "builder": "builder",
                    "builder_structure": "builder",
                    "builder_ui": "builder",
                    "builder_copy": "builder",
                    "builder_animation": "builder",
                    "builder_responsive": "builder",
                    "reviewer": "reviewer",
                    "reviewer_design": "reviewer",
                    "reviewer_code": "reviewer",
                    "tester": "tester",
                    "deployer": "deployer",
                    "debugger": "debugger",
                    "scorer": "reviewer",
                }
                for i, ne in enumerate(ne_list):
                    node_key = str(ne.get("node_key", "builder"))
                    node_label = str(ne.get("node_label", node_key))
                    # Map depends_on_keys (node_keys like "planner") to subtask IDs
                    # Subtask IDs are 1-indexed position strings
                    key_to_subtask_id = {}
                    for j, ne2 in enumerate(ne_list):
                        key_to_subtask_id[str(ne2.get("node_key", ""))] = str(j + 1)
                    deps = [
                        key_to_subtask_id.get(d, d)
                        for d in (ne.get("depends_on_keys") or [])
                        if d in key_to_subtask_id
                    ]
                    agent_type = _KEY_TO_AGENT.get(node_key, "builder")
                    prescribed_desc = str(
                        ne.get("input_summary")
                        or ne.get("task")
                        or ne.get("task_description")
                        or ""
                    ).strip()
                    # Preserve planner/Opus-authored task text when available.
                    task_desc = prescribed_desc or self._custom_node_task_desc(agent_type, node_label, goal)
                    custom_subtasks.append(SubTask(
                        id=str(i + 1),
                        agent_type=agent_type,
                        description=task_desc,
                        depends_on=deps,
                    ))
                plan = Plan(goal=goal, subtasks=custom_subtasks, difficulty=difficulty)
                logger.info(
                    f"[Custom Plan] Using OpenClaw plan directly: "
                    f"{len(custom_subtasks)} subtasks "
                    f"[{', '.join(st.agent_type for st in custom_subtasks)}]"
                )
            else:
                plan = await self._plan(goal, model, conversation_history=history, difficulty=difficulty)
            self.active_plan = plan

            if not plan.subtasks:
                return {"success": False, "error": "Failed to create plan", "plan": None}

            await self.emit("plan_created", {
                "subtasks": [{"id": st.id, "agent": st.agent_type, "task": st.description,
                              "depends_on": st.depends_on} for st in plan.subtasks],
                "total": len(plan.subtasks)
            })

            # P0-1: Build subtask → NE mapping after plan is created
            if self._canonical_ctx and self._canonical_ctx.get("node_executions"):
                ne_list = self._canonical_ctx["node_executions"]
                # Match by position: plan subtasks[i] → canonical NEs[i]
                for i, st in enumerate(plan.subtasks):
                    if i < len(ne_list):
                        self._subtask_ne_map[st.id] = ne_list[i]["id"]
                        logger.info(f"[Canonical] Mapped subtask {st.id} ({st.agent_type}) → NE {ne_list[i]['id']}")

            # ── Phase 2-5: EXECUTE loop ──
            result = await self._execute_plan(plan, model)

            # ── Phase 6: REPORT ──
            # Guarantee preview_ready fires if any HTML exists in output
            await self._emit_final_preview()

            report = self._build_report(plan, result)
            await self.emit("orchestrator_complete", report)
            return report

        except Exception as e:
            logger.error(f"Orchestrator error: {e}")
            await self.emit("orchestrator_error", {"error": str(e)})
            return {"success": False, "error": str(e)}

    # ═══════════════════════════════════════════
    # Phase 1: PLAN — AI decomposes the goal
    # ═══════════════════════════════════════════
    def _build_context_summary(self, conversation_history: Optional[List[Dict]] = None) -> str:
        """Build a condensed summary of recent conversation for planner context."""
        history = conversation_history or []
        if not history:
            return ""
        # Take last 8 messages, summarize concisely
        recent = history[-8:]
        lines = []
        for msg in recent:
            role = msg.get("role", "unknown")
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            # Truncate long messages
            if len(content) > 200:
                content = content[:200] + "..."
            if role == "user":
                lines.append(f"User: {content}")
            elif role == "agent":
                lines.append(f"AI: {content}")
        if not lines:
            return ""
        return (
            "\n\n[CONVERSATION CONTEXT — Previous interactions for reference]\n"
            + "\n".join(lines)
            + "\n[END CONTEXT]\n"
        )

    def _planner_prompt_for_difficulty(self, goal: str, difficulty: str) -> str:
        """Generate planner prompt tailored to difficulty level."""
        asset_pipeline_enabled = self._asset_pipeline_enabled_for_goal(goal)
        asset_goal = self._goal_wants_generated_assets(goal)
        capability_rules = ""
        if asset_pipeline_enabled:
            capability_rules = (
                "- This goal is asset-heavy and a real image backend is configured. "
                "You SHOULD insert imagegen, spritesheet, and assetimport ahead of builders when that will materially improve the result.\n"
                "- Those nodes must produce concrete art outputs, manifests, or integration-ready handoffs. Do NOT add them as decorative filler.\n"
            )
        elif asset_goal:
            capability_rules = (
                "- This goal would benefit from generated art, but NO configured image-generation backend is available.\n"
                "- Do NOT add imagegen, spritesheet, or assetimport just to look sophisticated. "
                "Instead instruct builders to use high-quality placeholders, SVG/pixel stand-ins, and a clear asset manifest.\n"
            )
        game_research_rules = (
            "- For game goals, analyst research must prioritize GitHub repos, source code, tutorials, docs, devlogs, postmortems, and implementation writeups.\n"
            "- For game goals, do NOT send analyst to spend time playing browser games or wandering through playable portals unless the user explicitly asked for that.\n"
        )
        base_rules = (
            "You are a task planner. Output ONLY a valid JSON object, no other text.\n\n"
            "ABSOLUTE RULES (MUST follow):\n"
            "- Builder task MUST match the user's requested product type (website/game/dashboard/tool/presentation/creative) while producing a single self-contained index.html.\n"
            "- Builder must save final output to /tmp/evermind_output/index.html via file_ops write (or provide full HTML fallback).\n"
            "- You MUST NOT mention GitHub Pages, Netlify, Vercel, or any cloud deployment.\n"
            "- You MUST NOT ask the tester to check public URLs.\n\n"
            + capability_rules
            + game_research_rules
        )

        if difficulty == "simple":
            return base_rules + (
                "Keep the plan to 2-3 subtasks (fast mode).\n"
                "Only expand beyond 3 subtasks if a specialized node is clearly necessary for output quality.\n"
                "Use 3 subtasks by default unless the goal is trivial.\n\n"
                "Output format:\n"
                '{"subtasks": [\n'
                '  {"id": "1", "agent": "builder", "task": "...", "depends_on": []},\n'
                '  {"id": "2", "agent": "deployer", "task": "Confirm files are saved and provide preview URL http://127.0.0.1:8765/preview/", "depends_on": ["1"]},\n'
                '  {"id": "3", "agent": "tester", "task": "Verify the HTML file exists, open preview, and validate visual completeness", "depends_on": ["2"]}\n'
                "]}\n"
            )
        elif difficulty == "pro":
            return base_rules + (
                "Keep the plan to 7 subtasks by default (advanced mode with parallel builders).\n"
                "Pro mode MUST have 2 builders.\n"
                "Asset-heavy goals with a configured image backend may expand beyond 7 subtasks to add imagegen/spritesheet/assetimport before the builders.\n"
                "REQUIRED structure for pro mode:\n"
                "- #1 analyst → research and design brief\n"
                "- #2 builder → build FIRST HALF (header, hero, features) — depends on #1\n"
                "- #3 builder → build SECOND HALF (content, pricing, footer) — depends on #1 (PARALLEL with #2!)\n"
                "- #4 reviewer → open browser, take screenshots, output APPROVED or REJECTED with detailed reasons (depends on #2, #3)\n"
                "- #5 deployer → confirm files and preview URL (depends on #2, #3)\n"
                "- #6 tester → full visual test (depends on #4, #5)\n"
                "- #7 debugger → fix issues from reviewer/tester (depends on #6)\n\n"
                "CRITICAL: Both builders (#2 and #3) MUST depend on #1 ONLY. This makes them run in PARALLEL.\n"
                "For website tasks, each builder saves to a SEPARATE file (part1.html, part2.html). "
                "Evermind assembles index.html preview automatically before reviewer/tester.\n"
                "Planner MUST assign DISTINCT, non-overlapping sections to each builder.\n"
                "Reviewer MUST open the browser preview, take screenshots, and output APPROVED or REJECTED.\n\n"
                "Output format:\n"
                '{"subtasks": [\n'
                '  {"id": "1", "agent": "analyst", "task": "Research design references", "depends_on": []},\n'
                '  {"id": "2", "agent": "builder", "task": "Build header, hero section, features grid. Save to /tmp/evermind_output/index_part1.html", "depends_on": ["1"]},\n'
                '  {"id": "3", "agent": "builder", "task": "Build testimonials, pricing, footer. Save to /tmp/evermind_output/index_part2.html", "depends_on": ["1"]},\n'
                '  {"id": "4", "agent": "reviewer", "task": "Open http://127.0.0.1:8765/preview/ in browser, take screenshots, check quality. Output APPROVED or REJECTED with reasons.", "depends_on": ["2","3"]},\n'
                '  {"id": "5", "agent": "deployer", "task": "Confirm files and preview URL", "depends_on": ["2","3"]},\n'
                '  {"id": "6", "agent": "tester", "task": "Full browser visual test", "depends_on": ["4","5"]},\n'
                '  {"id": "7", "agent": "debugger", "task": "Fix issues found by reviewer/tester", "depends_on": ["6"]}\n'
                "]}\n"
            )
        else:  # standard
            return base_rules + (
                "Keep the plan to 3-4 subtasks by default (balanced mode).\n"
                "Asset-heavy goals with a configured image backend may add analyst/imagegen/spritesheet/assetimport when those nodes clearly improve quality.\n"
                "Use 4 subtasks by default.\n"
                "Reviewer MUST open the browser preview, take screenshots, and output APPROVED or REJECTED with reasons.\n\n"
                "Output format:\n"
                '{"subtasks": [\n'
                '  {"id": "1", "agent": "builder", "task": "...", "depends_on": []},\n'
                '  {"id": "2", "agent": "reviewer", "task": "Open http://127.0.0.1:8765/preview/ in browser, take screenshots. Check layout, responsiveness, visual polish. Output: APPROVED or REJECTED with reasons.", "depends_on": ["1"]},\n'
                '  {"id": "3", "agent": "deployer", "task": "Confirm files are saved and provide preview URL http://127.0.0.1:8765/preview/", "depends_on": ["1"]},\n'
                '  {"id": "4", "agent": "tester", "task": "Verify HTML validity and preview visual quality", "depends_on": ["3"]}\n'
                "]}\n"
            )

    def _fallback_plan_for_difficulty(self, goal: str, difficulty: str) -> List:
        """Generate fallback subtasks when AI planning fails."""
        profile = task_classifier.classify(goal)
        builder_note = self._builder_asset_pipeline_note(goal)
        builder_desc = self._builder_task_description(goal)
        if builder_note:
            builder_desc = f"{builder_desc} {builder_note}"
        builder = SubTask(id="1", agent_type="builder", description=builder_desc)
        deployer_desc = "List generated files and provide local preview URL http://127.0.0.1:8765/preview/"
        tester_desc = profile.tester_hint
        asset_pipeline_enabled = self._asset_pipeline_enabled_for_goal(goal)

        if difficulty == "simple":
            return [
                builder,
                SubTask(id="2", agent_type="deployer", description=deployer_desc, depends_on=["1"]),
                SubTask(id="3", agent_type="tester", description=tester_desc, depends_on=["2"]),
            ]
        elif difficulty == "pro":
            if asset_pipeline_enabled:
                focus_1, focus_2 = self._pro_builder_focus(goal)
                imagegen_desc, spritesheet_desc, assetimport_desc = self._asset_pipeline_descriptions(goal)
                return [
                    SubTask(id="1", agent_type="analyst", description=f"ADVANCED MODE — {task_classifier.analyst_description(goal)}", depends_on=[]),
                    SubTask(id="2", agent_type="imagegen", description=imagegen_desc, depends_on=["1"]),
                    SubTask(id="3", agent_type="spritesheet", description=spritesheet_desc, depends_on=["1", "2"]),
                    SubTask(id="4", agent_type="assetimport", description=assetimport_desc, depends_on=["1", "2", "3"]),
                    SubTask(id="5", agent_type="builder", description=(
                        f"ADVANCED MODE — Use analyst notes and asset manifest.\n"
                        f"{builder_desc}\n{focus_1}"
                    ), depends_on=["1", "4"]),
                    SubTask(id="6", agent_type="builder", description=(
                        f"ADVANCED MODE — Use analyst notes and asset manifest.\n"
                        f"{builder_desc}\n{focus_2}"
                    ), depends_on=["1", "4"]),
                    SubTask(id="7", agent_type="reviewer", description=self._reviewer_task_description(goal, pro=True), depends_on=["5", "6"]),
                    SubTask(id="8", agent_type="deployer", description=deployer_desc, depends_on=["5", "6"]),
                    SubTask(id="9", agent_type="tester", description=tester_desc, depends_on=["7", "8"]),
                    SubTask(id="10", agent_type="debugger", description=(
                        "Fix any issues from reviewer/tester. Read and fix /tmp/evermind_output/index.html."
                    ), depends_on=["9"]),
                ]
            focus_1, focus_2 = self._pro_builder_focus(goal)
            builder3_deps = ["1"]  # ALWAYS parallel
            return [
                SubTask(id="1", agent_type="analyst", description=(
                    f"ADVANCED MODE — {task_classifier.analyst_description(goal)}"
                ), depends_on=[]),
                SubTask(id="2", agent_type="builder", description=(
                    f"ADVANCED MODE — Use analyst notes and build.\n"
                    f"{builder_desc}\n{focus_1}"
                ), depends_on=["1"]),
                SubTask(id="3", agent_type="builder", description=(
                    f"ADVANCED MODE — Use analyst notes and build.\n"
                    f"{builder_desc}\n{focus_2}"
                ), depends_on=builder3_deps),
                SubTask(id="4", agent_type="reviewer", description=self._reviewer_task_description(goal, pro=True), depends_on=["2", "3"]),
                SubTask(id="5", agent_type="deployer", description=deployer_desc, depends_on=["2", "3"]),
                SubTask(id="6", agent_type="tester", description=tester_desc, depends_on=["4", "5"]),
                SubTask(id="7", agent_type="debugger", description=(
                    "Fix any issues from reviewer/tester. Read and fix /tmp/evermind_output/index.html."
                ), depends_on=["6"]),
            ]
        else:  # standard
            if asset_pipeline_enabled:
                imagegen_desc, spritesheet_desc, assetimport_desc = self._asset_pipeline_descriptions(goal)
                return [
                    SubTask(id="1", agent_type="analyst", description=task_classifier.analyst_description(goal), depends_on=[]),
                    SubTask(id="2", agent_type="imagegen", description=imagegen_desc, depends_on=["1"]),
                    SubTask(id="3", agent_type="spritesheet", description=spritesheet_desc, depends_on=["1", "2"]),
                    SubTask(id="4", agent_type="assetimport", description=assetimport_desc, depends_on=["1", "2", "3"]),
                    SubTask(id="5", agent_type="builder", description=builder_desc, depends_on=["1", "4"]),
                    SubTask(id="6", agent_type="reviewer", description=self._reviewer_task_description(goal, pro=False), depends_on=["5"]),
                    SubTask(id="7", agent_type="deployer", description=deployer_desc, depends_on=["5"]),
                    SubTask(id="8", agent_type="tester", description=tester_desc, depends_on=["6", "7"]),
                ]
            return [
                builder,
                SubTask(id="2", agent_type="reviewer", description=self._reviewer_task_description(goal, pro=False), depends_on=["1"]),
                SubTask(id="3", agent_type="deployer", description=deployer_desc, depends_on=["1"]),
                SubTask(id="4", agent_type="tester", description=tester_desc, depends_on=["3"]),
            ]

    def _enforce_plan_shape(self, plan: Plan, goal: str, difficulty: str):
        """
        Keep plan topology stable across models to improve speed, token cost, and reliability.
        """
        profile = task_classifier.classify(goal)
        focus_1, focus_2 = self._pro_builder_focus(goal)
        is_website = profile.task_type == "website"
        asset_pipeline_enabled = self._asset_pipeline_enabled_for_goal(goal)
        builder_desc_base = self._builder_task_description(goal)
        builder_note = self._builder_asset_pipeline_note(goal)
        if builder_note:
            builder_desc_base = f"{builder_desc_base} {builder_note}"
        specialized_agents = {"scribe", "imagegen", "uidesign", "spritesheet", "assetimport"}
        core_agents = {"analyst", "builder", "reviewer", "deployer", "tester", "debugger"}

        if any(st.agent_type in specialized_agents for st in plan.subtasks) and not any(
            st.agent_type in core_agents for st in plan.subtasks
        ):
            for idx, st in enumerate(plan.subtasks, start=1):
                if not str(st.id or "").strip():
                    st.id = str(idx)
                if not str(st.description or "").strip():
                    label = (st.agent_type or "node").replace("_", " ").title()
                    st.description = self._custom_node_task_desc(st.agent_type, label, goal)
            return

        if difficulty == "pro":
            if asset_pipeline_enabled:
                imagegen_desc, spritesheet_desc, assetimport_desc = self._asset_pipeline_descriptions(goal)
                plan.subtasks = [
                    SubTask(id="1", agent_type="analyst", description=task_classifier.analyst_description(goal), depends_on=[]),
                    SubTask(id="2", agent_type="imagegen", description=imagegen_desc, depends_on=["1"]),
                    SubTask(id="3", agent_type="spritesheet", description=spritesheet_desc, depends_on=["1", "2"]),
                    SubTask(id="4", agent_type="assetimport", description=assetimport_desc, depends_on=["1", "2", "3"]),
                    SubTask(
                        id="5",
                        agent_type="builder",
                        description=(
                            "ADVANCED MODE — Use analyst notes and asset manifest.\n"
                            f"{builder_desc_base}\n{focus_1}"
                        ),
                        depends_on=["1", "4"],
                    ),
                    SubTask(
                        id="6",
                        agent_type="builder",
                        description=(
                            "ADVANCED MODE — Use analyst notes and asset manifest.\n"
                            f"{builder_desc_base}\n{focus_2}"
                        ),
                        depends_on=["1", "4"],
                    ),
                    SubTask(id="7", agent_type="reviewer", description=self._reviewer_task_description(goal, pro=True), depends_on=["5", "6"]),
                    SubTask(id="8", agent_type="deployer", description="List generated files and provide local preview URL http://127.0.0.1:8765/preview/", depends_on=["5", "6"]),
                    SubTask(id="9", agent_type="tester", description=profile.tester_hint, depends_on=["7", "8"]),
                    SubTask(
                        id="10",
                        agent_type="debugger",
                        description=(
                            "Fix any issues found by reviewer/tester. "
                            + ("Refine the assembled index.html generated from parallel builder parts if needed. " if is_website else "")
                            + "Use file_ops read to check /tmp/evermind_output/index.html, "
                            "then file_ops write to save the corrected version. "
                            "If no issues were found, confirm everything is good."
                        ),
                        depends_on=["9"],
                    ),
                ]
                return
            analyst_desc = ""
            for st in plan.subtasks:
                if st.agent_type == "analyst" and st.description.strip() and not analyst_desc:
                    analyst_desc = st.description.strip()

            if not analyst_desc:
                analyst_desc = task_classifier.analyst_description(goal)
            builder_desc = (
                f"ADVANCED MODE — Use analyst notes and build.\n"
                f"{builder_desc_base}\n"
                f"Extra pro requirements for {profile.task_type}: advanced polish, "
                "smooth transitions, and attention to detail."
            )

            # ALL task types: 2 builders in pro mode — ALWAYS parallel.
            # Both builders depend only on analyst (#1); website outputs are assembled later.
            focus_1, focus_2 = self._pro_builder_focus(goal)
            builder3_deps = ["1"]  # ALWAYS parallel — both depend on analyst only

            plan.subtasks = [
                SubTask(id="1", agent_type="analyst", description=analyst_desc, depends_on=[]),
                SubTask(
                    id="2",
                    agent_type="builder",
                    description=f"{builder_desc}\n{focus_1}",
                    depends_on=["1"],
                ),
                SubTask(
                    id="3",
                    agent_type="builder",
                    description=f"{builder_desc}\n{focus_2}",
                    depends_on=builder3_deps,
                ),
                SubTask(
                    id="4",
                    agent_type="reviewer",
                    description=self._reviewer_task_description(goal, pro=True),
                    depends_on=["2", "3"],
                ),
                SubTask(
                    id="5",
                    agent_type="deployer",
                    description="List generated files and provide local preview URL http://127.0.0.1:8765/preview/",
                    depends_on=["2", "3"],
                ),
                SubTask(
                    id="6",
                    agent_type="tester",
                    description=profile.tester_hint,
                    depends_on=["4", "5"],
                ),
                SubTask(
                    id="7",
                    agent_type="debugger",
                    description=(
                        "Fix any issues found by reviewer/tester. "
                        + ("Refine the assembled index.html generated from parallel builder parts if needed. " if is_website else "")
                        + "Use file_ops read to check /tmp/evermind_output/index.html, "
                        "then file_ops write to save the corrected version. "
                        "If no issues were found, confirm everything is good."
                    ),
                    depends_on=["6"],
                ),
            ]
            return

        if difficulty == "standard":
            if asset_pipeline_enabled:
                imagegen_desc, spritesheet_desc, assetimport_desc = self._asset_pipeline_descriptions(goal)
                plan.subtasks = [
                    SubTask(id="1", agent_type="analyst", description=task_classifier.analyst_description(goal), depends_on=[]),
                    SubTask(id="2", agent_type="imagegen", description=imagegen_desc, depends_on=["1"]),
                    SubTask(id="3", agent_type="spritesheet", description=spritesheet_desc, depends_on=["1", "2"]),
                    SubTask(id="4", agent_type="assetimport", description=assetimport_desc, depends_on=["1", "2", "3"]),
                    SubTask(id="5", agent_type="builder", description=builder_desc_base, depends_on=["1", "4"]),
                    SubTask(id="6", agent_type="reviewer", description=self._reviewer_task_description(goal, pro=False), depends_on=["5"]),
                    SubTask(id="7", agent_type="deployer", description="List generated files and provide local preview URL http://127.0.0.1:8765/preview/", depends_on=["5"]),
                    SubTask(id="8", agent_type="tester", description=profile.tester_hint, depends_on=["6", "7"]),
                ]
                return
            builder_desc = builder_desc_base
            plan.subtasks = [
                SubTask(id="1", agent_type="builder", description=builder_desc, depends_on=[]),
                SubTask(id="2", agent_type="reviewer", description=self._reviewer_task_description(goal, pro=False), depends_on=["1"]),
                SubTask(id="3", agent_type="deployer", description="List generated files and provide local preview URL http://127.0.0.1:8765/preview/", depends_on=["1"]),
                SubTask(id="4", agent_type="tester", description=profile.tester_hint, depends_on=["3"]),
            ]
            return

        if difficulty == "simple":
            builder_desc = builder_desc_base
            plan.subtasks = [
                SubTask(id="1", agent_type="builder", description=builder_desc, depends_on=[]),
                SubTask(id="2", agent_type="deployer", description="List generated files and provide local preview URL http://127.0.0.1:8765/preview/", depends_on=["1"]),
                SubTask(id="3", agent_type="tester", description=profile.tester_hint, depends_on=["2"]),
            ]

    async def _plan(self, goal: str, model: str, conversation_history: Optional[List[Dict]] = None, difficulty: str = "standard") -> Plan:
        """Use AI to break down the goal into subtasks."""
        await self.emit("phase_change", {"phase": "planning", "message": f"AI is analyzing the goal ({difficulty} mode)..."})

        # Build context from conversation history
        context_summary = self._build_context_summary(conversation_history)

        planner_node = {
            "type": "router",
            "prompt": self._planner_prompt_for_difficulty(goal, difficulty),
            "model": model
        }

        result = await self.ai_bridge.execute(
            node=planner_node, plugins=[], input_data=f"Goal: {goal}{context_summary}",
            model=model, on_progress=lambda d: self.emit("planning_progress", d)
        )

        plan = Plan(goal=goal, difficulty=difficulty)

        if not result.get("success"):
            # ── Emit error so frontend shows what went wrong ──
            error_msg = result.get("error", "Unknown planning error")
            logger.warning(f"Planning AI call failed, falling back to deterministic plan: {error_msg}")
            await self.emit("planning_fallback", {
                "message": f"规划阶段 AI 调用失败，已切换到稳态回退计划: {error_msg}",
                "reason": error_msg,
            })
            # Still create fallback subtasks with LOCAL execution model
            plan.subtasks = self._fallback_plan_for_difficulty(goal, difficulty)
            # CRITICAL: enforce plan shape even on fallback to ensure pro mode gets 2 builders
            self._enforce_plan_shape(plan, goal, difficulty)
            self._apply_retry_policy(plan)
            plan.status = TaskStatus.IN_PROGRESS
            return plan

        if result.get("success") and result.get("output"):
            try:
                # Extract JSON from response
                raw = result["output"]
                # Try to find JSON in the response
                json_start = raw.find("{")
                json_end = raw.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    parsed = json.loads(raw[json_start:json_end])
                    for st in parsed.get("subtasks", []):
                        plan.subtasks.append(SubTask(
                            id=str(st.get("id", len(plan.subtasks) + 1)),
                            agent_type=st.get("agent", "builder"),
                            description=st.get("task", ""),
                            depends_on=[str(d) for d in st.get("depends_on", [])]
                        ))
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Plan parsing error: {e}")
                plan.subtasks = self._fallback_plan_for_difficulty(goal, difficulty)

        if not plan.subtasks:
            plan.subtasks = self._fallback_plan_for_difficulty(goal, difficulty)

        self._enforce_plan_shape(plan, goal, difficulty)
        self._apply_retry_policy(plan)
        plan.status = TaskStatus.IN_PROGRESS
        return plan

    # ═══════════════════════════════════════════
    # Phase 2-5: EXECUTE with retry loop
    # ═══════════════════════════════════════════
    async def _execute_plan(self, plan: Plan, model: str) -> Dict:
        """Execute all subtasks with dependency resolution and retry on test failure."""
        results = {}
        completed = set()
        succeeded = set()
        failed = set()

        while not self._cancel:
            # Find subtasks ready to execute (all deps satisfied)
            ready = [
                st for st in plan.subtasks
                if st.id not in completed
                and st.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)
                and all(d in succeeded for d in st.depends_on)
            ]

            # ── Graceful degradation for parallel builder failures ──
            # If a downstream task (reviewer/deployer/tester) is blocked because
            # ONE of its parallel builder deps failed, but at least one other builder
            # dep succeeded, allow it to proceed rather than stalling the whole run.
            if not ready:
                for st in plan.subtasks:
                    if st.id in completed or st.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                        continue
                    if st.agent_type not in ("reviewer", "deployer", "tester"):
                        continue
                    if not st.depends_on:
                        continue
                    deps_ok = [d for d in st.depends_on if d in succeeded]
                    deps_fail = [d for d in st.depends_on if d in failed]
                    deps_pending = [d for d in st.depends_on if d not in completed]
                    if deps_pending:
                        continue  # still waiting for running deps
                    if not deps_ok:
                        continue  # ALL deps failed → truly stuck
                    # At least one dep succeeded and the rest failed (no pending)
                    # Check if the failed deps are builders (parallel builder scenario)
                    failed_builders = [
                        d for d in deps_fail
                        if any(s.id == d and s.agent_type == "builder" for s in plan.subtasks)
                    ]
                    preview_ready = self._has_preview_artifact_for_degraded_flow()
                    if failed_builders and deps_ok:
                        if preview_ready:
                            logger.warning(
                                f"Graceful degradation: {st.id} proceeding despite failed builders {failed_builders}; "
                                f"succeeded deps: {deps_ok}"
                            )
                        else:
                            # Still proceed — a succeeded builder means SOME output exists.
                            # Blocking all downstream causes the entire run to stall permanently.
                            logger.warning(
                                f"Graceful degradation: {st.id} proceeding despite failed builders {failed_builders} "
                                f"and missing preview artifact; succeeded deps: {deps_ok}"
                            )
                        ready.append(st)

            if not ready:
                # Mark downstream tasks as failed when any dependency has failed,
                # BUT soft-dependency types (imagegen, spritesheet, assetimport, scribe,
                # uidesign) are treated as optional — their failure does NOT block
                # downstream nodes.  We only block if ALL deps are hard-failed.
                SOFT_AGENT_TYPES = {"imagegen", "spritesheet", "assetimport", "scribe", "uidesign", "bgremove"}
                # Build lookup: subtask_id → agent_type
                _id_to_agent = {s.id: s.agent_type for s in plan.subtasks}

                blocked = []
                newly_ready = []
                for st in plan.subtasks:
                    if st.id in completed or st.status == TaskStatus.CANCELLED:
                        continue
                    failed_deps = [dep for dep in st.depends_on if dep in failed]
                    if not failed_deps:
                        continue
                    # Check if ALL failed deps are soft (optional) types
                    hard_failed = [d for d in failed_deps if _id_to_agent.get(d, "") not in SOFT_AGENT_TYPES]
                    if not hard_failed:
                        # All failed deps are soft — proceed without them
                        logger.info(
                            f"Soft-dep bypass: {st.id} ({st.agent_type}) proceeding despite "
                            f"failed soft deps {failed_deps}"
                        )
                        st.depends_on = [d for d in st.depends_on if d not in failed]
                        newly_ready.append(st)
                        continue
                    # Hard failure — block this node
                    st.status = TaskStatus.FAILED
                    st.error = f"Blocked by failed dependencies: {', '.join(failed_deps)}"
                    results[st.id] = {"success": False, "error": st.error, "blocked_by": failed_deps}
                    completed.add(st.id)
                    failed.add(st.id)
                    blocked.append(st.id)
                    await self.emit("subtask_progress", {
                        "subtask_id": st.id,
                        "stage": "error",
                        "message": st.error,
                    })
                    await self.emit("subtask_complete", {
                        "subtask_id": st.id,
                        "agent": st.agent_type,
                        "success": False,
                        "output_preview": "",
                        "full_output": "",
                        "files_created": [],
                        "error": st.error,
                    })
                if newly_ready:
                    ready.extend(newly_ready)
                if blocked:
                    logger.warning(f"Blocked subtasks due to failed dependencies: {blocked}")
                    if not newly_ready:
                        continue

                # Check if done or stuck
                all_done = all(
                    st.id in completed or st.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)
                    for st in plan.subtasks
                )
                if all_done:
                    break
                # Stuck — dependencies can't be satisfied
                stuck = [st for st in plan.subtasks if st.id not in completed and st.status != TaskStatus.CANCELLED]
                if stuck:
                    stuck_ids = [s.id for s in stuck]
                    logger.warning(f"Stuck subtasks: {stuck_ids}")
                    for st in stuck:
                        st.status = TaskStatus.FAILED
                        if not st.error:
                            st.error = "Execution stalled: no runnable task found while workflow is incomplete."
                        results[st.id] = {"success": False, "error": st.error, "stalled": True}
                        completed.add(st.id)
                        failed.add(st.id)
                        succeeded.discard(st.id)
                        await self.emit("subtask_progress", {
                            "subtask_id": st.id,
                            "stage": "error",
                            "message": st.error[:300],
                        })
                        await self.emit("subtask_complete", {
                            "subtask_id": st.id,
                            "agent": st.agent_type,
                            "success": False,
                            "output_preview": "",
                            "full_output": "",
                            "files_created": [],
                            "error": st.error[:300],
                        })
                    continue
                break

            # Execute ready subtasks (parallel when no deps between them)
            await self.emit("phase_change", {
                "phase": "executing",
                "message": f"Running {len(ready)} subtask(s)...",
                "subtasks": [st.id for st in ready]
            })

            tasks = [self._execute_subtask(st, plan, model, results) for st in ready]
            subtask_results = await asyncio.gather(*tasks, return_exceptions=True)

            for st, result in zip(ready, subtask_results):
                if isinstance(result, Exception):
                    st.error = str(result) or "Unknown subtask exception"
                    logger.warning(
                        f"Subtask {st.id} ({st.agent_type}) threw exception: {st.error[:200]}. "
                        f"Attempting retry ({st.retries}/{st.max_retries})."
                    )
                    # §FIX: Exceptions MUST go through _handle_failure for retry
                    retry_ok = await self._handle_failure(st, plan, model, results)
                    if retry_ok:
                        results[st.id] = {"success": True, "output": st.output, "retried": True}
                        completed.add(st.id)
                        succeeded.add(st.id)
                    else:
                        results[st.id] = {"success": False, "error": st.error}
                        completed.add(st.id)
                        failed.add(st.id)
                elif isinstance(result, dict) and result.get("requeue_requested"):
                    requeue_ids = [str(x) for x in (result.get("requeue_subtasks") or []) if str(x)]
                    if not requeue_ids:
                        requeue_ids = [st.id]
                    logger.info(f"Subtask {st.id} requested requeue for: {requeue_ids}")
                    for requeue_id in requeue_ids:
                        target = next((task for task in plan.subtasks if task.id == requeue_id), None)
                        if target:
                            target.status = TaskStatus.PENDING
                            target.output = ""
                            target.error = ""
                            target.completed_at = 0
                        results.pop(requeue_id, None)
                        completed.discard(requeue_id)
                        succeeded.discard(requeue_id)
                        failed.discard(requeue_id)
                    results[st.id] = {
                        "success": False,
                        "requeue_requested": True,
                        "error": result.get("error", ""),
                    }
                elif result.get("success"):
                    st.status = TaskStatus.COMPLETED
                    st.output = result.get("output", "")
                    st.error = ""
                    st.completed_at = time.time()
                    results[st.id] = result
                    completed.add(st.id)
                    succeeded.add(st.id)
                else:
                    # Failed — attempt retry
                    retry_ok = await self._handle_failure(st, plan, model, results)
                    if retry_ok:
                        results[st.id] = {"success": True, "output": st.output, "retried": True}
                        completed.add(st.id)
                        succeeded.add(st.id)
                    else:
                        results[st.id] = {"success": False, "error": st.error}
                        completed.add(st.id)
                        failed.add(st.id)

            # Check for test failures → trigger retry loop
            for st in plan.subtasks:
                if st.agent_type == "tester" and st.status == TaskStatus.COMPLETED:
                    test_result = self._parse_test_result(st.output)
                    test_status = str(test_result.get("status", "pass")).lower()
                    retryable = bool(test_result.get("retryable", True))
                    test_errors = test_result.get("errors", []) or []
                    logger.info(
                        f"Tester parse: id={st.id} status={test_status} retryable={retryable} "
                        f"errors={str(test_errors)[:180]}"
                    )
                    await self.emit("subtask_progress", {
                        "subtask_id": st.id,
                        "stage": "tester_parse_result",
                        "status": test_status,
                        "retryable": retryable,
                        "errors": test_errors[:3],
                    })
                    if test_status == "fail" and retryable:
                        await self._retry_from_failure(
                            plan,
                            st,
                            test_result,
                            model,
                            results,
                            succeeded,
                            completed,
                            failed,
                        )
                    elif test_status == "fail":
                        reason = str(test_errors[0] if test_errors else test_result.get("suggestion") or "Tester reported failure")
                        st.status = TaskStatus.FAILED
                        st.error = reason[:400]
                        results[st.id] = {
                            "success": False,
                            "error": st.error,
                            "non_retryable": True,
                        }
                        succeeded.discard(st.id)
                        completed.add(st.id)
                        failed.add(st.id)
                        await self.emit("subtask_progress", {
                            "subtask_id": st.id,
                            "stage": "tester_non_retryable_failure",
                            "message": st.error[:300],
                        })

        return results

    async def _execute_subtask(self, subtask: SubTask, plan: Plan, model: str, prev_results: Dict) -> Dict:
        """Execute a single subtask through the appropriate agent."""
        # §SAFETY: Planner nodes must NEVER fail — wrap entire execution in safety net
        if subtask.agent_type == "planner":
            return await self._execute_subtask_planner_safe(subtask, plan, model, prev_results)
        return await self._execute_subtask_inner(subtask, plan, model, prev_results)

    async def _execute_subtask_planner_safe(self, subtask: SubTask, plan: Plan, model: str, prev_results: Dict) -> Dict:
        """Planner wrapper: guarantees success via fallback skeleton on ANY error including timeout."""
        planner_timeout = self._configured_subtask_timeout("planner")  # default 120s
        try:
            # §P0-2: Hard timeout via asyncio.wait_for — guarantees the planner
            # cannot block indefinitely even if the AI model never responds.
            result = await asyncio.wait_for(
                self._execute_subtask_inner(subtask, plan, model, prev_results),
                timeout=planner_timeout,
            )
            if result.get("success"):
                return result
            # Planner returned failure — use fallback
            fallback = self._planner_fallback_skeleton(plan.goal)
            if fallback:
                logger.info(f"[Planner] Safety wrapper recovered failed planner {subtask.id}")
                subtask.status = TaskStatus.COMPLETED
                subtask.output = fallback
                subtask.error = ""
                await self._sync_ne_status(subtask.id, "passed", output_summary=fallback[:200])
                return {"success": True, "output": fallback, "error": "", "tool_results": [], "mode": "planner_safety_fallback"}
            return result
        except asyncio.TimeoutError:
            logger.warning(f"[Planner] Hard timeout ({planner_timeout}s) for subtask {subtask.id} — using fallback skeleton")
            fallback = self._planner_fallback_skeleton(plan.goal)
            subtask.status = TaskStatus.COMPLETED
            subtask.output = fallback or "Planning completed with defaults."
            subtask.error = ""
            await self._sync_ne_status(subtask.id, "passed", output_summary=(fallback or "Planner timeout fallback")[:200])
            return {"success": True, "output": fallback or "Planning completed with defaults.", "error": "", "tool_results": [], "mode": "planner_timeout_fallback"}
        except Exception as e:
            logger.error(f"[Planner] Safety wrapper caught exception for {subtask.id}: {e}")
            fallback = self._planner_fallback_skeleton(plan.goal)
            subtask.status = TaskStatus.COMPLETED
            subtask.output = fallback or "Planning completed with defaults."
            subtask.error = ""
            await self._sync_ne_status(subtask.id, "passed", output_summary=(fallback or "Planner fallback")[:200])
            return {"success": True, "output": fallback or "Planning completed with defaults.", "error": "", "tool_results": [], "mode": "planner_exception_fallback"}

    async def _execute_subtask_inner(self, subtask: SubTask, plan: Plan, model: str, prev_results: Dict) -> Dict:
        """Execute a single subtask through the appropriate agent (inner implementation)."""
        subtask.status = TaskStatus.IN_PROGRESS
        logger.info(
            f"Subtask start: id={subtask.id} agent={subtask.agent_type} retries={subtask.retries} "
            f"task={subtask.description[:140]}"
        )

        await self.emit("subtask_start", {
            "subtask_id": subtask.id,
            "agent": subtask.agent_type,
            "task": subtask.description[:200],
        })
        if subtask.agent_type in ("reviewer", "tester", "deployer"):
            self._materialize_parallel_builder_preview()

        # ── Emit loaded skills for UI visibility ──
        loaded_skills: List[str] = []
        try:
            from agent_skills import resolve_skill_names_for_goal
            goal_text = plan.goal if hasattr(plan, 'goal') else str(subtask.description)
            loaded_skills = resolve_skill_names_for_goal(subtask.agent_type, goal_text)
            if loaded_skills:
                self._update_ne_context(subtask.id, loaded_skills=loaded_skills)
                self._append_ne_activity(
                    subtask.id,
                    f"已加载技能：{', '.join(loaded_skills[:6])}",
                    entry_type="sys",
                )
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "skills_loaded",
                    "skills": loaded_skills,
                })
        except Exception:
            pass

        # P0-1: Sync canonical NE to running + pass input_summary for node detail popup
        await self._sync_ne_status(subtask.id, "running", input_summary=subtask.description[:500])
        self._append_ne_activity(
            subtask.id,
            f"任务说明：{subtask.description[:240]}",
            entry_type="sys",
        )
        await self._emit_ne_progress(subtask.id, progress=5, phase="starting", partial_output=f"Starting {subtask.agent_type}...")

        # Build context from dependency outputs
        context_parts = []
        for dep_id in subtask.depends_on:
            dep_result = prev_results.get(dep_id, {})
            dep_task = next((s for s in plan.subtasks if s.id == dep_id), None)
            if dep_task and dep_result.get("output"):
                dep_output = str(dep_result.get("output", ""))
                if dep_task.agent_type == "analyst" and subtask.agent_type != "analyst":
                    analyst_contract = self._build_analyst_handoff_context(plan, subtask, dep_output)
                    if analyst_contract:
                        context_parts.append(analyst_contract[:MAX_DEP_CONTEXT_CHARS * 2])
                        continue
                context_parts.append(
                    f"[Result from {dep_task.agent_type} #{dep_id}]:\n{dep_output[:MAX_DEP_CONTEXT_CHARS]}"
                )

        context = "\n\n".join(context_parts)
        skill_contract = self._build_skill_contract_block(subtask.agent_type, loaded_skills)
        repo_context = None
        try:
            repo_prompt_source = f"{plan.goal}\n{subtask.description}".strip()
            repo_context = build_repo_context(subtask.agent_type, repo_prompt_source, getattr(self.ai_bridge, "config", None))
        except Exception:
            repo_context = None
        if repo_context and repo_context.get("activity_note"):
            self._append_ne_activity(
                subtask.id,
                str(repo_context.get("activity_note") or "")[:320],
                entry_type="sys",
            )

        # ── Inject execution context ──
        if repo_context:
            repo_root = str(repo_context.get("repo_root") or "").strip()
            verification = repo_context.get("verification_commands") or []
            verification_note = ""
            if isinstance(verification, list) and verification:
                verification_note = f"Suggested verification: {' / '.join(str(item) for item in verification[:3])}\n"
            output_info = (
                "[System Context — Existing Repository Mode]\n"
                f"Workspace repository root: {repo_root}\n"
                "You are modifying an existing codebase in-place.\n"
                "Do NOT default to writing /tmp/evermind_output/index.html unless the task explicitly asks for a new standalone preview artifact.\n"
                "Use file_ops to inspect and edit files inside this repository, preserve the existing structure, and keep edits targeted.\n"
                f"{verification_note}"
            )
        elif subtask.agent_type == "builder":
            # Builder should focus on deterministic local file writes, not preview navigation.
            output_info = (
                f"[System Context]\n"
                f"Output directory: {str(OUTPUT_DIR)}\n"
                f"Files must be written to: {str(OUTPUT_DIR)}/\n"
                f"Use file_ops write for final HTML save.\n"
            )
        else:
            output_info = (
                f"[System Context]\n"
                f"Output directory: {str(OUTPUT_DIR)}\n"
                f"Preview server URL: http://127.0.0.1:{PREVIEW_PORT}/preview/\n"
                f"Files should be written to: {str(OUTPUT_DIR)}/\n"
            )
        input_parts = [subtask.description]
        if skill_contract:
            input_parts.append(skill_contract)
        input_parts.append(output_info)
        if context:
            input_parts.append(context)
        full_input = "\n\n".join(part.strip() for part in input_parts if str(part or "").strip())

        # Create a virtual node for the agent
        agent_node = {
            "type": subtask.agent_type,
            "model": model,
            "id": f"auto_{subtask.id}",
            "name": f"{subtask.agent_type.title()} #{subtask.id}",
        }

        enabled = get_default_plugins_for_node(subtask.agent_type, config=getattr(self.ai_bridge, "config", None))
        plugins = [PluginRegistry.get(p) for p in enabled if PluginRegistry.get(p)]

        last_partial_output = getattr(subtask, "last_partial_output", "") or ""
        browser_actions: List[Dict[str, Any]] = []

        async def on_progress(data):
            nonlocal last_partial_output
            stage = str(data.get("stage") or "").strip().lower()
            source = str(data.get("source") or "").strip().lower()
            preview = str(data.get("preview") or data.get("partial_output") or "").strip()
            if stage == "partial_output" and source == "model" and preview:
                last_partial_output = preview[:4000]
                subtask.last_partial_output = last_partial_output
            if stage == "executing_plugin":
                plugin_name = str(data.get("plugin") or "").strip()
                if plugin_name:
                    self._append_ne_activity(
                        subtask.id,
                        f"工具调用：{plugin_name}",
                        entry_type="sys",
                    )
            if stage == "browser_action":
                browser_action = {
                    "action": str(data.get("action") or "").strip().lower(),
                    "subaction": str(data.get("subaction") or data.get("intent") or "").strip().lower(),
                    "ok": bool(data.get("ok")),
                    "url": str(data.get("url") or "").strip(),
                    "target": str(data.get("target") or "").strip(),
                    "observation": str(data.get("observation") or "").strip(),
                    "state_hash": str(data.get("state_hash") or "").strip(),
                    "previous_state_hash": str(data.get("previous_state_hash") or "").strip(),
                    "state_changed": bool(data.get("state_changed", False)),
                    "keys_count": int(data.get("keys_count", 0) or 0),
                    "console_error_count": int(data.get("console_error_count", 0) or 0),
                    "page_error_count": int(data.get("page_error_count", 0) or 0),
                    "failed_request_count": int(data.get("failed_request_count", 0) or 0),
                }
                browser_actions.append(browser_action)
                line = self._browser_action_log_line(browser_action)
                if line:
                    self._append_ne_activity(
                        subtask.id,
                        line,
                        entry_type="info" if browser_action.get("ok") else "warn",
                    )
            await self.emit("subtask_progress", {"subtask_id": subtask.id, **data})

        try:
            timeout_sec = self._configured_subtask_timeout(subtask.agent_type)
            heartbeat_sec = self._configured_progress_heartbeat()
            start_ts = time.time()

            exec_task = asyncio.create_task(self.ai_bridge.execute(
                node=agent_node,
                plugins=plugins,
                input_data=full_input,
                model=model,
                on_progress=on_progress,
            ))

            result: Dict[str, Any]
            while True:
                done, _pending = await asyncio.wait({exec_task}, timeout=heartbeat_sec)
                if exec_task in done:
                    result = exec_task.result()
                    break

                elapsed = int(time.time() - start_ts)
                # P0-3: Emit canonical progress during heartbeat
                progress_pct = min(90, int(10 + (elapsed / max(timeout_sec, 1)) * 80))
                phase = "正在调用AI模型" if elapsed < 30 else "正在处理中" if elapsed < 60 else "仍在执行中，请耐心等待"
                # §FIX-3: Context-aware partial output per agent type
                partial = self._heartbeat_partial_output(
                    subtask.agent_type, elapsed,
                    loaded_skills=loaded_skills,
                    task_desc=subtask.description[:100],
                    streaming_text=last_partial_output,
                )
                await self._emit_ne_progress(
                    subtask.id,
                    progress=progress_pct,
                    phase=phase,
                    partial_output=partial,
                )
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "waiting_ai",
                    "agent": subtask.agent_type,
                    "elapsed_sec": elapsed,
                    "partial_output": partial,
                    "loaded_skills": loaded_skills,
                })

                if elapsed >= timeout_sec:
                    exec_task.cancel()
                    try:
                        await exec_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                    timeout_msg = (
                        f"{subtask.agent_type} execution timeout after {elapsed}s."
                    )
                    result = {"success": False, "output": last_partial_output or "", "error": timeout_msg, "tool_results": []}
                    # Store partial output for continuation retry
                    subtask.last_partial_output = last_partial_output
                    # §FIX-4: Planner fallback on timeout — generate deterministic skeleton
                    if subtask.agent_type == "planner":
                        fallback = self._planner_fallback_skeleton(plan.goal)
                        if fallback:
                            logger.info(f"[Planner] Timeout fallback generated for subtask {subtask.id}")
                            result = {"success": True, "output": fallback, "error": "", "tool_results": [], "mode": "planner_timeout_fallback"}
                    break

            if not isinstance(result, dict):
                raise TypeError(f"ai_bridge returned non-dict result: {type(result).__name__}")

            full_output = str(result.get("output", ""))
            if not result.get("success"):
                # §FIX-4: Planner fallback on AI failure — don't block the chain
                if subtask.agent_type == "planner":
                    fallback = self._planner_fallback_skeleton(plan.goal)
                    if fallback:
                        err_msg = str(result.get("error") or "")
                        logger.info(f"[Planner] AI failure fallback for subtask {subtask.id}: {err_msg[:100]}")
                        result = {"success": True, "output": fallback, "error": "", "tool_results": [], "mode": "planner_ai_fallback"}
                        full_output = fallback
                        # Sync NE to completed after recovery
                        await self._sync_ne_status(subtask.id, "passed", output_summary=fallback[:200])
                if not result.get("success"):
                    err_msg = str(result.get("error") or "").strip() or "Unknown AI execution error"
                    result["error"] = err_msg
                    subtask.error = err_msg
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "error",
                        "message": err_msg[:500],
                    })
            preview_gate_result: Optional[Dict[str, Any]] = None
            tool_call_stats = result.get("tool_call_stats", {}) if isinstance(result, dict) else {}
            if not isinstance(tool_call_stats, dict):
                tool_call_stats = {}

            # ── Collect files created by tools or extract from text ──
            files_created = []
            tool_results = result.get("tool_results", [])
            if subtask.agent_type == "builder":
                write_calls = 0
                for tr in tool_results:
                    if not isinstance(tr, dict):
                        continue
                    if tr.get("written") or (isinstance(tr.get("data"), dict) and tr.get("data", {}).get("written")):
                        write_calls += 1
                logger.info(f"Builder tool_results: count={len(tool_results)} write_calls={write_calls}")
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "builder_tool_results",
                    "count": len(tool_results),
                    "write_calls": write_calls,
                })

            # Debug: log what tool_results contains
            if subtask.agent_type == "builder":
                logger.info(f"Builder tool_results count={len(tool_results)}: {[{k: v for k, v in tr.items() if k != 'content'} if isinstance(tr, dict) else str(tr)[:100] for tr in tool_results[:10]]}")
                logger.info(f"Builder output_text (first 300): {full_output[:300]}")

            # Detect file writes from tool results.
            # file_ops write returns {"path": ..., "written": True} (flat dict)
            # or may be wrapped as {"data": {"path": ..., "written": True}}
            for tr in tool_results:
                if not isinstance(tr, dict):
                    continue
                # Check flat structure first (file_ops native format)
                if tr.get("written"):
                    fpath = tr.get("path", "")
                    if fpath:
                        files_created.append(self._normalize_generated_path(fpath))
                    continue
                # Check nested data structure
                data = tr.get("data")
                if isinstance(data, dict) and data.get("written"):
                    fpath = data.get("path", "")
                    if fpath:
                        files_created.append(self._normalize_generated_path(fpath))

            # Fallback: extract code blocks from AI text and save as files
            if subtask.agent_type == "builder" and full_output and not files_created:
                files_created = self._extract_and_save_code(full_output, subtask.id)

            # Final fallback for builder: scan output directory for recent HTML.
            # Only enabled during an active run (run_started_at set) to avoid
            # accidentally reusing stale artifacts in isolated/test executions.
            if subtask.agent_type == "builder" and not files_created and self._run_started_at > 0:
                scan_cutoff = self._run_started_at - 5.0
                for html_file in OUTPUT_DIR.rglob("*.html"):
                    try:
                        if html_file.stat().st_mtime >= scan_cutoff:
                            files_created.append(str(html_file))
                    except Exception:
                        pass
                if files_created:
                    logger.info(f"Builder disk scan found: {files_created}")

            if files_created:
                # De-duplicate artifacts while preserving order (tool writes may report same file twice).
                deduped: List[str] = []
                seen_paths = set()
                for f in files_created:
                    normalized = self._normalize_generated_path(f)
                    if normalized in seen_paths:
                        continue
                    seen_paths.add(normalized)
                    deduped.append(normalized)
                files_created = deduped

                html_files = [f for f in files_created if f.endswith(('.html', '.htm'))]
                preview_html = self._select_preview_artifact_for_files(files_created) if html_files else None
                if preview_html:
                    preview_artifact = self._normalize_generated_path(str(preview_html))
                    if preview_artifact not in files_created:
                        files_created.append(preview_artifact)

                logger.info(f"Files from subtask {subtask.id}: {files_created}")
                await self.emit("files_created", {
                    "subtask_id": subtask.id,
                    "files": files_created,
                    "output_dir": str(OUTPUT_DIR),
                })

                # ── Emit preview_ready if HTML files were created ──
                if preview_html:
                    preview_url = build_preview_url_for_file(preview_html, output_dir=OUTPUT_DIR)
                    await self.emit("preview_ready", {
                        "subtask_id": subtask.id,
                        "preview_url": preview_url,
                        "files": files_created,
                        "output_dir": str(OUTPUT_DIR),
                    })
                    # Strong artifact gate: ensure preview target file exists and passes baseline checks.
                    preview_gate_result = validate_html_file(preview_html)
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "preview_validation",
                        "ok": bool(preview_gate_result.get("ok")),
                        "preview_url": preview_url,
                        "errors": preview_gate_result.get("errors", [])[:4],
                        "warnings": preview_gate_result.get("warnings", [])[:4],
                        "score": (preview_gate_result.get("checks") or {}).get("score"),
                    })
                elif html_files and any(is_partial_html_artifact(Path(f)) for f in html_files):
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "preview_waiting_parts",
                        "message": "Parallel builder part saved. Waiting for sibling builder before strict preview validation.",
                    })

            if subtask.agent_type == "builder" and result.get("success"):
                if preview_gate_result is not None and not preview_gate_result.get("ok"):
                    preview_msg = (
                        f"Preview artifact validation failed. "
                        f"Errors: {preview_gate_result.get('errors', [])[:3]}"
                    )
                    result["success"] = False
                    result["error"] = preview_msg
                    subtask.error = preview_msg
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "preview_validation_failed",
                        "message": preview_msg,
                    })

                quality = self._validate_builder_quality(files_created, full_output)
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "quality_gate",
                    "score": quality.get("score"),
                    "errors": quality.get("errors", [])[:5],
                    "warnings": quality.get("warnings", [])[:5],
                })
                if not quality.get("pass"):
                    quality_msg = (
                        f"Builder quality gate failed (score={quality.get('score')}). "
                        f"Errors: {quality.get('errors', [])[:3]}"
                    )
                    # Keep task artifacts for debug, but avoid exposing low-quality root preview.
                    root_index = OUTPUT_DIR / "index.html"
                    if root_index.exists() and str(root_index) in files_created:
                        try:
                            root_index.unlink()
                        except Exception:
                            pass
                    result["success"] = False
                    result["error"] = quality_msg
                    subtask.error = quality_msg
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "quality_gate_failed",
                        "message": quality_msg,
                    })

            if subtask.agent_type == "reviewer" and result.get("success"):
                browser_calls = int(tool_call_stats.get("browser", 0) or 0)
                direct_upstream_builders = {
                    st.id for st in plan.subtasks
                    if st.agent_type == "builder" and st.id in set(subtask.depends_on or [])
                }
                if browser_calls < 1:
                    reviewer_msg = (
                        "Reviewer visual gate failed: browser tool was not used. "
                        "Reviewer must navigate preview and take screenshots."
                    )
                    result["success"] = False
                    result["error"] = reviewer_msg
                    subtask.error = reviewer_msg
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "reviewer_visual_gate_failed",
                        "message": reviewer_msg,
                    })
                else:
                    interaction_error = self._interaction_gate_error(
                        "reviewer",
                        task_classifier.classify(plan.goal).task_type,
                        browser_actions,
                    )
                    reviewer_gate: Dict[str, Any] = {"ok": True, "preview_url": None, "errors": [], "warnings": [], "smoke": {"status": "skipped", "reason": "not_applicable"}}
                    reviewer_gate_ok = True
                    reviewer_gate_errors: List[str] = []
                    reviewer_gate_warnings: List[str] = []
                    reviewer_smoke: Dict[str, Any] = reviewer_gate.get("smoke", {}) or {}
                    if direct_upstream_builders:
                        reviewer_gate = await self._run_reviewer_visual_gate()
                        reviewer_gate_ok = bool(reviewer_gate.get("ok"))
                        reviewer_gate_errors = reviewer_gate.get("errors", []) or []
                        reviewer_gate_warnings = reviewer_gate.get("warnings", []) or []
                        reviewer_smoke = reviewer_gate.get("smoke", {}) or {}
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "reviewer_visual_gate",
                            "ok": reviewer_gate_ok,
                            "preview_url": reviewer_gate.get("preview_url"),
                            "errors": reviewer_gate_errors[:4],
                            "warnings": reviewer_gate_warnings[:4],
                            "smoke_status": reviewer_smoke.get("status", "skipped"),
                        })
                    can_force_reject = bool(direct_upstream_builders) and bool(reviewer_gate.get("preview_url"))
                    if can_force_reject and (interaction_error or not reviewer_gate_ok):
                        forced_output = self._build_reviewer_forced_rejection(
                            interaction_error=interaction_error or "",
                            preview_gate=reviewer_gate,
                        )
                        result["output"] = forced_output
                        full_output = forced_output
                        result["success"] = True
                        result["error"] = ""
                        subtask.error = ""
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "reviewer_forced_rejection",
                            "message": "Reviewer deterministic gate converted the review into a structured REJECTED verdict for builder rework.",
                            "preview_url": reviewer_gate.get("preview_url"),
                            "errors": reviewer_gate_errors[:4],
                            "interaction_error": interaction_error or "",
                        })
                    elif interaction_error:
                        result["success"] = False
                        result["error"] = interaction_error
                        subtask.error = interaction_error
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "reviewer_interaction_gate_failed",
                            "message": interaction_error,
                        })
                    elif not reviewer_gate_ok:
                        gate_msg = (
                            f"Reviewer deterministic gate failed: "
                            f"{(reviewer_gate_errors or ['unknown preview validation failure'])[:2]}"
                        )
                        result["success"] = False
                        result["error"] = gate_msg
                        subtask.error = gate_msg
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "reviewer_visual_gate_failed",
                            "message": gate_msg,
                        })

            if subtask.agent_type == "analyst" and result.get("success"):
                browser_calls = int(tool_call_stats.get("browser", 0) or 0)
                visited_urls = self._collect_reference_urls(full_output, tool_results)
                if visited_urls and not any(url in full_output for url in visited_urls):
                    url_block = "Reference sites visited:\n" + "\n".join(f"- {url}" for url in visited_urls[:5])
                    full_output = f"{url_block}\n\n{full_output}".strip()
                    result["output"] = full_output
                # Soft validation: log missing sections as a warning but do NOT fail
                # the analyst.  _build_analyst_handoff_context already falls back to
                # raw output when tagged sections are absent (L644-651).
                missing_tags = self._validate_analyst_handoff(full_output, plan)
                if missing_tags:
                    logger.info(
                        f"Analyst handoff soft-warning: missing sections {missing_tags[:6]}; "
                        f"downstream will use raw analyst output as fallback."
                    )
                if browser_calls < 1:
                    logger.info(
                        f"Analyst did not perform any browser calls; "
                        f"output may lack real reference data."
                    )

            # ── Reviewer rejection → trigger builder re-run (ALL modes) ──
            if (
                subtask.agent_type == "reviewer"
                and result.get("success")
            ):
                reviewer_output = (result.get("output") or "").strip()
                reviewer_verdict = self._parse_reviewer_verdict(reviewer_output)
                reviewer_rejected = reviewer_verdict == "REJECTED"
                rejection_details = ""

                if reviewer_rejected:
                    # Extract improvement suggestions from reviewer output
                    rejection_details = self._format_reviewer_rework_brief(reviewer_output)
                    upstream_builder_ids = {str(dep) for dep in (subtask.depends_on or [])}
                    builders_to_requeue = [
                        st for st in plan.subtasks
                        if st.agent_type == "builder"
                        and st.status == TaskStatus.COMPLETED
                        and st.id in upstream_builder_ids
                    ]
                    eligible_builders = [
                        st for st in builders_to_requeue
                        if st.retries < st.max_retries
                    ]
                    max_rejections = self._configured_max_reviewer_rejections()
                    can_requeue = (
                        bool(eligible_builders)
                        and self._reviewer_requeues < max_rejections
                    )
                    if can_requeue:
                        self._reviewer_requeues += 1
                        rejection_msg = (
                            f"Reviewer REJECTED the product (round {self._reviewer_requeues}/{max_rejections}). "
                            f"Builder will re-run with reviewer feedback."
                        )
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "reviewer_rejection",
                            "message": rejection_msg,
                            "rejection_round": self._reviewer_requeues,
                            "max_rejections": max_rejections,
                        })
                        for builder_task in eligible_builders:
                            builder_task.status = TaskStatus.PENDING
                            builder_task.retries += 1
                            builder_task.error = (
                                f"Reviewer rejected (round {self._reviewer_requeues}): "
                                f"{rejection_details[:600]}"
                            )
                            builder_task.description = (
                                f"{builder_task.description}\n\n"
                                f"⚠️ REVIEWER REJECTED YOUR OUTPUT (round {self._reviewer_requeues}/{max_rejections}). "
                                f"You MUST fix these issues:\n"
                                f"{rejection_details[:800]}\n\n"
                                f"Review the existing output artifacts, fix the problems, and keep the "
                                f"overall product consistent with the sibling builder output.\n"
                            )
                        # Reset reviewer so it re-checks after builder fixes
                        subtask.status = TaskStatus.PENDING
                        subtask.output = ""
                        subtask.completed_at = 0
                        result["success"] = True
                        result["error"] = rejection_msg
                        result["requeue_requested"] = True
                        result["requeue_subtasks"] = [st.id for st in eligible_builders] + [subtask.id]
                        subtask.error = ""
                        logger.info(
                            "Reviewer rejected — re-running builders %s",
                            [st.id for st in eligible_builders],
                        )
                    else:
                        # Builder exhausted retries — let it pass with warning
                        reason = (
                            f"rejection budget reached ({self._reviewer_requeues}/{max_rejections})"
                            if self._reviewer_requeues >= max_rejections
                            else "builder retries exhausted"
                        )
                        logger.warning(f"Reviewer rejected but cannot requeue ({reason}) — proceeding")
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "reviewer_rejection_no_retry",
                            "message": f"Reviewer rejected but cannot requeue ({reason}), proceeding.",
                        })

            if subtask.agent_type == "tester" and result.get("success"):
                tester_browser_calls = int(tool_call_stats.get("browser", 0) or 0)
                if tester_browser_calls < 1:
                    tester_msg = (
                        "Tester visual gate failed: browser tool was not used. "
                        "Tester must navigate preview and capture screenshots."
                    )
                    result["success"] = False
                    result["error"] = tester_msg
                    subtask.error = tester_msg
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "tester_visual_gate_failed",
                        "message": tester_msg,
                    })
                else:
                    interaction_error = self._interaction_gate_error(
                        "tester",
                        task_classifier.classify(plan.goal).task_type,
                        browser_actions,
                    )
                    if interaction_error:
                        result["success"] = False
                        result["error"] = interaction_error
                        subtask.error = interaction_error
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "tester_interaction_gate_failed",
                            "message": interaction_error,
                        })

            if subtask.agent_type == "tester" and result.get("success"):
                tester_gate = await self._run_tester_visual_gate()
                gate_ok = bool(tester_gate.get("ok"))
                gate_errors = tester_gate.get("errors", []) or []
                gate_warnings = tester_gate.get("warnings", []) or []
                smoke = tester_gate.get("smoke", {}) or {}
                smoke_status = smoke.get("status", "skipped")
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "tester_visual_gate",
                    "ok": gate_ok,
                    "preview_url": tester_gate.get("preview_url"),
                    "errors": gate_errors[:4],
                    "warnings": gate_warnings[:4],
                    "smoke_status": smoke_status,
                })
                gate_note = (
                    f"Deterministic visual gate {'passed' if gate_ok else 'failed'}; "
                    f"smoke={smoke_status}; preview={tester_gate.get('preview_url') or 'n/a'}."
                )
                gate_note += f" __EVERMIND_TESTER_GATE__={'PASS' if gate_ok else 'FAIL'}"
                if not gate_ok:
                    # Keep success=True so downstream parse triggers the structured retry path.
                    # Include a strong fail marker consumed by _parse_test_result.
                    gate_note += f" QUALITY GATE FAILED: {gate_errors[:2]}"
                full_output = (full_output + "\n\n" + gate_note).strip()
                result["output"] = full_output

            # Extract usage/cost from AI result for token monitoring
            usage_data = result.get("usage", {}) or {}
            prompt_tokens = int(usage_data.get("prompt_tokens", 0) or usage_data.get("input_tokens", 0) or 0)
            completion_tokens = int(usage_data.get("completion_tokens", 0) or usage_data.get("output_tokens", 0) or 0)
            total_tokens = int(usage_data.get("total_tokens", 0) or 0) or (prompt_tokens + completion_tokens)
            estimated_cost = float(result.get("cost", 0) or 0)
            # If cost is still 0 and we have tokens, estimate from bridge
            if estimated_cost <= 0 and total_tokens > 0:
                try:
                    estimated_cost = self.ai_bridge._estimate_response_cost(model, usage_data)
                except Exception:
                    estimated_cost = 0.0

            await self.emit("subtask_complete", {
                "subtask_id": subtask.id,
                "agent": subtask.agent_type,
                "success": result.get("success", False),
                "output_preview": full_output[:2000],
                "full_output": full_output,
                "files_created": files_created,
                "error": result.get("error", "") if not result.get("success") else "",
                "tokens_used": total_tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost": estimated_cost,
            })
            reference_urls = self._collect_reference_urls(full_output, result.get("tool_results", []) or [])
            if reference_urls:
                self._update_ne_context(subtask.id, reference_urls=reference_urls)
                self._append_ne_activity(
                    subtask.id,
                    f"浏览器步骤: 参考并访问网站 {', '.join(reference_urls[:4])}",
                    entry_type="info",
                )
            if files_created:
                self._append_ne_activity(
                    subtask.id,
                    f"生成文件：{', '.join(files_created[:6])}",
                    entry_type="ok",
                )
            summary_stub = type("SummaryStub", (), {"output": full_output, "agent_type": subtask.agent_type})()
            for item in self._summarize_node_work(summary_stub)[:6]:
                self._append_ne_activity(subtask.id, item, entry_type="ok")
            # P0-1: Sync canonical NE to terminal state with human-readable summary
            if result.get("success"):
                human_summary = self._humanize_output_summary(
                    subtask.agent_type, full_output, True, files_created
                )
                self._append_ne_activity(
                    subtask.id,
                    f"执行完成：{human_summary}",
                    entry_type="ok",
                )
                await self._sync_ne_status(
                    subtask.id,
                    "passed",
                    output_summary=human_summary,
                    tokens_used=total_tokens,
                    cost=estimated_cost,
                    loaded_skills=loaded_skills,
                    reference_urls=reference_urls,
                )
                await self._emit_ne_progress(subtask.id, progress=100, phase="complete")
            else:
                # §FIX: Do NOT sync NE to 'failed' here — let _handle_failure decide
                # whether to retry or finalize. Only update output_summary/error for display.
                human_summary = self._humanize_output_summary(
                    subtask.agent_type, result.get("error", ""), False
                )
                self._append_ne_activity(
                    subtask.id,
                    f"执行失败：{str(result.get('error', '') or human_summary)[:320]}",
                    entry_type="error",
                )
                # Update NE data without changing status (will be set by _handle_failure or calling code)
                self._update_ne_failure_details(
                    subtask.id,
                    output_summary=human_summary,
                    error_message=result.get("error", ""),
                    tokens_used=total_tokens,
                    cost=estimated_cost,
                )
            logger.info(
                f"Subtask done: id={subtask.id} agent={subtask.agent_type} success={bool(result.get('success'))} "
                f"output_len={len(full_output)} files={len(files_created)} retries={subtask.retries} "
                f"error={(str(result.get('error', ''))[:180] if not result.get('success') else '')}"
            )

            return result
        except Exception as e:
            subtask.error = str(e)
            logger.exception(f"Subtask {subtask.id} crashed: {e}")
            await self.emit("subtask_complete", {
                "subtask_id": subtask.id,
                "agent": subtask.agent_type,
                "success": False,
                "output_preview": "",
                "full_output": "",
                "files_created": [],
                "error": str(e),
            })
            # Keep the node in a retryable state; _handle_failure decides whether this becomes terminal.
            human_summary = self._humanize_output_summary(subtask.agent_type, str(e), False)
            self._update_ne_failure_details(
                subtask.id,
                output_summary=human_summary,
                error_message=str(e),
            )
            return {"success": False, "output": "", "error": str(e), "tool_results": []}

    def _extract_and_save_code(self, output: str, subtask_id: str) -> list:
        """Extract code from AI output and save as files.
        Handles: markdown code blocks, raw HTML without fences.
        Returns list of file paths created."""
        files = []
        task_dir = OUTPUT_DIR / f"task_{subtask_id}"
        task_dir.mkdir(parents=True, exist_ok=True)

        seen_langs = {}  # track duplicates

        # Strategy 1: Extract markdown code blocks
        for match in _CODE_BLOCK_RE.finditer(output):
            lang = (match.group(1) or 'txt').lower()
            code = match.group(2).strip()
            if not code or len(code) < 10:
                continue  # skip tiny snippets
            if lang in ('html', 'htm'):
                code = self._normalize_html_artifact(code)

            base_name = _LANG_FILENAME.get(lang, f'output.{lang}')
            # Handle duplicates
            count = seen_langs.get(base_name, 0)
            seen_langs[base_name] = count + 1
            if count > 0:
                stem, ext = os.path.splitext(base_name)
                base_name = f"{stem}_{count}{ext}"

            filepath = task_dir / base_name
            try:
                filepath.write_text(code, encoding='utf-8')
                files.append(str(filepath))
                logger.info(f"Saved code to {filepath} ({len(code)} chars)")

                # Also save HTML to root output dir for easy preview access
                if lang in ('html', 'htm') and base_name == 'index.html':
                    root_copy = OUTPUT_DIR / 'index.html'
                    root_copy.write_text(code, encoding='utf-8')
                    files.append(str(root_copy))
                    logger.info(f"Also saved to root: {root_copy}")
            except Exception as e:
                logger.error(f"Failed to save {filepath}: {e}")

        # Strategy 2: If no HTML code block found, look for raw HTML in output
        has_html = any(f.endswith('.html') for f in files)
        if not has_html and ('<!DOCTYPE' in output or '<html' in output):
            # Try to extract the HTML portion
            html_start = output.find('<!DOCTYPE')
            if html_start < 0:
                html_start = output.find('<html')
            html_end = output.rfind('</html>')
            if html_start >= 0:
                if html_end > html_start:
                    html_code = output[html_start:html_end + 7]
                else:
                    html_code = output[html_start:].rstrip()
                html_code = self._normalize_html_artifact(html_code)
                if len(html_code) > 50:
                    filepath = task_dir / 'index.html'
                    try:
                        filepath.write_text(html_code, encoding='utf-8')
                        files.append(str(filepath))
                        # Also save to root
                        root_copy = OUTPUT_DIR / 'index.html'
                        root_copy.write_text(html_code, encoding='utf-8')
                        files.append(str(root_copy))
                        logger.info(f"Extracted raw HTML: {filepath} ({len(html_code)} chars)")
                    except Exception as e:
                        logger.error(f"Failed to save extracted HTML: {e}")

        return files

    def _collect_upstream_repair_targets(self, plan: Plan, test_task: SubTask) -> List[SubTask]:
        """
        Find nearest upstream builder/debugger tasks, even when tester depends on deployer.
        """
        task_map = {st.id: st for st in plan.subtasks}
        queue: List[tuple[str, int]] = [(dep, 0) for dep in test_task.depends_on]
        visited = set()
        scored: Dict[str, int] = {}

        while queue:
            cur_id, depth = queue.pop(0)
            if cur_id in visited:
                continue
            visited.add(cur_id)
            cur = task_map.get(cur_id)
            if not cur:
                continue
            if cur.agent_type in ("builder", "debugger"):
                best = scored.get(cur.id)
                if best is None or depth < best:
                    scored[cur.id] = depth
            for upstream in cur.depends_on:
                queue.append((upstream, depth + 1))

        if not scored:
            # Last fallback: include any builder/debugger in plan.
            return [st for st in plan.subtasks if st.agent_type in ("builder", "debugger")]

        ordered_ids = sorted(scored.keys(), key=lambda sid: (scored[sid], sid))
        return [task_map[sid] for sid in ordered_ids if sid in task_map]

    async def _run_preview_visual_gate(self, *, run_smoke: bool, gate_name: str) -> Dict[str, Any]:
        """Shared deterministic preview validation for reviewer/tester stages."""
        self._materialize_parallel_builder_preview()
        task_id, html_file = latest_preview_artifact(OUTPUT_DIR)
        if not html_file:
            fallback_candidates = []
            fallback_root = OUTPUT_DIR / "index.html"
            fallback_candidates.append(fallback_root)
            try:
                resolved_root = OUTPUT_DIR.resolve() / "index.html"
                fallback_candidates.append(resolved_root)
            except Exception:
                pass
            for candidate in fallback_candidates:
                try:
                    if candidate.exists() and candidate.is_file():
                        html_file = candidate
                        task_id = "root"
                        logger.warning(
                            f"{gate_name} visual gate artifact fallback hit: using {candidate}"
                        )
                        break
                except Exception:
                    continue
        if not html_file:
            return {
                "ok": False,
                "errors": ["No HTML preview artifact found for tester validation"],
                "warnings": [],
                "preview_url": None,
                "smoke": {"status": "skipped", "reason": "no_artifact"},
                "task_id": task_id,
            }
        preview_url = build_preview_url_for_file(html_file, output_dir=OUTPUT_DIR)
        result = await validate_preview(preview_url, run_smoke=run_smoke)
        result["task_id"] = task_id
        return result

    async def _run_tester_visual_gate(self) -> Dict[str, Any]:
        """
        Deterministic post-check for tester stage so visual validation is not purely model-text based.
        """
        return await self._run_preview_visual_gate(
            run_smoke=self._configured_tester_smoke(),
            gate_name="Tester",
        )

    async def _run_reviewer_visual_gate(self) -> Dict[str, Any]:
        """
        Deterministic post-check for reviewer stage so blank/white previews become structured rejections.
        """
        return await self._run_preview_visual_gate(
            run_smoke=self._configured_reviewer_smoke(),
            gate_name="Reviewer",
        )

    async def _emit_final_preview(self):
        """
        Guaranteed preview_ready emission at end of run().
        Scans output dir for HTML files created/updated in this run and emits preview URL if found.
        Fixes: builder retry / extraction might not emit preview_ready,
        leaving the frontend stuck on "暂无预览".
        """
        try:
            if not OUTPUT_DIR.exists():
                return

            self._materialize_parallel_builder_preview()

            # Prefer run-local HTML artifacts to avoid opening stale previews from previous runs.
            cutoff = max(self._run_started_at - 2.0, 0.0)
            run_local: List[tuple[float, Path]] = []
            for html in OUTPUT_DIR.rglob("*"):
                if not html.is_file() or html.suffix.lower() not in (".html", ".htm"):
                    continue
                if is_partial_html_artifact(html):
                    continue
                try:
                    mtime = html.stat().st_mtime
                except Exception:
                    continue
                if mtime >= cutoff:
                    run_local.append((mtime, html))

            if not run_local:
                logger.info("Final preview scan: no run-local HTML artifacts found")
                return

            run_local.sort(key=lambda item: item[0], reverse=True)
            latest_html = run_local[0][1]
            preview_url = build_preview_url_for_file(latest_html, output_dir=OUTPUT_DIR)
            logger.info(f"Final preview_ready: {preview_url}")
            await self.emit("preview_ready", {
                "preview_url": preview_url,
                "files": [str(path) for _, path in run_local[:20]],
                "output_dir": str(OUTPUT_DIR),
                "final": True,
            })
        except Exception as e:
            logger.warning(f"_emit_final_preview error: {e}")

    # ═══════════════════════════════════════════
    # Retry Logic — the key differentiator
    # ═══════════════════════════════════════════
    async def _handle_failure(self, subtask: SubTask, plan: Plan, model: str, results: Dict) -> bool:
        """Handle a failed subtask — retry with error context."""
        if subtask.retries >= subtask.max_retries:
            logger.warning(f"Subtask {subtask.id} exceeded max retries ({subtask.max_retries})")
            subtask.status = TaskStatus.FAILED
            # §FIX: Only sync NE to failed after ALL retries exhausted
            err_msg = f"All {subtask.max_retries} retries exhausted. Last error: {(subtask.error or 'Unknown')[:200]}"
            await self._sync_ne_status(subtask.id, "failed", error_message=err_msg)
            await self.emit("subtask_progress", {
                "subtask_id": subtask.id,
                "stage": "error",
                "message": err_msg,
            })
            return False

        subtask.retries += 1
        plan.total_retries += 1
        subtask.status = TaskStatus.RETRYING
        retry_error = (subtask.error or "Unknown error").strip() or "Unknown error"
        logger.info(
            f"Retrying subtask: id={subtask.id} agent={subtask.agent_type} "
            f"attempt={subtask.retries}/{subtask.max_retries} model={model} error={retry_error[:200]}"
        )

        # §FIX: Reset NE to running so frontend shows "retrying" not "failed"
        await self._sync_ne_status(
            subtask.id, "running",
            input_summary=f"Retry {subtask.retries}/{subtask.max_retries}: {subtask.description[:200]}",
            progress=5,
            phase="retrying",
        )

        await self.emit("subtask_retry", {
            "subtask_id": subtask.id,
            "retry": subtask.retries,
            "max_retries": subtask.max_retries,
            "error": retry_error[:200]
        })
        await self.emit("subtask_progress", {
            "subtask_id": subtask.id,
            "stage": "error",
            "message": f"Retry {subtask.retries}/{subtask.max_retries}: {retry_error[:300]}",
        })

        # Re-execute with error context — smart retry per agent type
        if subtask.agent_type == "builder":
            is_timeout = "timeout" in str(subtask.error or "").lower()
            partial_output = getattr(subtask, 'last_partial_output', '') or ''
            if is_timeout and subtask.retries == 1 and len(partial_output) > 100:
                # First retry after timeout WITH partial output: store it internally
                # but do NOT dump raw code into the task description (UI shows this).
                # Save partial to a temp file for the builder to read.
                partial_file = Path("/tmp/evermind_output/_partial_builder.html")
                try:
                    partial_file.parent.mkdir(parents=True, exist_ok=True)
                    partial_file.write_text(partial_output[:8000], encoding="utf-8")
                    saved_partial = True
                except Exception:
                    saved_partial = False
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ 上次执行因超时中断，但已有部分代码产出。\n"
                    + (f"部分产出已保存在 /tmp/evermind_output/_partial_builder.html，请用 file_ops read 读取它。\n" if saved_partial else "")
                    + "请在已有代码基础上继续完成，不要从零开始。\n"
                    "完成后用 file_ops write 保存完整文件到 /tmp/evermind_output/index.html。\n"
                )
            elif is_timeout and subtask.retries == 1:
                # First retry after timeout: drastically simplified prompt
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ PREVIOUS ATTEMPT TIMED OUT. THIS TIME:\n"
                    "- Write MAX 150 lines total\n"
                    "- Skip fancy animations, keep CSS minimal\n"
                    "- Focus on WORKING code, not perfection\n"
                    "- Call file_ops write IMMEDIATELY with your HTML\n"
                    "- Do NOT overthink — just output the code NOW\n"
                )
            elif is_timeout and subtask.retries >= 2:
                # Second retry: absolute minimum
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ TIMED OUT TWICE. Write absolute minimum version:\n"
                    "- MAX 100 lines. Bare bones but functional.\n"
                    "- Call file_ops write to /tmp/evermind_output/index.html NOW.\n"
                )
            else:
                # Non-timeout error retry
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    f"⚠️ PREVIOUS ERROR: {subtask.error}\n"
                    "Fix the issue. Save to /tmp/evermind_output/index.html via file_ops write.\n"
                )
        elif subtask.agent_type == "analyst":
            analyst_error = str(subtask.error or "").lower()
            if "timeout" in analyst_error:
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ PREVIOUS ATTEMPT TIMED OUT — DO NOT USE BROWSER THIS TIME.\n"
                    "Use your built-in knowledge to provide a design brief.\n"
                    "Do NOT call browser tool. Do NOT visit any websites.\n"
                    "Just write your analysis and recommendations based on what you already know.\n"
                    "Be concise — output a SHORT design brief (under 500 words).\n"
                )
            else:
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    f"⚠️ PREVIOUS ERROR: {subtask.error}\n"
                    "This retry MUST use the browser tool on at least 2 different live URLs.\n"
                    "If one site blocks you, skip it and visit another.\n"
                    "Your final report MUST include ALL required XML tags for downstream handoff:\n"
                    "<reference_sites>, <design_direction>, <non_negotiables>, <deliverables_contract>, <risk_register>, "
                    "<builder_1_handoff>, <builder_2_handoff>, <reviewer_handoff>, "
                    "<tester_handoff>, <debugger_handoff>.\n"
                    "Do NOT finish after a single site. Do NOT return freeform prose only.\n"
                )
        else:
            enhanced_input = (
                f"{subtask.description}\n\n"
                f"⚠️ PREVIOUS ATTEMPT FAILED (retry {subtask.retries}/{subtask.max_retries}):\n"
                f"Error: {subtask.error}\n\n"
                f"Please fix the issue and try again. Be more careful and faster this time."
            )

        agent_node = {"type": subtask.agent_type, "model": model, "id": f"auto_{subtask.id}_r{subtask.retries}",
                      "name": f"{subtask.agent_type.title()} #{subtask.id} (retry {subtask.retries})"}
        # IMPORTANT: route retries through _execute_subtask so builder quality gate / files_created
        # checks are identical to first-run execution (prevents false-positive "success").
        original_desc = subtask.description
        subtask.description = enhanced_input
        try:
            result = await self._execute_subtask(subtask, plan, model, results)
        finally:
            subtask.description = original_desc

        if result.get("success"):
            subtask.status = TaskStatus.COMPLETED
            subtask.output = result.get("output", "")
            subtask.error = ""
            subtask.completed_at = time.time()
            return True
        else:
            subtask.error = str(result.get("error") or "").strip() or "Unknown error"
            # Try downgrading model on failure for auto-recovery
            next_model = self._downgrade_model(model)
            if next_model != model:
                logger.info(f"Auto-recovery: downgrading model {model} → {next_model} for retry")
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "model_downgrade",
                    "from_model": model,
                    "to_model": next_model,
                })
            return await self._handle_failure(subtask, plan, next_model, results)  # Recursive retry

    async def _retry_from_failure(
        self,
        plan: Plan,
        test_task: SubTask,
        test_result: Dict,
        model: str,
        results: Dict,
        succeeded: set,
        completed: set,
        failed: set,
    ):
        """When a test fails, go back and re-run the builder that produced the code."""
        if plan.total_retries >= plan.max_total_retries:
            await self.emit("orchestrator_max_retries", {"total_retries": plan.total_retries})
            return

        await self.emit("test_failed_retrying", {
            "test_task_id": test_task.id,
            "errors": test_result.get("errors", []),
            "suggestion": test_result.get("suggestion", "")
        })

        # Find repair targets upstream (builder/debugger), not only direct deps.
        builder_deps = self._collect_upstream_repair_targets(plan, test_task)
        repaired = False

        for builder_task in builder_deps:
            if builder_task.retries >= builder_task.max_retries:
                continue

            # Re-run the builder with test failure context
            builder_task.retries += 1
            plan.total_retries += 1
            builder_task.status = TaskStatus.RETRYING
            succeeded.discard(builder_task.id)
            succeeded.discard(test_task.id)

            await self.emit("subtask_retry", {
                "subtask_id": builder_task.id,
                "retry": builder_task.retries,
                "reason": "test_failed",
                "test_errors": test_result.get("errors", [])[:3]
            })

            enhanced_input = (
                f"{builder_task.description}\n\n"
                f"[System Context]\n"
                f"Output directory: {str(OUTPUT_DIR)}\n"
                f"Preview server URL: http://127.0.0.1:8765/preview/\n\n"
                f"🔴 THE TESTS FAILED! Here's what went wrong:\n"
                f"Errors: {json.dumps(test_result.get('errors', []))}\n"
                f"Suggestion: {test_result.get('suggestion', 'Review and fix the code')}\n\n"
                f"IMPORTANT: Keep the HTML complete and production-ready (roughly 220-500 lines). "
                f"Start with <!DOCTYPE html> and end with </html>. "
                f"Use task-adaptive quality requirements below:\n"
                f"{self._builder_design_requirements(plan.goal)}\n"
                f"CRITICAL: save final HTML via file_ops write to /tmp/evermind_output/index.html. "
                f"Do not rely on text-only HTML response.\n\n"
                f"Fix the issues and return a concise summary after file write."
            )

            agent_node = {"type": builder_task.agent_type, "model": model,
                          "id": f"auto_{builder_task.id}_fix{builder_task.retries}",
                          "name": f"Debugger #{builder_task.id} (fix attempt {builder_task.retries})"}
            # Reuse the same execution pipeline and quality gates as normal subtask execution.
            original_desc = builder_task.description
            builder_task.description = enhanced_input
            try:
                result = await self._execute_subtask(builder_task, plan, model, results)
            finally:
                builder_task.description = original_desc

            if result.get("success"):
                builder_task.status = TaskStatus.COMPLETED
                builder_task.output = result.get("output", "")
                builder_task.error = ""
                builder_task.completed_at = time.time()
                results[builder_task.id] = result
                succeeded.add(builder_task.id)
                completed.add(builder_task.id)
                failed.discard(builder_task.id)
                repaired = True

                # Reset all downstream tasks so they re-run against fresh builder artifacts.
                downstream_ids: set[str] = set()
                queue = [builder_task.id]
                while queue:
                    current = queue.pop(0)
                    for st in plan.subtasks:
                        if st.id in downstream_ids or st.id == builder_task.id:
                            continue
                        if current in st.depends_on:
                            downstream_ids.add(st.id)
                            queue.append(st.id)

                if downstream_ids:
                    await self.emit("subtask_progress", {
                        "subtask_id": builder_task.id,
                        "stage": "requeue_downstream",
                        "message": f"Reset downstream tasks after test failure: {', '.join(sorted(downstream_ids))}",
                    })

                for st in plan.subtasks:
                    if st.id not in downstream_ids:
                        continue
                    st.status = TaskStatus.PENDING
                    st.output = ""
                    st.error = ""
                    st.completed_at = 0
                    results.pop(st.id, None)
                    succeeded.discard(st.id)
                    completed.discard(st.id)
                    failed.discard(st.id)
                break

        if not repaired:
            # No upstream task could be repaired; persist tester failure explicitly.
            test_error = (
                "Tester detected a blocking issue and no repair task was available. "
                f"Errors: {test_result.get('errors', [])[:2]}"
            )
            test_task.status = TaskStatus.FAILED
            test_task.error = test_error
            results[test_task.id] = {"success": False, "error": test_error}
            succeeded.discard(test_task.id)
            completed.add(test_task.id)
            failed.add(test_task.id)
            await self.emit("subtask_progress", {
                "subtask_id": test_task.id,
                "stage": "error",
                "message": test_error[:300],
            })

    def _parse_test_result(self, output: str) -> Dict:
        """Parse tester agent output for pass/fail status."""
        lower = (output or "").lower()
        infra_non_retry_markers = [
            "no html preview artifact found",
            "browser smoke test unavailable",
            "playwright runtime unavailable",
            "invalid preview url or path",
        ]

        def _is_infra_non_retry(text: str) -> bool:
            return any(marker in text for marker in infra_non_retry_markers)

        # Explicit deterministic gate markers first (must override any stale/pass JSON text).
        if "__evermind_tester_gate__=fail" in lower or "deterministic visual gate failed" in lower:
            retryable = not _is_infra_non_retry(lower)
            return {
                "status": "fail",
                "errors": [output[:500]],
                "suggestion": "Review tester visual gate findings",
                "retryable": retryable,
            }
        if "__evermind_tester_gate__=pass" in lower or "deterministic visual gate passed" in lower:
            if "quality gate failed" not in lower:
                return {"status": "pass", "details": output[:500], "retryable": False}

        try:
            json_start = output.find("{")
            json_end = output.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(output[json_start:json_end])
                # Normalize: treat "deployed"/"approved" as pass
                status = str(parsed.get("status", "")).lower()
                if status in ("deployed", "approved", "pass", "success"):
                    parsed["status"] = "pass"
                    parsed["retryable"] = False
                    return parsed
                if status in ("fail", "failed", "error"):
                    parsed["status"] = "fail"
                    details = json.dumps(parsed, ensure_ascii=False).lower()
                    parsed["retryable"] = not _is_infra_non_retry(details)
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

        # Heuristic fallback: prioritize explicit failure markers before pass markers.
        strong_fail_markers = [
            "missing <head", "missing <style", "no css styles", "html structure is incomplete",
            "truncated", "incomplete html", "unterminated", "exception", "traceback",
            "quality gate failed",
        ]
        cloud_only_markers = ["no public url", "cannot deploy", "netlify", "github pages", "vercel", "no url"]
        if any(marker in lower for marker in strong_fail_markers):
            retryable = not _is_infra_non_retry(lower)
            return {"status": "fail", "errors": [output[:500]], "suggestion": "Review output", "retryable": retryable}
        fail_word = bool(re.search(r"\bfail(?:ed|ure)?\b", lower))
        negated_fail_word = bool(re.search(r"\b(no|without|zero|0)\s+fail(?:ed|ure|s)?\b", lower))
        if "error" in lower or "bug" in lower or "broken" in lower or (fail_word and not negated_fail_word):
            if any(ignore in lower for ignore in cloud_only_markers):
                return {"status": "pass", "details": "Local files created (no cloud deploy needed)", "retryable": False}
            retryable = not _is_infra_non_retry(lower)
            return {"status": "fail", "errors": [output[:500]], "suggestion": "Review output", "retryable": retryable}
        if any(w in lower for w in ["files exist", "verified", "all files", "preview_url", "deployed",
                                     "created successfully", "looks correct", "approved", "status\": \"pass"]):
            return {"status": "pass", "details": output[:500], "retryable": False}
        return {"status": "pass", "details": output[:500], "retryable": False}

    def _parse_reviewer_verdict(self, output: str) -> str:
        """
        Parse reviewer output into APPROVED / REJECTED.
        Defaults to REJECTED when verdict is absent — better to re-check than ship broken.
        """
        text = (output or "").strip()
        if not text:
            return "REJECTED"  # Empty review = review not completed

        # Prefer structured JSON verdict when available.
        try:
            json_start = text.find("{")
            json_end = text.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(text[json_start:json_end])
                verdict = str(parsed.get("verdict", "")).strip().upper()
                if verdict in ("APPROVED", "REJECTED"):
                    # Extra guard: if verdict says APPROVED but mentions blank/empty sections, override
                    issues = parsed.get("issues", [])
                    blocking_issues = parsed.get("blocking_issues", [])
                    required_changes = parsed.get("required_changes", [])
                    missing_deliverables = parsed.get("missing_deliverables", [])
                    ship_readiness = parsed.get("ship_readiness")
                    blank_count = int(parsed.get("blank_sections_found", 0) or 0)
                    if verdict == "APPROVED" and blank_count > 1:
                        logger.warning(f"Reviewer said APPROVED but found {blank_count} blank sections — overriding to REJECTED")
                        return "REJECTED"
                    if verdict == "APPROVED" and issues:
                        issue_text = " ".join(str(i) for i in issues).lower()
                        if any(kw in issue_text for kw in ["blank", "empty", "missing content", "no content", "空白", "没有内容"]):
                            logger.warning(f"Reviewer said APPROVED but issues mention blank content — overriding to REJECTED")
                            return "REJECTED"
                    if verdict == "APPROVED" and isinstance(blocking_issues, list) and any(str(item).strip() for item in blocking_issues):
                        logger.warning("Reviewer said APPROVED but blocking_issues is non-empty — overriding to REJECTED")
                        return "REJECTED"
                    if verdict == "APPROVED" and isinstance(required_changes, list) and any(str(item).strip() for item in required_changes):
                        logger.warning("Reviewer said APPROVED but required_changes is non-empty — overriding to REJECTED")
                        return "REJECTED"
                    if verdict == "APPROVED" and isinstance(missing_deliverables, list) and any(str(item).strip() for item in missing_deliverables):
                        logger.warning("Reviewer said APPROVED but missing_deliverables is non-empty — overriding to REJECTED")
                        return "REJECTED"
                    if verdict == "APPROVED" and isinstance(ship_readiness, (int, float)) and float(ship_readiness) < 7:
                        logger.warning("Reviewer said APPROVED but ship_readiness < 7 — overriding to REJECTED")
                        return "REJECTED"
                    # Score-based auto-REJECT: enforce minimum quality thresholds
                    if verdict == "APPROVED":
                        scores = parsed.get("scores", {})
                        if isinstance(scores, dict) and scores:
                            score_values = [v for v in scores.values() if isinstance(v, (int, float))]
                            if score_values:
                                min_score = min(score_values)
                                avg_score = sum(score_values) / len(score_values)
                                if min_score <= 4:
                                    logger.warning(
                                        f"Reviewer said APPROVED but min score={min_score} (≤4) — overriding to REJECTED"
                                    )
                                    return "REJECTED"
                                if avg_score < 7:
                                    logger.warning(
                                        f"Reviewer said APPROVED but avg score={avg_score:.1f} (<7) — overriding to REJECTED"
                                    )
                                    return "REJECTED"
                                for key in ("functionality", "completeness", "content-completeness", "originality"):
                                    if key in scores and isinstance(scores.get(key), (int, float)) and float(scores.get(key)) < 6:
                                        logger.warning(
                                            "Reviewer said APPROVED but %s score=%s (<6) — overriding to REJECTED",
                                            key,
                                            scores.get(key),
                                        )
                                        return "REJECTED"
                    return verdict
        except Exception:
            pass

        lower = text.lower()
        if '"rejected"' in lower or "'rejected'" in lower:
            return "REJECTED"
        if '"needs_changes"' in lower or "'needs_changes'" in lower:
            return "REJECTED"
        if "verdict" in lower and "reject" in lower:
            return "REJECTED"
        # Check for content-emptiness keywords even when verdict is APPROVED
        if any(kw in lower for kw in ["blank section", "empty section", "no content", "空白", "内容缺失"]):
            return "REJECTED"
        if '"approved"' in lower or "'approved'" in lower:
            return "APPROVED"
        # No clear verdict — default to REJECTED, better safe than shipping broken
        return "REJECTED"

    # ═══════════════════════════════════════════
    # Report
    # ═══════════════════════════════════════════
    def _build_report(self, plan: Plan, results: Dict) -> Dict:
        success_count = sum(1 for st in plan.subtasks if st.status == TaskStatus.COMPLETED)
        fail_count = sum(1 for st in plan.subtasks if st.status == TaskStatus.FAILED)
        terminal_count = sum(1 for st in plan.subtasks if st.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED))
        pending_count = len(plan.subtasks) - terminal_count
        total_tokens = 0
        total_cost = 0.0
        for item in (results or {}).values():
            if not isinstance(item, dict):
                continue
            total_tokens += max(0, int(item.get("tokens_used", 0) or 0))
            total_cost += max(0.0, float(item.get("cost", 0.0) or 0.0))
        return {
            "success": fail_count == 0 and success_count == len(plan.subtasks),
            "goal": plan.goal,
            "difficulty": plan.difficulty,
            "total_subtasks": len(plan.subtasks),
            "completed": success_count,
            "failed": fail_count,
            "pending": pending_count,
            "total_retries": plan.total_retries,
            "duration_seconds": round(time.time() - plan.created_at, 1),
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 6),
            "subtasks": [
                {
                    "id": st.id, "agent": st.agent_type, "task": st.description,
                    "status": st.status.value, "retries": st.retries,
                    "output_preview": st.output[:600] if st.output else "",
                    "work_summary": self._summarize_node_work(st),
                    "files_created": results.get(st.id, {}).get("files_created", []),
                    "error": st.error[:300] if st.error else "",
                    "duration_seconds": round(getattr(st, 'ended_at', 0) - getattr(st, 'started_at', 0), 1) if getattr(st, 'ended_at', 0) and getattr(st, 'started_at', 0) else None,
                    "started_at": getattr(st, 'started_at', None),
                    "ended_at": getattr(st, 'ended_at', None),
                }
                for st in plan.subtasks
            ],
            "results": {k: {"success": v.get("success"), "output_len": len(str(v.get("output", "")))}
                        for k, v in results.items()}
        }

    def _summarize_node_work(self, st) -> list:
        """Extract a concise list of what this node actually did from its output."""
        bullets = []
        output = (st.output or "").strip()
        agent = (st.agent_type or "").lower()
        if not output:
            return bullets

        # 1. Files created/written
        file_mentions = re.findall(r'(?:wrote|write|saved?|created?|generated?)[^\n]*?(/tmp/[^\s"\'`]+|index\.html|[\w.-]+\.html|[\w.-]+\.css|[\w.-]+\.js)', output, re.IGNORECASE)
        if file_mentions:
            unique = list(dict.fromkeys(file_mentions))[:3]
            bullets.append(f"生成文件：{', '.join(unique)}")

        # 2. Technologies/features used
        tech_patterns = {
            "Canvas 2D": r'canvas|getContext\s*\(\s*["\']2d',
            "CSS Grid": r'grid-template|display:\s*grid',
            "CSS 动画": r'@keyframes|animation-name',
            "响应式设计": r'@media\s*\(',
            "毛玻璃效果": r'backdrop-filter|blur\(',
            "SVG 图标": r'<svg|xmlns.*svg',
            "JavaScript 交互": r'addEventListener|onclick|click\s*\(',
            "游戏循环": r'requestAnimationFrame|gameLoop|game_loop',
            "碰撞检测": r'collision|intersect|hitTest',
            "粒子效果": r'particle|Particle',
            "本地存储": r'localStorage',
            "Web Audio": r'AudioContext|oscillator',
            "滑动导航": r'slide|\.active|translateX',
            "图表": r'chart|bar-chart|donut|pie',
            "数据表格": r'<table|<th|<td',
        }
        found_tech = []
        for label, pattern in tech_patterns.items():
            if re.search(pattern, output, re.IGNORECASE):
                found_tech.append(label)
        if found_tech:
            bullets.append(f"使用技术：{', '.join(found_tech[:5])}")

        # 3. Content sections / key features
        if agent == "builder":
            section_patterns = re.findall(r'<(?:section|div)\s+(?:class|id)=["\']([^"\']+)', output, re.IGNORECASE)
            if section_patterns:
                unique_sections = list(dict.fromkeys(section_patterns))[:5]
                bullets.append(f"页面区块：{', '.join(unique_sections)}")

            # Line count
            html_match = re.search(r'<!DOCTYPE|<html', output, re.IGNORECASE)
            if html_match:
                html_content = output[html_match.start():]
                end_match = re.search(r'</html>', html_content, re.IGNORECASE)
                if end_match:
                    line_count = html_content[:end_match.end()].count('\n') + 1
                    bullets.append(f"代码规模：约 {line_count} 行 HTML")

            # Color scheme
            color_vars = re.findall(r'--(?:primary|accent|bg|surface):\s*([^;]+)', output)
            if color_vars:
                bullets.append(f"配色方案：{', '.join(v.strip() for v in color_vars[:3])}")

        elif agent == "analyst":
            # What the analyst found
            if re.search(r'参考|reference|设计|design|inspiration|建议|suggest', output, re.IGNORECASE):
                bullets.append("完成参考调研，提炼设计建议")
            if re.search(r'http[s]?://[^\s"\']+', output):
                urls = re.findall(r'http[s]?://[^\s"\'`<>]+', output)
                bullets.append(f"访问了 {min(len(urls), 5)} 个参考站点")

        elif agent == "tester":
            # Visual score
            score_match = re.search(r'visual_score["\s:]+(\d+)', output, re.IGNORECASE)
            if score_match:
                bullets.append(f"视觉评分：{score_match.group(1)}/10")
            if re.search(r'screenshot|截图', output, re.IGNORECASE):
                bullets.append("已截图进行视觉验证")
            status_match = re.search(r'"status"\s*:\s*"(pass|fail)"', output, re.IGNORECASE)
            if status_match:
                s = status_match.group(1).upper()
                bullets.append(f"测试结果：{s}")

        elif agent == "reviewer":
            # Scores
            for dim in ['layout', 'color', 'typography', 'animation', 'responsive']:
                m = re.search(rf'"{dim}"\s*:\s*(\d+)', output, re.IGNORECASE)
                if m:
                    bullets.append(f"{dim} 评分：{m.group(1)}/10")
            verdict_match = re.search(r'"verdict"\s*:\s*"(APPROVED|REJECTED)"', output, re.IGNORECASE)
            if verdict_match:
                bullets.append(f"审查结论：{verdict_match.group(1)}")

        # 4. Builder description (model's own summary)
        desc_lines = output.split('\n')
        for line in desc_lines[-10:]:
            clean = line.strip()
            if len(clean) > 20 and not clean.startswith(('<', '{', '```', '//')) and not re.match(r'^[\s{}\[\]<>]', clean):
                if any(kw in clean.lower() for kw in ['built', 'created', 'implemented', 'features', '实现', '创建', '包含', '功能', '特色', '完成']):
                    bullets.append(clean[:120])
                    break

        return bullets[:8]

    def stop(self):
        """Cancel the current execution."""
        self._cancel = True
