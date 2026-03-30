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
import colorsys
import json
import random
from hashlib import sha1
from html import unescape
from html.parser import HTMLParser
from html_postprocess import postprocess_generated_text, postprocess_html, repair_html_structure
import logging
import os
import re
import shutil
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse
from dataclasses import dataclass, field

from plugins.base import (
    PluginRegistry,
    is_image_generation_available,
    resolve_enabled_plugins_for_node,
)
import task_classifier
from task_store import get_artifact_store, get_node_execution_store, get_run_store
from preview_validation import (
    _latest_mtime,
    _preferred_preview_candidate,
    build_preview_url_for_file,
    collect_script_context,
    collect_stylesheet_context,
    has_truncation_marker,
    inspect_body_structure,
    inspect_shared_local_script_safety,
    inspect_html_integrity,
    is_bootstrap_html_artifact,
    is_partial_html_artifact,
    latest_preview_artifact,
    latest_stable_preview_artifact,
    update_visual_baseline,
    validate_html_file,
    validate_preview,
)
from repo_map import build_repo_context
from runtime_paths import resolve_output_dir
from workflow_templates import pro_template_profile

# Output directory for generated files
OUTPUT_DIR = resolve_output_dir()
PREVIEW_PORT = os.getenv("PORT", "8765")
MIN_COMMERCIAL_HTML_BYTES = int(os.getenv("EVERMIND_MIN_HTML_BYTES", "1200"))
MIN_COMMERCIAL_CSS_RULES = int(os.getenv("EVERMIND_MIN_CSS_RULES", "10"))
MIN_SEMANTIC_BLOCKS = int(os.getenv("EVERMIND_MIN_SEMANTIC_BLOCKS", "4"))
MAX_EMOJI_GLYPHS = int(os.getenv("EVERMIND_MAX_EMOJI_GLYPHS", "0"))
MAX_DEP_CONTEXT_CHARS = int(os.getenv("EVERMIND_DEP_CONTEXT_CHARS", "900"))
# Keep the orchestrator watchdog looser than the AI-bridge pre-write timeout.
# The bridge has a smarter fallback path (forced text-only final delivery), so
# this outer guard should only trip if the inner recovery path itself stalls.
BUILDER_FIRST_WRITE_TIMEOUT_SEC = int(
    os.getenv("EVERMIND_ORCH_BUILDER_FIRST_WRITE_TIMEOUT_SEC", "180")
)
BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC = int(os.getenv("EVERMIND_BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC", "90"))
BUILDER_DIRECT_MULTIFILE_MARKER = "DIRECT MULTI-FILE DELIVERY ONLY."
BUILDER_TARGET_OVERRIDE_MARKER = "HTML TARGET OVERRIDE:"
BUILDER_NAV_REPAIR_ONLY_MARKER = "__EVERMIND_NAV_REPAIR_ONLY__"
MULTI_PAGE_MIN_HTML_BYTES = int(os.getenv("EVERMIND_MULTI_PAGE_MIN_HTML_BYTES", "2000"))
MOTION_MULTI_PAGE_MIN_HTML_BYTES = int(os.getenv("EVERMIND_MOTION_MULTI_PAGE_MIN_HTML_BYTES", "2400"))

# Regex to match ```lang filename\n...``` code blocks
_CODE_BLOCK_RE = re.compile(r'```([^\n`]*)\n(.*?)```', re.DOTALL)
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

_CONTINUATION_HINT_RE = re.compile(
    r"(继续|接着|延续|沿用|基于上次|在这个基础上|在上个版本上|上一轮|刚才那个|同一个项目|继续优化|"
    r"continue|keep iterating|same project|same site|based on the previous|iterate on the current)",
    re.IGNORECASE,
)
_ITERATIVE_EDIT_HINT_RE = re.compile(
    r"(修改|改一下|再改|优化|再优化|微调|调整|完善|打磨|升级|修一下|继续做|继续完善|"
    r"modify|revise|refine|improve|iterate|tweak|polish|update)",
    re.IGNORECASE,
)
_DEICTIC_PROJECT_HINT_RE = re.compile(
    r"(这个|这次|当前|现有|刚才|上次|上一版|上一轮|前一个|同一个|该项目|该网站|该游戏|"
    r"this|current|existing|previous|same|that one|the site|the game|the project)",
    re.IGNORECASE,
)
_NEW_PROJECT_HINT_RE = re.compile(
    r"(全新|新建|重新做一个|重新创建|从零开始|另外做一个|另一个|新项目|新网站|新游戏|"
    r"brand new|new project|new site|new game|from scratch|create a new|build a new)",
    re.IGNORECASE,
)


class TaskStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
    TESTING = "testing"
    FAILED = "failed"
    BLOCKED = "blocked"
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
    builder_has_written_file: bool = False
    builder_written_files: List[str] = field(default_factory=list)
    builder_last_write_at: float = 0


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

    def _builder_direct_multifile_mode(self, subtask: Optional[SubTask]) -> bool:
        if not subtask or getattr(subtask, "agent_type", "") != "builder":
            return False
        text = str(getattr(subtask, "description", "") or "")
        return BUILDER_DIRECT_MULTIFILE_MARKER.lower() in text.lower()

    def _builder_execution_direct_multifile_mode(
        self,
        plan: Optional[Plan],
        subtask: Optional[SubTask],
        model: str,
    ) -> bool:
        if self._builder_direct_multifile_mode(subtask):
            return True
        if not plan or not subtask or getattr(subtask, "agent_type", "") != "builder":
            return False
        if not self._is_multi_page_website_goal(plan.goal):
            return False
        override_targets = self._builder_target_override_targets(
            str(getattr(subtask, "description", "") or ""),
            can_write_root_index=self._builder_can_write_root_index(plan, subtask, plan.goal),
        )
        if override_targets:
            return True
        if "kimi" not in str(model or "").strip().lower():
            return False
        assigned_targets = self._builder_bootstrap_targets(plan, subtask)
        return len(assigned_targets) >= 3

    def _builder_execution_direct_text_mode(
        self,
        plan: Optional[Plan],
        subtask: Optional[SubTask],
    ) -> bool:
        if not plan or not subtask or getattr(subtask, "agent_type", "") != "builder":
            return False
        if self._builder_direct_multifile_mode(subtask):
            return False
        if self._is_multi_page_website_goal(plan.goal):
            return False
        try:
            profile = task_classifier.classify(str(plan.goal or ""))
        except Exception:
            return False
        if profile.task_type != "game":
            return False
        assigned_targets = self._builder_bootstrap_targets(plan, subtask)
        return len(assigned_targets) <= 1

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
        # Immutable preview promotion: once a builder version passes quality, freeze it.
        self._stable_preview_path: Optional[Path] = None
        self._stable_preview_files: List[str] = []
        self._stable_preview_stage: str = ""

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
        if normalized in {"passed", "failed", "blocked", "cancelled", "skipped"}:
            return 100
        return 0

    def _reset_progress_tracking(self, *subtask_ids: str) -> None:
        """Clear monotonic progress high-water marks for fresh queued/retry runs."""
        if not hasattr(self, "_progress_high_water"):
            return
        for subtask_id in subtask_ids:
            sid = str(subtask_id or "").strip()
            if sid:
                self._progress_high_water.pop(sid, None)

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

    def _visual_baseline_scope(self, goal: str) -> str:
        task_id = str((self._canonical_ctx or {}).get("task_id") or "").strip()
        if task_id:
            return f"task_{task_id}"
        normalized_goal = re.sub(r"\s+", " ", str(goal or "").strip().lower())
        if not normalized_goal:
            return "goal_default"
        return f"goal_{sha1(normalized_goal.encode('utf-8')).hexdigest()[:12]}"

    def _persist_visual_regression_artifact(self, subtask_id: str, gate_name: str, gate_result: Dict[str, Any]) -> None:
        visual = gate_result.get("visual_regression") if isinstance(gate_result, dict) else None
        if not isinstance(visual, dict):
            return
        status = str(visual.get("status", "") or "").strip().lower()
        summary = str(visual.get("summary", "") or "").strip()
        if status not in {"warn", "fail"} or not summary:
            return

        entry_type = "warn" if status == "warn" else "error"
        self._append_ne_activity(
            subtask_id,
            f"{gate_name} 视觉回归：{summary[:500]}",
            entry_type=entry_type,
        )

        run_id = str((self._canonical_ctx or {}).get("run_id") or "").strip()
        ne_id = self._ne_id_for_subtask(subtask_id) or ""
        if not run_id:
            return

        lines = [f"{gate_name} visual regression: {status.upper()}", summary]
        issues = visual.get("issues") if isinstance(visual.get("issues"), list) else []
        suggestions = visual.get("suggestions") if isinstance(visual.get("suggestions"), list) else []
        captures = visual.get("captures") if isinstance(visual.get("captures"), list) else []
        if issues:
            lines.append("Issues:")
            lines.extend(f"- {str(item)[:240]}" for item in issues[:6])
        if suggestions:
            lines.append("Suggestions:")
            lines.extend(f"- {str(item)[:240]}" for item in suggestions[:6])
        if captures:
            lines.append("Capture diffs:")
            for capture in captures[:4]:
                if not isinstance(capture, dict):
                    continue
                lines.append(
                    "- {name}: changed={changed} area={area} diff={diff}".format(
                        name=str(capture.get("name") or "capture")[:40],
                        changed=str(capture.get("changed_ratio", "")),
                        area=str(capture.get("diff_area_ratio", "")),
                        diff=str(capture.get("diff_path") or "")[:240],
                    )
                )

        artifact = get_artifact_store().save_artifact({
            "run_id": run_id,
            "node_execution_id": ne_id,
            "artifact_type": "diff_summary",
            "title": f"{gate_name} visual regression",
            "content": "\n".join(lines)[:50000],
            "metadata": {
                "status": status,
                "summary": summary,
                "preview_url": gate_result.get("preview_url"),
                "captures": captures[:6],
                "scope_key": visual.get("scope_key", ""),
                "page_key": visual.get("page_key", ""),
            },
        })
        if ne_id and artifact.get("id"):
            try:
                get_node_execution_store().update_node_execution(ne_id, {"artifact_ids": [artifact["id"]]})
            except Exception:
                pass

    def _persist_node_artifact(
        self,
        subtask_id: str,
        *,
        artifact_type: str,
        title: str,
        path: str = "",
        content: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        run_id = str((self._canonical_ctx or {}).get("run_id") or "").strip()
        ne_id = self._ne_id_for_subtask(subtask_id) or ""
        if not run_id:
            return None
        artifact = get_artifact_store().save_artifact({
            "run_id": run_id,
            "node_execution_id": ne_id,
            "artifact_type": artifact_type,
            "title": title,
            "path": path,
            "content": content,
            "metadata": metadata or {},
        })
        if ne_id and artifact.get("id"):
            try:
                get_node_execution_store().update_node_execution(ne_id, {"artifact_ids": [artifact["id"]]})
            except Exception:
                pass
        return artifact

    def _persist_tool_artifacts(self, subtask_id: str, tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        persisted: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for result in tool_results or []:
            if not isinstance(result, dict):
                continue
            plugin_name = str(result.get("_plugin") or result.get("plugin") or "").strip().lower()
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            action = str(data.get("action") or "").strip().lower()
            for artifact in result.get("artifacts") or []:
                if not isinstance(artifact, dict):
                    continue
                path = str(artifact.get("path") or "").strip()
                if not path:
                    continue
                dedupe_key = (plugin_name, path)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                artifact_kind = str(artifact.get("type") or "").strip().lower()
                artifact_type = "preview_ref"
                if plugin_name in {"browser", "browser_use"}:
                    artifact_type = "browser_trace" if artifact_kind == "trace" or path.lower().endswith(".zip") else "browser_capture"
                saved = self._persist_node_artifact(
                    subtask_id,
                    artifact_type=artifact_type,
                    title=f"{plugin_name or 'tool'} {action or artifact_kind or 'artifact'}",
                    path=path,
                    metadata={
                        "plugin": plugin_name,
                        "action": action,
                        "artifact_kind": artifact_kind,
                        "url": data.get("url"),
                        "final_url": data.get("final_url"),
                        "browser_mode": data.get("browser_mode"),
                        "requested_mode": data.get("requested_mode"),
                        "trace_path": data.get("trace_path"),
                        "capture_path": data.get("capture_path"),
                        "recording_path": data.get("recording_path"),
                        "screenshot_paths": data.get("screenshot_paths"),
                        "action_names": data.get("action_names"),
                    },
                )
                if saved:
                    persisted.append(saved)
        return persisted

    def _reconcile_canonical_context_with_plan(self, plan: "Plan") -> List[str]:
        ctx = self._canonical_ctx or {}
        if not ctx or ctx.get("is_custom_plan"):
            return []
        ne_list = ctx.get("node_executions")
        if not isinstance(ne_list, list) or not ne_list or len(ne_list) != len(plan.subtasks):
            return []

        expected_key_by_subtask_id: Dict[str, str] = {}
        for idx, subtask in enumerate(plan.subtasks):
            if idx >= len(ne_list):
                break
            expected_key_by_subtask_id[subtask.id] = str(ne_list[idx].get("node_key") or "").strip()

        drift_lines: List[str] = []
        for idx, subtask in enumerate(plan.subtasks):
            ne = ne_list[idx]
            ne_id = str(ne.get("id") or "").strip()
            expected_input_summary = str(subtask.description or "")[:2000]
            expected_depends = [
                expected_key_by_subtask_id.get(dep, "")
                for dep in (subtask.depends_on or [])
                if expected_key_by_subtask_id.get(dep, "")
            ]
            updates: Dict[str, Any] = {}
            if list(ne.get("depends_on_keys") or []) != expected_depends:
                updates["depends_on_keys"] = expected_depends
                drift_lines.append(
                    f"{ne.get('node_key') or subtask.agent_type}: depends {ne.get('depends_on_keys') or []} -> {expected_depends}"
                )
            if str(ne.get("input_summary") or "") != expected_input_summary:
                updates["input_summary"] = expected_input_summary
                drift_lines.append(
                    f"{ne.get('node_key') or subtask.agent_type}: input_summary reconciled to current plan"
                )
            if not updates:
                continue
            ne.update(updates)
            if ne_id:
                try:
                    get_node_execution_store().update_node_execution(ne_id, updates)
                except Exception:
                    pass

        if drift_lines:
            logger.warning("[Canonical] Reconciled plan/context drift: %s", drift_lines[:8])
            run_id = str(ctx.get("run_id") or "").strip()
            get_artifact_store().save_artifact({
                "run_id": run_id,
                "artifact_type": "state_snapshot",
                "title": "Canonical graph reconciliation",
                "content": "\n".join(drift_lines)[:50000],
                "metadata": {
                    "difficulty": self.difficulty,
                    "drift_count": len(drift_lines),
                },
            })
            if isinstance(ctx.get("state_snapshot"), dict):
                ctx["state_snapshot"]["reconciled_at"] = time.time()
                ctx["state_snapshot"]["drift_count"] = len(drift_lines)
        return drift_lines

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
        if effective_action == "record_scroll":
            effective_action = "scroll"
        target = str(action.get("target") or "").strip()
        url = str(action.get("url") or "").strip()
        state_changed = bool(action.get("state_changed"))
        keys_count = int(action.get("keys_count", 0) or 0)
        snapshot_ref_count = int(action.get("snapshot_ref_count", 0) or 0)
        observation = str(action.get("observation") or "").strip()
        at_bottom = bool(action.get("at_bottom"))
        at_top = bool(action.get("at_top"))
        is_scrollable = action.get("is_scrollable")
        frame_count = int(action.get("frame_count", 0) or 0)
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
            if action_name == "record_scroll" and frame_count > 0:
                suffix = f"，已录制 {frame_count} 帧滚动证据"
                if at_bottom:
                    return f"浏览器步骤: 连续滚动录制整页内容{suffix}，并确认到达页面底部"
                return f"浏览器步骤: 连续滚动录制整页内容{suffix}"
            if at_bottom:
                return "浏览器步骤: 向下滚动页面并检查后续内容，已确认到达页面底部"
            if at_top:
                return "浏览器步骤: 向上滚动页面并回看顶部内容"
            if is_scrollable is False:
                return "浏览器步骤: 尝试滚动页面，并确认当前页面几乎不可滚动"
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

            if status == "queued" or extra.get("reset_started_at"):
                self._reset_progress_tracking(subtask_id)

            nes = get_node_execution_store()
            server_module._transition_node_if_needed(ne_id, status)
            update_data: Dict[str, Any] = {}
            update_data["progress"] = self._canonical_progress_for_status(
                status,
                explicit=extra.get("progress"),
            )
            if "phase" in extra and extra["phase"] is not None:
                update_data["phase"] = str(extra["phase"])[:120]
            if status in ("running", "queued") and extra.get("reset_started_at"):
                update_data["started_at"] = time.time() if status == "running" else 0.0
                update_data["ended_at"] = 0.0
            # Update output_summary if provided
            if "output_summary" in extra and extra["output_summary"] is not None:
                update_data["output_summary"] = str(extra["output_summary"])[:2000]
            if "input_summary" in extra and extra["input_summary"] is not None:
                update_data["input_summary"] = str(extra["input_summary"])[:2000]
            if "error_message" in extra and extra["error_message"] is not None:
                update_data["error_message"] = str(extra["error_message"])[:2000]
            if extra.get("assigned_model") is not None:
                update_data["assigned_model"] = str(extra.get("assigned_model") or "")[:100]
            if extra.get("assigned_provider") is not None:
                update_data["assigned_provider"] = str(extra.get("assigned_provider") or "")[:60]
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
            if extra.get("retry_count") is not None:
                update_data["retry_count"] = int(extra["retry_count"])
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
            raw_progress = self._canonical_progress_for_status("running", explicit=progress)
            # Monotonic progress: never let progress go backwards (fixes UI jitter during continuation batches)
            if not hasattr(self, "_progress_high_water"):
                self._progress_high_water: Dict[str, int] = {}
            hwm = self._progress_high_water.get(subtask_id, 0)
            if raw_progress < hwm:
                raw_progress = hwm
            else:
                self._progress_high_water[subtask_id] = raw_progress
            update_data["progress"] = raw_progress
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
    _STRONG_MODELS = ["kimi-coding", "gpt-5.4", "claude-4-sonnet", "gemini-2.5-pro", "o3"]
    _DOWNGRADE_CHAIN = ["kimi-coding", "gpt-5.4", "deepseek-v3", "gemini-2.0-flash", "qwen-max"]

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
        Hard upper bound for one subtask execution.
        Builder gets a slightly longer window because complex multi-page sites
        otherwise time out right before artifact promotion.
        """
        cfg = getattr(self.ai_bridge, "config", None)
        # Timeouts tuned per role: builder needs more time,
        # planner must finish fast (lightweight spec output only).
        defaults = {
            "builder": 960,
            "planner": 120,
            "analyst": 240,
            "polisher": 420,
            "reviewer": 180,
            "imagegen": 240,
            "spritesheet": 180,
            "assetimport": 150,
        }
        default_timeout = defaults.get(agent_type, 360)
        max_timeout = 960 if agent_type == "builder" else 900
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
        return max(60, min(value, max_timeout))

    def _execution_timeout_for_subtask(
        self,
        plan: Optional[Plan],
        subtask: Optional[SubTask],
        model: str,
    ) -> int:
        agent_type = getattr(subtask, "agent_type", "") if subtask else ""
        base_timeout = self._configured_subtask_timeout(agent_type or "")
        if not plan or not subtask or agent_type != "builder":
            return base_timeout
        if not self._builder_execution_direct_multifile_mode(plan, subtask, model):
            return base_timeout

        target_count = max(
            len(self._builder_bootstrap_targets(plan, subtask)),
            task_classifier.requested_page_count(plan.goal),
            1,
        )
        if target_count < 6:
            return base_timeout

        try:
            per_page_extra = int(os.getenv("EVERMIND_BUILDER_DIRECT_MULTIFILE_EXTRA_PER_PAGE_SEC", "120"))
        except Exception:
            per_page_extra = 120
        try:
            timeout_cap = int(os.getenv("EVERMIND_BUILDER_DIRECT_MULTIFILE_MAX_TIMEOUT_SEC", "1800"))
        except Exception:
            timeout_cap = 1800
        per_page_extra = max(60, min(per_page_extra, 300))
        timeout_cap = max(base_timeout, min(timeout_cap, 2400))
        boosted_timeout = min(timeout_cap, base_timeout + (max(target_count - 5, 0) * per_page_extra))
        if boosted_timeout > base_timeout:
            logger.info(
                "Boosted builder execution timeout for direct_multifile batch: subtask=%s targets=%s timeout=%ss",
                subtask.id,
                target_count,
                boosted_timeout,
            )
        return boosted_timeout

    def _configured_progress_heartbeat(self) -> int:
        raw = os.getenv("EVERMIND_PROGRESS_HEARTBEAT_SEC", "20")
        try:
            value = int(raw)
        except Exception:
            value = 20
        return max(5, min(value, 120))

    def _watchdog_timeout_grace_seconds(self) -> int:
        raw = os.getenv("EVERMIND_WATCHDOG_TIMEOUT_GRACE_SEC", "45")
        try:
            value = int(raw)
        except Exception:
            value = 45
        return max(20, min(value, 300))

    def _sync_ne_timeout_budget(self, subtask_id: str, timeout_sec: int) -> None:
        ne_id = self._ne_id_for_subtask(subtask_id)
        desired_timeout = int(timeout_sec or 0)
        if not ne_id or desired_timeout <= 0:
            return
        try:
            ne_store = get_node_execution_store()
            current = ne_store.get_node_execution(ne_id) or {}
            current_timeout = int(current.get("timeout_seconds", 0) or 0)
            desired_timeout = max(current_timeout, desired_timeout + self._watchdog_timeout_grace_seconds())
            if desired_timeout != current_timeout:
                ne_store.update_node_execution(ne_id, {"timeout_seconds": desired_timeout})
                logger.info(
                    "[Canonical] Updated NE timeout budget: subtask=%s ne=%s timeout=%ss",
                    subtask_id,
                    ne_id,
                    desired_timeout,
                )
        except Exception:
            pass

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
        # Asset-generation nodes (long runtime, token-limited) get reduced retry budget
        LOW_RETRY_AGENTS = {"spritesheet", "imagegen", "assetimport", "bgremove"}
        for st in plan.subtasks:
            # Planner NEVER retries — it has a deterministic fallback skeleton
            if getattr(st, "agent_type", "") == "planner":
                st.max_retries = 0
            elif getattr(st, "agent_type", "") in LOW_RETRY_AGENTS:
                st.max_retries = min(retries, 1)  # At most 1 retry for asset nodes
            elif getattr(st, "agent_type", "") == "polisher":
                # Polisher is expensive (reads+writes all files); retrying rarely helps
                # and wastes 7+ minutes per attempt. Cap at 1 retry max.
                st.max_retries = min(retries, 1)
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
        if agent_type == "polisher":
            return (
                f"{node_label}: Refine the strongest existing deliverable for: {goal[:200]}. "
                "Do NOT collapse the site to fewer pages, swap content themes, or replace good sections with weaker rewrites. "
                "Improve motion, hierarchy, spacing, typography, imagery treatment, and premium finish while preserving the site's strongest structure. "
                "\n\n"
                "POLISH STRATEGY (CRITICAL):\n"
                "- Prefer shared styles.css and app.js upgrades FIRST when they can lift every route together before touching page HTML.\n"
                "- If the site has more than 3 HTML files, patch at most 2 HTML files by default and focus on the weakest routes unless the visual-gap report names more.\n"
                "- Do NOT rewrite the whole site just to add polish. Preserve working navigation, copy, section order, and route structure.\n"
                "- Read only the files you need: start from styles.css/app.js, index.html, and any route explicitly flagged as weak.\n"
                "\n"
                "IMAGERY AND LAYOUT (CRITICAL):\n"
                "- Do NOT inject giant decorative images, random stock photos, or mismatched visuals just to fill space.\n"
                "- Replace placeholders only when the replacement clearly fits the page topic and composition; otherwise prefer a strong CSS/SVG composition over a bad photo.\n"
                "- Keep media containers disciplined with explicit aspect-ratio, object-fit, max-width, and balanced spacing so images never dominate the layout.\n"
                "- Fix awkward nav/footer alignment, uneven spacing, and black/white-only surfaces by extending the existing palette into 2-4 coordinated tones instead of resetting the design.\n"
                "\n"
                "MOTION AND JS:\n"
                "- Add restrained premium motion only where it improves hierarchy, continuity, or feedback.\n"
                "- Prefer CSS/vanilla JS or the existing shared script. Do NOT add a new animation library unless the current artifact already depends on it or a route is impossible to polish without it.\n"
                "- Keep filenames stable, preserve working navigation contracts, and save upgraded HTML/CSS/JS back under /tmp/evermind_output/.\n"
            )
        if agent_type == "debugger":
            return (
                f"{node_label}: Fix issues found by reviewer/tester for: {goal[:200]}. "
                "Preserve the strongest current artifact, repair only the concrete failing area, and do NOT restyle already-good pages. "
                "Start from the exact files implicated by reviewer/tester findings instead of rewriting the whole site."
            )
        if agent_type == "scribe":
            documentation_task = bool(re.search(
                r"(api|docs?|documentation|reference|manual|guide|spec|文档|说明|教程|手册)",
                goal or "",
                re.IGNORECASE,
            ))
            language = task_classifier.requested_output_language(goal)
            language_line = ""
            if language == "en":
                language_line = " Keep sample labels and content guidance in English."
            elif language == "zh":
                language_line = " Keep sample labels and content guidance in Chinese."
            if documentation_task:
                return (
                    f"{node_label}: Write concise technical documentation for: {goal[:200]}. "
                    "Provide clear structure, examples, API usage patterns, edge cases, and adoption notes. "
                    "Do NOT write production source code unless the task explicitly asks for code. "
                    "Keep it implementation-ready and easy for the next node or user to apply."
                    f"{language_line}"
                )
            return (
                f"{node_label}: Create a SHORT content-architecture handoff for: {goal[:200]}. "
                "Define page-by-page structure, narrative flow, CTA priorities, and copy tone. "
                "Do NOT write HTML, CSS, or JavaScript. "
                "Keep it implementation-ready and compact (roughly 350 words max)."
                f"{language_line}"
            )
        if agent_type == "uidesign":
            language = task_classifier.requested_output_language(goal)
            language_line = ""
            if language == "en":
                language_line = " Any sample labels or UI copy guidance must be in English."
            elif language == "zh":
                language_line = " Any sample labels or UI copy guidance must be in Chinese."
            return (
                f"{node_label}: Produce a concise UI design brief for: {goal[:200]}. "
                "Define layout hierarchy, component behavior, motion intent, and visual system decisions. "
                "Do NOT write production code. Keep the handoff short and implementation-ready."
                f"{language_line}"
            )
        if agent_type == "imagegen":
            asset_mode = task_classifier.game_asset_pipeline_mode(goal)
            if asset_mode == "3d":
                return (
                    f"{node_label}: Produce 3D-game asset concept packs for: {goal[:200]}. "
                    "Return concrete prompts, orthographic turnaround guidance, material/texture notes, silhouette rules, "
                    "and fallback placeholder directions that builders can execute immediately."
                )
            return (
                f"{node_label}: Produce game/web image assets or prompt packs for: {goal[:200]}. "
                "If the comfyui plugin is available, check it first and use it when the pipeline is configured. "
                "Otherwise return concrete prompts, negative prompts, style-lock notes, shot variants, and fallback illustration guidance."
            )
        if agent_type == "spritesheet":
            asset_mode = task_classifier.game_asset_pipeline_mode(goal)
            if asset_mode == "3d":
                return (
                    f"{node_label}: Plan 3D asset packages for: {goal[:200]}. "
                    "Output ONLY compact JSON with asset_families, model_targets, rig_or_animation_clips, "
                    "material_constraints, export_layout, lod_rules, and builder_replacement_rules. "
                    "No prose, no file writes, no speculative extras."
                )
            return (
                f"{node_label}: Plan sprite sheet assets for: {goal[:200]}. "
                "Output ONLY compact JSON with asset_families, animation_states, palette_constraints, export_layout, "
                "frame_counts, and builder_replacement_rules. No prose, no file writes, no speculative extras."
            )
        if agent_type == "assetimport":
            asset_mode = task_classifier.game_asset_pipeline_mode(goal)
            if asset_mode == "3d":
                return (
                    f"{node_label}: Organize the 3D asset pipeline for: {goal[:200]}. "
                    "Output ONLY compact JSON with naming_rules, folder_structure, manifest_fields, runtime_mapping, "
                    "placeholder_fallbacks, and builder_integration_notes for models, textures, and animation clips. "
                    "No prose, no file writes, no speculative extras."
                )
            return (
                f"{node_label}: Organize the asset pipeline for: {goal[:200]}. "
                "Output ONLY compact JSON with naming_rules, folder_structure, manifest_fields, runtime_mapping, "
                "and builder_integration_notes. No prose, no file writes, no speculative extras."
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
        if any(st.agent_type == "polisher" for st in plan.subtasks):
            tags.append("polisher_handoff")
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

    def _condense_handoff_seed(self, text: str, limit: int = 900) -> str:
        cleaned = re.sub(r"</?[A-Za-z0-9_:-]+>", " ", str(text or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[:limit].rstrip() + "..."

    def _builder_refinement_context(self, plan: Plan, subtask: SubTask) -> str:
        if subtask.agent_type != "builder":
            return ""
        slot_index = self._builder_slot_index(plan, subtask.id)
        if slot_index <= 1 and getattr(subtask, "retries", 0) <= 0:
            return ""

        index_path = OUTPUT_DIR / "index.html"
        if not index_path.exists() or not index_path.is_file():
            return ""
        try:
            html = index_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        lower = html.lower()
        if "<!-- evermind-bootstrap scaffold -->" in lower:
            return ""

        quality = self._html_quality_report(html, source=str(index_path))
        visual_gaps = self._visual_gap_entries_for_paths([str(index_path)])
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", str(title_match.group(1) if title_match else "")).strip()
        headings = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", item)).strip()
            for item in re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", html, re.IGNORECASE | re.DOTALL)
        ]
        headings = [item for item in headings if item][:8]
        content_seed = self._condense_handoff_seed(
            re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL),
            limit=1400,
        )

        lines = [
            "[Current Artifact Refinement Context]",
            "A previous builder pass already produced a non-empty index.html. Preserve the strongest existing gameplay shell and improve it in place.",
        ]
        if title:
            lines.append(f"- Current page title: {title}")
        lines.append(f"- Current artifact size: {len(html.encode('utf-8'))} bytes")
        lines.append(f"- Current quality score: {int(quality.get('score', 0) or 0)}")
        quality_errors = [str(item).strip() for item in (quality.get("errors") or []) if str(item or "").strip()]
        quality_warnings = [str(item).strip() for item in (quality.get("warnings") or []) if str(item or "").strip()]
        if quality_errors:
            lines.append("- Current blocking issues: " + "; ".join(quality_errors[:4]))
        if quality_warnings:
            lines.append("- Current warnings: " + "; ".join(quality_warnings[:4]))
        if visual_gaps:
            lines.append("- Visual/content gaps detected: " + "; ".join(item.lstrip("- ").strip() for item in visual_gaps[:4]))
        if headings:
            lines.append("- Existing visible headings/modules to preserve when still good: " + " | ".join(headings[:6]))
        if content_seed:
            lines.append("- Existing artifact content snapshot:")
            lines.append(content_seed)
        lines.extend([
            "- Rewrite the full index.html in place instead of inventing a fresh empty layout.",
            "- Remove or fill every empty wrapper/container. Do NOT leave decorative div/section/article shells without meaningful visible content.",
            "- Preserve working gameplay, HUD, start flow, and strong visual sections from the previous pass unless they are clearly broken.",
        ])
        return "\n".join(lines)

    def _synthesized_analyst_handoff_sections(
        self,
        plan: Plan,
        analyst_output: str,
        visited_urls: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        visited_urls = [str(url).strip() for url in (visited_urls or []) if str(url).strip()]
        summary_seed = self._condense_handoff_seed(analyst_output)
        multi_page = task_classifier.wants_multi_page(plan.goal)
        page_count = task_classifier.requested_page_count(plan.goal)
        builder_count = len([st for st in plan.subtasks if st.agent_type == "builder"])
        if builder_count <= 1:
            builder_1_focus = self._single_builder_handoff_focus(plan.goal)
            builder_2_focus = "N/A — this execution plan uses a single builder, so continuity and full delivery stay with Builder 1."
        else:
            builder_1_focus, builder_2_focus = self._pro_builder_focus(plan.goal)

        reference_sites = (
            "\n".join(f"- {url}" for url in visited_urls[:6])
            if visited_urls
            else "- No trusted live reference URLs were captured in this run. Do not invent citations downstream."
        )
        design_direction = "\n".join([
            "- Visual direction: Apple-adjacent premium minimalism, restrained palette, high-end typography, cinematic motion, and strong whitespace rhythm.",
            "- Keep one coherent design system across all pages; do not let later retries drift into a cheaper or flatter style.",
            f"- Analyst summary seed: {summary_seed or 'Focus on a concise, luxurious, editorial presentation with substantial real content.'}",
        ])
        non_negotiables = "\n".join([
            "- Write real deliverables, not task_*/index.html preview fallbacks or page fragments.",
            "- No blank middle sections, no empty routes, no one-word pages, no placeholder copy, and no emoji glyphs inside the shipped UI.",
            "- Every requested page/route must contain substantial visible content above and below the fold.",
            "- Shared navigation must work across every shipped page and preserve the same premium visual system on desktop and mobile.",
        ])
        delivery_lines = [
            task_classifier.delivery_contract(plan.goal).strip(),
            task_classifier.multi_page_contract(plan.goal).strip(),
            "- Prefer file_ops write under the runtime output directory and keep named HTML files stable across retries.",
        ]
        if multi_page:
            delivery_lines.append(f"- Minimum expectation: index.html plus {max(page_count - 1, 1)} additional linked HTML page(s) with real content.")
        deliverables_contract = "\n".join(line for line in delivery_lines if line)
        risk_register = "\n".join([
            "- Highest risk: builder spends tool turns on list/read calls, then falls back to text-only output without saving real named HTML files.",
            "- Highest risk: blank or invalid file_ops paths trigger security policy failures and stall delivery.",
            "- Highest risk: a weaker retry overwrites a previously stronger preview with a partial or single-page artifact.",
            "- Highest risk: first and last sections render while the middle collapses or stays blank; reviewer/tester must inspect full-page depth, not just the first viewport.",
        ])
        reviewer_handoff = "\n".join([
            "- Reject any regression that removes previously good sections, collapses middle content, or reduces the site to a weaker single-page artifact.",
            "- For multi-page work, approve only after visiting every requested page through real navigation and confirming each route has real content.",
            "- Treat blank bands, missing lower sections, broken navigation, dead local links, giant icon placeholders, or low-density placeholder copy as ship blockers.",
        ])
        polisher_handoff = "\n".join([
            "- Preserve the strongest existing structure and content; only intensify quality, motion, transitions, and finish.",
            "- Add premium continuity between pages and sections instead of hard cuts, abrupt blocks, or static filler surfaces.",
            "- Never reduce page count, delete strong sections, or flatten the site into a cheaper single-page fallback during polish.",
            "- Replace giant line-art/icon placeholder visuals and broken local routes instead of polishing around them.",
        ])
        tester_handoff = "\n".join([
            "- Navigate every requested page, not just the landing page.",
            "- Capture evidence from top, middle, and lower sections; explicitly fail if the middle of the page is blank or collapsed.",
            "- Interact with real navigation/controls and verify the page state changes after clicks.",
            "- Fail any route set that still exposes dead local links or giant icon/pattern placeholder visuals as final content.",
        ])
        debugger_handoff = "\n".join([
            "- Preserve the strongest existing output and repair the smallest failing area first.",
            "- Never replace a strong multi-page site with a weaker single-page or text-only fallback during repair.",
            "- Fix invalid file paths, missing page writes, blank sections, and broken navigation before cosmetic tweaks.",
        ])
        return {
            "reference_sites": reference_sites,
            "design_direction": design_direction,
            "non_negotiables": non_negotiables,
            "deliverables_contract": deliverables_contract,
            "risk_register": risk_register,
            "builder_1_handoff": builder_1_focus,
            "builder_2_handoff": builder_2_focus,
            "reviewer_handoff": reviewer_handoff,
            "polisher_handoff": polisher_handoff,
            "tester_handoff": tester_handoff,
            "debugger_handoff": debugger_handoff,
        }

    def _materialize_analyst_handoff(
        self,
        plan: Plan,
        analyst_output: str,
        visited_urls: Optional[List[str]] = None,
    ) -> tuple[str, List[str], List[str]]:
        base_output = str(analyst_output or "").strip()
        missing_tags = self._validate_analyst_handoff(base_output, plan)
        if not missing_tags:
            return base_output, [], []
        synthesized = self._synthesized_analyst_handoff_sections(plan, base_output, visited_urls=visited_urls)
        appended_blocks: List[str] = []
        synthesized_tags: List[str] = []
        for tag in missing_tags:
            content = str(synthesized.get(tag, "") or "").strip()
            if not content:
                continue
            appended_blocks.append(f"<{tag}>\n{content}\n</{tag}>")
            synthesized_tags.append(tag)
        if not appended_blocks:
            return base_output, [], missing_tags
        augmented = (base_output + "\n\n" + "\n\n".join(appended_blocks)).strip()
        remaining_tags = self._validate_analyst_handoff(augmented, plan)
        return augmented, synthesized_tags, remaining_tags

    def _build_analyst_handoff_context(self, plan: Plan, subtask: SubTask, analyst_output: str) -> str:
        sections: List[tuple[str, str]] = []
        shared_tags = [
            ("References", "reference_sites"),
            ("Design Direction", "design_direction"),
            ("Non-Negotiables", "non_negotiables"),
            ("Deliverables Contract", "deliverables_contract"),
            ("Curated Image Library", "curated_image_library"),
            ("Skill Activation Plan", "skill_activation_plan"),
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
        elif subtask.agent_type == "polisher":
            role_tags.append(("Polisher Handoff", "polisher_handoff"))
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
            # F2-1: Filter pure URL lines & truncate to reduce context pollution
            filtered_lines = []
            for line in fallback.splitlines():
                stripped = line.strip()
                # Skip lines that are just bare URLs (no useful context)
                if stripped.startswith("http") and " " not in stripped:
                    continue
                filtered_lines.append(line)
            fallback = "\n".join(filtered_lines)
            return (
                "[Analyst Execution Contract]\n"
                "Use the upstream analyst report below as a mandatory execution brief.\n"
                f"{fallback[:1200]}"
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

    def _collect_transitive_dependency_ids(self, plan: Plan, seed_ids: List[str]) -> List[str]:
        by_id = {str(st.id): st for st in (plan.subtasks or [])}
        queue = [str(item) for item in (seed_ids or []) if str(item)]
        seen: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            task = by_id.get(current)
            if not task:
                continue
            for dep_id in (task.depends_on or []):
                dep_text = str(dep_id or "").strip()
                if dep_text and dep_text not in seen:
                    queue.append(dep_text)
        return [str(st.id) for st in (plan.subtasks or []) if str(st.id) in seen]

    def _transitive_upstream_builders(self, plan: Plan, subtask: SubTask) -> List[SubTask]:
        upstream_ids = set(self._collect_transitive_dependency_ids(plan, list(subtask.depends_on or [])))
        return [
            st for st in (plan.subtasks or [])
            if st.agent_type == "builder" and str(st.id) in upstream_ids
        ]

    def _collect_transitive_downstream_ids(self, plan: Plan, seed_ids: List[str]) -> List[str]:
        reverse_deps: Dict[str, List[str]] = {}
        for task in (plan.subtasks or []):
            task_id = str(task.id)
            for dep_id in (task.depends_on or []):
                dep_text = str(dep_id or "").strip()
                if not dep_text:
                    continue
                reverse_deps.setdefault(dep_text, []).append(task_id)

        queue = [str(item) for item in (seed_ids or []) if str(item)]
        seen = set(queue)
        downstream: List[str] = []
        while queue:
            current = queue.pop(0)
            for dependent_id in reverse_deps.get(current, []):
                if dependent_id in seen:
                    continue
                seen.add(dependent_id)
                downstream.append(dependent_id)
                queue.append(dependent_id)
        return downstream

    def _debugger_noop_reason(self, plan: Plan, subtask: SubTask, prev_results: Dict) -> str:
        if subtask.agent_type != "debugger":
            return ""
        upstream_ids = set(self._collect_transitive_dependency_ids(plan, list(subtask.depends_on or [])))
        if not upstream_ids:
            return ""

        reviewer_outputs: List[str] = []
        tester_outputs: List[str] = []
        for task in (plan.subtasks or []):
            task_id = str(task.id)
            if task_id not in upstream_ids:
                continue
            result = prev_results.get(task.id, {}) if isinstance(prev_results, dict) else {}
            error_text = str((result or {}).get("error") or getattr(task, "error", "") or "").strip()
            if error_text:
                return ""
            output_text = str((result or {}).get("output") or getattr(task, "output", "") or "").strip()
            if task.agent_type == "reviewer" and output_text:
                reviewer_outputs.append(output_text)
            elif task.agent_type == "tester" and output_text:
                tester_outputs.append(output_text)

        if not reviewer_outputs or not tester_outputs:
            return ""
        if any(self._parse_reviewer_verdict(text) != "APPROVED" for text in reviewer_outputs):
            return ""
        if any(str(self._parse_test_result(text).get("status") or "").lower() != "pass" for text in tester_outputs):
            return ""
        return "Reviewer/tester 已通过且未给出可执行修复项，保留当前最强版本，不做额外改写。"

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
        strengths = parsed.get("strengths") or []
        ship_readiness = parsed.get("ship_readiness")
        blank_sections = parsed.get("blank_sections_found")

        def _clean_list(items: Any) -> List[str]:
            if not isinstance(items, list):
                return []
            cleaned: List[str] = []
            seen: set[str] = set()
            for item in items:
                text = re.sub(r"\s+", " ", str(item or "")).strip(" -")
                if not text:
                    continue
                lowered = text.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                cleaned.append(text[:240])
            return cleaned

        def _extract_routes(items: List[str]) -> List[str]:
            routes: List[str] = []
            seen: set[str] = set()
            for item in items:
                for match in re.findall(r"\b[\w./-]+\.html\b", item, re.IGNORECASE):
                    route = self._normalize_preview_path(match)
                    if route and route.lower() not in seen:
                        seen.add(route.lower())
                        routes.append(route)
            return routes

        def _owner_for_change(text: str) -> str:
            lowered = str(text or "").lower()
            builder_markers = (
                "missing", "route", ".html", "navigation", "nav", "link", "cta", "button",
                "image", "photo", "media", "landmark", "mismatched", "placeholder",
                "blank", "empty", "content", "copy", "structure", "interactive",
                "form", "accordion", "tabs", "page", "section",
            )
            polisher_markers = (
                "spacing", "typography", "font", "line-height", "kerning",
                "alignment", "padding", "margin", "motion", "animation",
                "transition", "hover", "color", "palette", "surface", "glow",
                "shadow", "rhythm", "polish",
            )
            if any(marker in lowered for marker in builder_markers):
                return "Builder"
            if any(marker in lowered for marker in polisher_markers):
                return "Polisher"
            return "Builder"

        cleaned_issues = _clean_list(issues)
        cleaned_required = _clean_list(required_changes)
        cleaned_missing = _clean_list(missing_deliverables)
        cleaned_acceptance = _clean_list(acceptance)
        cleaned_strengths = _clean_list(strengths)
        route_mentions = _extract_routes(cleaned_issues + cleaned_required + cleaned_missing + cleaned_acceptance)

        builder_changes: List[str] = []
        polisher_changes: List[str] = []
        for item in cleaned_missing + cleaned_required + cleaned_issues:
            owner = _owner_for_change(item)
            prefixed = item if re.match(r"^(Builder|Polisher)\s*:", item, re.IGNORECASE) else f"{owner}: {item}"
            if owner == "Polisher":
                polisher_changes.append(prefixed)
            else:
                builder_changes.append(prefixed)

        color_score = int(scores.get("color", 0) or 0) if scores else 0
        typography_score = int(scores.get("typography", 0) or 0) if scores else 0
        animation_score = int(scores.get("animation", 0) or 0) if scores else 0
        completeness_score = int(scores.get("completeness", 0) or 0) if scores else 0
        functionality_score = int(scores.get("functionality", 0) or 0) if scores else 0
        originality_score = int(scores.get("originality", 0) or 0) if scores else 0
        if color_score and color_score < 7:
            builder_changes.append(
                "Builder: extend the palette beyond flat pure black / pure white surfaces; use 2-4 coordinated tones across body, cards, nav, footer, and CTA states."
            )
        if completeness_score and completeness_score < 7:
            builder_changes.append(
                "Builder: fill every requested route with real content and a meaningful visual anchor above the fold; no empty media slots or text-only premium pages."
            )
        if functionality_score and functionality_score < 7:
            builder_changes.append(
                "Builder: repair the core interaction path so reviewer can click or use at least one real interactive element and observe a state change."
            )
        if originality_score and originality_score < 7:
            builder_changes.append(
                "Builder: strengthen art direction and route differentiation so the site stops feeling generic/template-like."
            )
        if typography_score and typography_score < 7:
            polisher_changes.append(
                "Polisher: tighten typography hierarchy, line-height, spacing, and nav/footer alignment without rewriting the site's structure."
            )
        if animation_score and animation_score < 7:
            polisher_changes.append(
                "Polisher: add restrained but visible motion continuity, hover feedback, and section reveal polish while preserving the existing page architecture."
            )

        def _dedupe(items: List[str]) -> List[str]:
            deduped: List[str] = []
            seen: set[str] = set()
            for item in items:
                lowered = str(item or "").strip().lower()
                if not lowered or lowered in seen:
                    continue
                seen.add(lowered)
                deduped.append(str(item).strip())
            return deduped

        builder_changes = _dedupe(builder_changes)[:6]
        polisher_changes = _dedupe(polisher_changes)[:4]

        lines = [f"Verdict: {verdict}"]
        if ship_readiness not in (None, ""):
            lines.append(f"Ship readiness: {ship_readiness}")
        if scores:
            score_bits = [f"{k}={v}" for k, v in list(scores.items())[:8]]
            if score_bits:
                lines.append("Score summary: " + ", ".join(score_bits))
        if blank_sections not in (None, ""):
            lines.append(f"Blank sections found: {blank_sections}")
        if route_mentions:
            lines.append("Route-specific issues:")
            lines.extend(f"- {route}" for route in route_mentions[:8])
        if cleaned_issues:
            lines.append("Blocking issues:")
            lines.extend(f"- {item}" for item in cleaned_issues[:6])
        if builder_changes:
            lines.append("Builder fixes:")
            lines.extend(f"- {item}" for item in builder_changes[:6])
        if polisher_changes:
            lines.append("Polisher follow-up:")
            lines.extend(f"- {item}" for item in polisher_changes[:4])
        if cleaned_acceptance:
            lines.append("Acceptance criteria:")
            lines.extend(f"- {item}" for item in cleaned_acceptance[:6])
        if cleaned_strengths:
            lines.append("Preserve:")
            lines.extend(f"- {item}" for item in cleaned_strengths[:4])
        return "\n".join(lines)[:2200]

    def _normalize_preview_path(self, raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        parsed = urlparse(text)
        path = parsed.path or text
        if "/preview/" in path:
            path = path.split("/preview/", 1)[1]
        path = path.split("#", 1)[0].split("?", 1)[0].strip()
        path = path.lstrip("/")
        if not path:
            return "index.html"
        if path.endswith("/"):
            return f"{path}index.html"
        last_segment = path.rsplit("/", 1)[-1]
        if "." not in last_segment:
            return f"{path}/index.html"
        return path

    def _is_multi_page_website_goal(self, goal: str) -> bool:
        try:
            return (
                task_classifier.classify(goal).task_type == "website"
                and task_classifier.wants_multi_page(goal)
            )
        except Exception:
            return False

    def _builder_can_write_root_index(
        self,
        plan: Optional[Plan],
        subtask: Optional[SubTask],
        goal: str,
    ) -> bool:
        if not plan or not subtask or subtask.agent_type != "builder":
            return True
        if not self._is_multi_page_website_goal(goal):
            return True
        return self._builder_slot_index(plan, subtask.id) == 1

    def _secondary_builder_root_backup_path(self, subtask_id: str) -> Path:
        return OUTPUT_DIR / "_builder_backups" / f"root_index_before_{subtask_id}.bak"

    def _snapshot_root_index_for_secondary_builder(self, plan: Optional[Plan], subtask: Optional[SubTask]) -> None:
        if not plan or not subtask or subtask.agent_type != "builder":
            return
        if self._builder_can_write_root_index(plan, subtask, plan.goal):
            return
        root_index = OUTPUT_DIR / "index.html"
        if not root_index.exists() or not root_index.is_file():
            return
        backup_path = self._secondary_builder_root_backup_path(subtask.id)
        try:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root_index, backup_path)
        except Exception as e:
            logger.warning(f"Failed to snapshot root index before secondary builder {subtask.id}: {e}")

    def _restore_root_index_after_secondary_builder(
        self,
        plan: Optional[Plan],
        subtask: Optional[SubTask],
        prev_results: Optional[Dict[str, Any]] = None,
    ) -> None:
        root_index = OUTPUT_DIR / "index.html"
        if plan and subtask and subtask.agent_type == "builder":
            backup_path = self._secondary_builder_root_backup_path(subtask.id)
            if backup_path.exists() and backup_path.is_file():
                try:
                    root_index.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_path, root_index)
                    return
                except Exception as e:
                    logger.warning(f"Failed to restore root index from secondary builder backup {backup_path}: {e}")

            homepage_owner = self._homepage_builder_task(plan)
            if homepage_owner:
                candidates: List[Path] = []
                owner_task_copy = OUTPUT_DIR / f"task_{homepage_owner.id}" / "index.html"
                candidates.append(owner_task_copy)
                owner_result = (prev_results or {}).get(homepage_owner.id, {}) if isinstance(prev_results, dict) else {}
                for item in owner_result.get("files_created", []) or []:
                    try:
                        candidates.append(Path(str(item)))
                    except Exception:
                        continue
                for candidate in candidates:
                    try:
                        if candidate.exists() and candidate.is_file() and candidate.name == "index.html":
                            root_index.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(candidate, root_index)
                            return
                    except Exception:
                        continue

        self._restore_root_index_from_stable_preview()

    def _missing_builder_targets(
        self,
        plan: Optional[Plan],
        subtask: Optional[SubTask],
        observed_html_files: Optional[List[str]] = None,
    ) -> List[str]:
        if not plan or not subtask or subtask.agent_type != "builder":
            return []
        if (
            not self._builder_html_target_restriction_enabled(plan, subtask)
            and not self._is_multi_page_website_goal(plan.goal)
        ):
            return []
        assigned_targets = self._builder_bootstrap_targets(plan, subtask)
        if not assigned_targets:
            return []
        observed = observed_html_files
        if observed is None:
            observed = self._evaluate_multi_page_artifacts(plan.goal).get("html_files", []) or []
        observed_set = {
            self._normalize_preview_path(str(item))
            for item in (observed or [])
            if str(item or "").strip()
        }
        missing: List[str] = []
        for name in assigned_targets:
            normalized = self._normalize_preview_path(name)
            if normalized not in observed_set:
                missing.append(name)
        return missing

    def _builder_repair_targets(
        self,
        plan: Optional[Plan],
        subtask: Optional[SubTask],
        multi_page_gate: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        if not plan or not subtask or subtask.agent_type != "builder":
            return []
        assigned_targets = self._builder_bootstrap_targets(plan, subtask)
        if not assigned_targets:
            return []
        gate = multi_page_gate or self._evaluate_multi_page_artifacts(plan.goal)
        observed_targets = gate.get("observed_html_files", []) or gate.get("html_files", []) or []
        invalid_targets = {
            self._normalize_preview_path(str(item))
            for item in (gate.get("invalid_html_files", []) or [])
            if str(item or "").strip()
        }

        repair_targets: List[str] = []
        for name in self._missing_builder_targets(plan, subtask, observed_targets):
            if name not in repair_targets:
                repair_targets.append(name)
        for name in assigned_targets:
            if self._normalize_preview_path(name) in invalid_targets and name not in repair_targets:
                repair_targets.append(name)

        can_patch_root = self._builder_can_write_root_index(plan, subtask, plan.goal)
        nav_needs_root_patch = bool(
            (gate.get("missing_nav_targets") or [])
            or (gate.get("unlinked_secondary_pages") or [])
        )
        repair_scope = str(gate.get("repair_scope") or "")
        owns_non_root_repairs = any(name != "index.html" for name in repair_targets)
        if (
            can_patch_root
            and "index.html" in assigned_targets
            and (
                "index.html" in repair_targets
                or repair_scope == "root_nav_only"
                or owns_non_root_repairs
                or (
                    not self._builder_html_target_restriction_enabled(plan, subtask)
                    and nav_needs_root_patch
                )
            )
            and "index.html" not in repair_targets
        ):
            repair_targets.insert(0, "index.html")
        return repair_targets

    def _builder_error_repair_targets(self, error_text: str) -> List[str]:
        text = str(error_text or "")
        targets: List[str] = []
        for match in re.findall(r"([A-Za-z0-9][A-Za-z0-9._/-]*\.html?)", text, re.IGNORECASE):
            name = Path(str(match or "").strip()).name
            if not name or name in targets:
                continue
            targets.append(name)
        if (
            (
                "index.html does not expose enough working local navigation links" in text
                or "index.html references missing local pages" in text
            )
            and "index.html" not in targets
        ):
            targets.insert(0, "index.html")
        return targets

    def _builder_retry_should_keep_full_scope(
        self,
        builder_error_text: str,
        aggregate_gate: Optional[Dict[str, Any]],
        assigned_targets: List[str],
        retry_targets: List[str],
    ) -> bool:
        if len(assigned_targets) < 4:
            return False
        if not retry_targets or len(retry_targets) >= len(assigned_targets):
            return False

        gate = aggregate_gate or {}
        if str(gate.get("repair_scope") or "") == "root_nav_only":
            return False

        lower = str(builder_error_text or "").lower()
        gate_blob = " ".join(str(item or "").lower() for item in (gate.get("errors") or []))
        scope_markers = (
            "quality gate failed",
            "unfinished visual placeholders",
            "placeholder visual blocks",
            "empty visual/media blocks",
            "empty map/location blocks",
            "icon/pattern placeholder visuals",
            "too thin / stub-like",
            "motion-rich brief requires",
            "page-to-page transition treatment",
        )
        return any(marker in lower or marker in gate_blob for marker in scope_markers)

    def _builder_nav_repair_only(self, error_text: str) -> bool:
        lower = str(error_text or "").lower()
        if (
            "index.html does not expose enough working local navigation links" not in lower
            and "index.html references missing local pages" not in lower
        ):
            return False
        disqualifying_markers = (
            "too thin / stub-like",
            "multi-page delivery incomplete",
            "invalid or corrupted",
            "missing root index.html",
            "broken local navigation links detected",
            "html target not assigned",
            "only builder 1 may write",
            "did not save any real named html page",
            "did not finish its assigned html pages",
            "wrong language",
            "wrong topic",
            "contamination",
            "污染",
        )
        return not any(marker in lower for marker in disqualifying_markers)

    def _builder_nav_repair_retry_active(self, subtask: Optional[SubTask]) -> bool:
        if not subtask or subtask.agent_type != "builder":
            return False
        text = str(getattr(subtask, "description", "") or "")
        return BUILDER_NAV_REPAIR_ONLY_MARKER in text

    def _capture_builder_retry_locked_root_artifacts(
        self,
        plan: Plan,
        subtask: SubTask,
    ) -> Dict[str, str]:
        if not self._builder_nav_repair_retry_active(subtask) or not OUTPUT_DIR.exists():
            return {}
        allowed_names = {
            Path(name).name
            for name in self._builder_allowed_html_targets(plan, subtask)
            if str(name or "").strip()
        }
        snapshot: Dict[str, str] = {}
        for item in OUTPUT_DIR.iterdir():
            if not item.is_file():
                continue
            if item.name in allowed_names:
                continue
            if item.suffix.lower() not in (".html", ".htm", ".css", ".js"):
                continue
            try:
                snapshot[str(item.resolve())] = item.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
        return snapshot

    def _restore_builder_retry_locked_root_artifacts(
        self,
        plan: Plan,
        subtask: SubTask,
        snapshot: Optional[Dict[str, str]],
    ) -> List[str]:
        if not snapshot:
            return []
        allowed_names = {
            Path(name).name
            for name in self._builder_allowed_html_targets(plan, subtask)
            if str(name or "").strip()
        }
        restored: List[str] = []
        snapshot_paths = set(snapshot.keys())
        for abs_path, content in snapshot.items():
            path = Path(abs_path)
            try:
                current = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else None
            except Exception:
                current = None
            if current == content:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                if path.name not in restored:
                    restored.append(path.name)
            except Exception:
                continue
        if OUTPUT_DIR.exists():
            for item in OUTPUT_DIR.iterdir():
                if not item.is_file():
                    continue
                if item.name in allowed_names:
                    continue
                if item.suffix.lower() not in (".html", ".htm", ".css", ".js"):
                    continue
                try:
                    resolved = str(item.resolve())
                except Exception:
                    resolved = str(item)
                if resolved in snapshot_paths:
                    continue
                try:
                    item.unlink()
                    if item.name not in restored:
                        restored.append(item.name)
                except Exception:
                    continue
        return restored

    def _sanitize_builder_generated_files(
        self,
        plan: Plan,
        subtask: SubTask,
        files_created: List[str],
        *,
        prev_results: Optional[Dict[str, Any]] = None,
    ) -> tuple[List[str], List[str]]:
        if subtask.agent_type != "builder" or not files_created:
            return files_created, []
        if not self._is_multi_page_website_goal(plan.goal):
            return files_created, []
        allowed_names = {
            Path(name).name
            for name in self._builder_allowed_html_targets(plan, subtask)
            if str(name or "").strip()
        }
        restrict_html_targets = bool(allowed_names)

        kept: List[str] = []
        dropped: List[str] = []
        restored_root = False
        for item in files_created:
            normalized = self._normalize_generated_path(item)
            path = Path(normalized)
            if path.suffix.lower() not in (".html", ".htm"):
                kept.append(normalized)
                continue
            if self._is_internal_non_deliverable_html(path) or self._is_task_preview_fallback_html(path):
                dropped.append(path.name)
                try:
                    if path.exists() and path.is_file():
                        path.unlink()
                except Exception:
                    pass
                continue
            if not restrict_html_targets:
                kept.append(normalized)
                continue
            if path.name not in allowed_names:
                dropped.append(path.name)
                try:
                    if path.exists() and path.is_file():
                        path.unlink()
                except Exception:
                    pass
                if path.name == "index.html" and not self._builder_can_write_root_index(plan, subtask, plan.goal):
                    self._restore_root_index_after_secondary_builder(plan, subtask, prev_results=prev_results)
                    restored_root = True
                continue
            kept.append(normalized)

        if restored_root and "index.html" not in allowed_names:
            kept = [item for item in kept if Path(item).name != "index.html"]

        if not restrict_html_targets:
            created_html_names = {
                Path(item).name
                for item in kept
                if Path(item).suffix.lower() in (".html", ".htm")
            }
            for name in self._builder_bootstrap_targets(plan, subtask):
                scaffold_path = OUTPUT_DIR / name
                try:
                    if (
                        scaffold_path.exists()
                        and scaffold_path.is_file()
                        and is_bootstrap_html_artifact(scaffold_path)
                        and scaffold_path.name not in created_html_names
                    ):
                        scaffold_path.unlink()
                        dropped.append(scaffold_path.name)
                except Exception:
                    continue

        deduped: List[str] = []
        seen = set()
        for item in kept:
            normalized = self._normalize_generated_path(item)
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped, dropped

    def _restore_root_index_from_stable_preview(self) -> None:
        root_index = OUTPUT_DIR / "index.html"
        try:
            if self._run_started_at > 0 or (self._stable_preview_path and self._stable_preview_path.exists()):
                self._hydrate_stable_preview_from_disk()
            stable = self._stable_preview_path
            if stable and stable.exists() and stable.name == "index.html":
                root_index.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stable, root_index)
                return
        except Exception as e:
            logger.warning(f"Failed to restore root index from stable preview: {e}")
        if root_index.exists():
            logger.info("No stable preview snapshot available; keeping live root index.html in place.")

    def _hydrate_stable_preview_from_disk(self) -> None:
        if self._stable_preview_path and self._stable_preview_path.exists():
            return
        stable: Optional[Path] = None
        if self._run_started_at > 0:
            run_root = self._stable_preview_root()
            if not run_root.exists():
                return
            candidates: List[tuple[float, Path]] = []
            for snapshot_dir in sorted(run_root.iterdir()):
                if not snapshot_dir.is_dir():
                    continue
                html_files = [
                    html for html in snapshot_dir.rglob("*")
                    if html.is_file() and html.suffix.lower() in (".html", ".htm")
                ]
                preview_html = _preferred_preview_candidate(html_files, bucket_root=snapshot_dir)
                if preview_html is None:
                    continue
                candidates.append((_latest_mtime(html_files), preview_html))
            if not candidates:
                return
            candidates.sort(key=lambda item: item[0], reverse=True)
            stable = candidates[0][1]
        else:
            _task_id, stable = latest_stable_preview_artifact(OUTPUT_DIR)
            if not stable or not stable.exists():
                return

        if not stable or not stable.exists():
            return
        self._stable_preview_path = stable
        self._stable_preview_stage = "persisted_success"
        try:
            snapshot_root = stable.parent
            copied_files = [
                str(path)
                for path in sorted(snapshot_root.rglob("*"))
                if path.is_file()
            ]
            self._stable_preview_files = copied_files or [str(stable)]
        except Exception:
            self._stable_preview_files = [str(stable)]

    def _restore_output_from_stable_preview(self) -> List[str]:
        """
        Restore the last known-good stable snapshot into the live output root.

        This is used before a reviewer-requested rework so builder retries patch the
        strongest prior version instead of continuing from a partially-regressed set
        of live files.
        """
        self._hydrate_stable_preview_from_disk()
        stable = self._stable_preview_path
        if not stable or not stable.exists():
            return []

        try:
            snapshot_root = stable.parent.resolve()
            output_root = OUTPUT_DIR.resolve()
        except Exception:
            return []

        snapshot_rel_files: Dict[str, Path] = {}
        snapshot_html_rel: set[str] = set()
        for source in sorted(snapshot_root.rglob("*")):
            if not source.is_file():
                continue
            try:
                rel = source.resolve().relative_to(snapshot_root).as_posix()
            except Exception:
                continue
            snapshot_rel_files[rel] = source
            if source.suffix.lower() in (".html", ".htm"):
                snapshot_html_rel.add(rel)

        if not snapshot_rel_files:
            return []

        for current in sorted(OUTPUT_DIR.rglob("*")):
            if not current.is_file() or current.suffix.lower() not in (".html", ".htm"):
                continue
            try:
                rel = current.resolve().relative_to(output_root)
            except Exception:
                continue
            if rel.parts and rel.parts[0] in {"_stable_previews", "_builder_backups"}:
                continue
            if rel.as_posix() in snapshot_html_rel:
                continue
            try:
                current.unlink()
            except Exception:
                continue

        restored: List[str] = []
        for rel, source in snapshot_rel_files.items():
            dest = output_root / Path(rel)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                restored.append(str(dest))
            except Exception as exc:
                logger.warning("Failed to restore stable preview artifact %s → %s: %s", source, dest, exc)
        return restored

    def _builder_reviewer_retry_active(self, subtask: Optional["SubTask"]) -> bool:
        if not subtask or subtask.agent_type != "builder" or int(getattr(subtask, "retries", 0) or 0) < 1:
            return False
        err_text = str(getattr(subtask, "error", "") or "").strip().lower()
        desc_text = str(getattr(subtask, "description", "") or "").strip().lower()
        return "reviewer rejected" in err_text or "reviewer rework" in desc_text or "reviewer" in err_text

    def _builder_retry_regression_reasons(
        self,
        subtask: "SubTask",
        files_created: List[str],
    ) -> List[str]:
        if not self._builder_reviewer_retry_active(subtask):
            return []
        self._hydrate_stable_preview_from_disk()
        stable = self._stable_preview_path
        if not stable or not stable.exists():
            return []

        try:
            stable_root = stable.parent.resolve()
            output_root = OUTPUT_DIR.resolve()
        except Exception:
            return []

        stable_html_files = [
            path for path in stable_root.rglob("*")
            if path.is_file() and path.suffix.lower() in (".html", ".htm")
        ]
        current_html_files = [
            path for path in output_root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in (".html", ".htm")
            and not self._is_internal_non_deliverable_html(path)
            and "_stable_previews" not in path.parts
            and "_builder_backups" not in path.parts
        ]
        if not stable_html_files or not current_html_files:
            return []

        def _text_len(path: Path) -> int:
            try:
                return len(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                return 0

        stable_total_chars = sum(_text_len(path) for path in stable_html_files)
        current_total_chars = sum(_text_len(path) for path in current_html_files)
        stable_preview = _preferred_preview_candidate(stable_html_files, bucket_root=stable_root) or stable_html_files[0]
        current_preview = _preferred_preview_candidate(current_html_files, bucket_root=output_root) or current_html_files[0]
        stable_preview_chars = _text_len(stable_preview)
        current_preview_chars = _text_len(current_preview)

        reasons: List[str] = []
        if len(stable_html_files) >= 2 and len(current_html_files) <= max(1, len(stable_html_files) // 2):
            reasons.append(
                f"HTML page count collapsed from {len(stable_html_files)} to {len(current_html_files)}"
            )
        if stable_total_chars >= 6000 and current_total_chars <= max(2200, int(stable_total_chars * 0.35)):
            reasons.append(
                f"total HTML size collapsed from {stable_total_chars} chars to {current_total_chars}"
            )
        if stable_preview_chars >= 3000 and current_preview_chars <= max(1200, int(stable_preview_chars * 0.35)):
            reasons.append(
                f"primary preview size collapsed from {stable_preview_chars} chars to {current_preview_chars}"
            )
        if len(files_created) <= 1 and len(stable_html_files) >= 2:
            reasons.append(
                f"builder retry only produced {len(files_created)} file while the stable artifact already had {len(stable_html_files)} HTML pages"
            )
        return reasons[:3]

    def _cleanup_internal_builder_artifacts(self) -> List[str]:
        """
        Remove internal bootstrap / partial artifacts from the live output root so a
        failed builder run cannot leave the user with a mixed preview such as
        "old homepage + fresh blank scaffold pages".
        """
        removed: List[str] = []
        for current in sorted(OUTPUT_DIR.rglob("*")):
            if not current.is_file():
                continue
            name = current.name
            should_remove = (
                is_partial_html_artifact(current)
                or is_bootstrap_html_artifact(current)
                or name == "_partial_builder.html"
            )
            if not should_remove:
                continue
            try:
                current.unlink()
                removed.append(str(current))
            except Exception:
                continue
        return removed

    def _current_preview_hint(self, goal: str, *, allow_stable_fallback: bool = True) -> str:
        task_id, html_file = latest_preview_artifact(OUTPUT_DIR)
        source_label = "current_output"
        if (not html_file or not html_file.exists()) and allow_stable_fallback:
            stable = self._stable_preview_path if self._stable_preview_path and self._stable_preview_path.exists() else None
            if stable is not None:
                task_id, html_file = "current_run_stable", stable
                source_label = "stable_snapshot"
        if not html_file or not html_file.exists():
            return ""
        try:
            preview_url = build_preview_url_for_file(html_file, output_dir=OUTPUT_DIR)
        except Exception:
            return ""
        try:
            rel = html_file.resolve().relative_to(OUTPUT_DIR.resolve()).as_posix()
        except Exception:
            rel = str(html_file)
        lines = [
            f"Current preview artifact to inspect first: {preview_url}",
            f"Preview artifact path: {rel} ({source_label})",
        ]
        if task_classifier.wants_multi_page(goal):
            lines.append(
                "Treat this exact artifact as the active review target first, then follow its real navigation to inspect every linked page from the same output set."
            )
        return "\n".join(lines) + "\n"

    def _merge_reviewer_rework_into_builder_description(
        self,
        description: str,
        rejection_details: str,
        *,
        round_num: int,
        max_rejections: int,
    ) -> str:
        marker = "\n\n⚠️ REVIEWER REJECTED YOUR OUTPUT"
        base = str(description or "").strip()
        if marker in base:
            base = base.split(marker, 1)[0].rstrip()
        rework_block = (
            f"⚠️ REVIEWER REJECTED YOUR OUTPUT (round {round_num}/{max_rejections}). "
            f"You MUST fix these issues:\n"
            f"{str(rejection_details or '').strip()[:800]}\n\n"
            "Review the existing output artifacts, fix the problems, and keep the "
            "overall product consistent with the previously strongest version.\n"
        )
        return f"{base}\n\n{rework_block}".strip()

    def _current_run_html_artifacts(self) -> List[Path]:
        if not OUTPUT_DIR.exists():
            return []
        cutoff = max(self._run_started_at - 2.0, 0.0)
        html_files: List[Path] = []
        for html in OUTPUT_DIR.rglob("*"):
            if not html.is_file() or html.suffix.lower() not in (".html", ".htm"):
                continue
            if self._is_internal_non_deliverable_html(html) or self._is_task_preview_fallback_html(html):
                continue
            if self._run_started_at > 0:
                try:
                    if html.stat().st_mtime < cutoff:
                        continue
                except Exception:
                    continue
            html_files.append(html)
        root_index = OUTPUT_DIR / "index.html"
        if root_index.exists() and root_index.is_file():
            try:
                root_recent = root_index.stat().st_mtime >= cutoff if self._run_started_at > 0 else True
            except Exception:
                root_recent = False
            if (
                root_recent
                and not self._is_internal_non_deliverable_html(root_index)
                and not self._is_task_preview_fallback_html(root_index)
                and all(path.resolve() != root_index.resolve() for path in html_files)
            ):
                html_files.append(root_index)
        html_files.sort(key=lambda path: (path.name != "index.html", str(path)))
        return html_files

    def _current_run_css_artifacts(self) -> List[Path]:
        if not OUTPUT_DIR.exists():
            return []
        cutoff = max(self._run_started_at - 2.0, 0.0)
        css_files: List[Path] = []
        for css in OUTPUT_DIR.rglob("*.css"):
            if not css.is_file():
                continue
            if self._run_started_at > 0:
                try:
                    if css.stat().st_mtime < cutoff:
                        continue
                except Exception:
                    continue
            css_files.append(css)
        root_styles = OUTPUT_DIR / "styles.css"
        if root_styles.exists() and root_styles.is_file():
            try:
                root_recent = root_styles.stat().st_mtime >= cutoff if self._run_started_at > 0 else True
            except Exception:
                root_recent = False
            if root_recent and all(path.resolve() != root_styles.resolve() for path in css_files):
                css_files.append(root_styles)
        css_files.sort(key=lambda path: (path.name != "styles.css", str(path)))
        return css_files

    def _is_internal_output_artifact(self, path: Path) -> bool:
        item = path if isinstance(path, Path) else Path(str(path))
        if item.name == "_partial_builder.html":
            return True
        try:
            rel = item.resolve().relative_to(OUTPUT_DIR.resolve())
        except Exception:
            rel = item
        parts = rel.parts
        if not parts:
            return False
        if parts[0] in {"_stable_previews", "_builder_backups"}:
            return True
        return False

    def _current_run_deliverable_artifacts(self) -> List[Path]:
        if not OUTPUT_DIR.exists():
            return []
        cutoff = max(self._run_started_at - 2.0, 0.0)
        deliverable_exts = {
            ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".json",
            ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp", ".ico",
            ".mp4", ".webm", ".ogg", ".mp3", ".wav",
            ".woff", ".woff2", ".ttf", ".otf",
        }
        deliverables: List[Path] = []
        seen: set[str] = set()
        for item in OUTPUT_DIR.rglob("*"):
            if not item.is_file():
                continue
            if self._is_internal_output_artifact(item):
                continue
            if item.suffix.lower() not in deliverable_exts:
                continue
            if item.suffix.lower() in {".html", ".htm"} and (
                self._is_internal_non_deliverable_html(item)
                or self._is_task_preview_fallback_html(item)
            ):
                continue
            if item.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"} and re.match(
                r"^(tmp|temp|screenshot)",
                item.stem,
                re.IGNORECASE,
            ):
                continue
            if self._run_started_at > 0:
                try:
                    if item.stat().st_mtime < cutoff:
                        continue
                except Exception:
                    continue
            normalized = self._normalize_generated_path(str(item))
            if normalized in seen:
                continue
            seen.add(normalized)
            deliverables.append(item)
        deliverables.sort(
            key=lambda path: (
                0 if path.name == "index.html" else 1,
                0 if path.name == "styles.css" else 1,
                0 if path.name == "app.js" else 1,
                str(path),
            )
        )
        return deliverables

    def _collect_current_run_css_bundle(self) -> str:
        parts: List[str] = []
        for css_file in self._current_run_css_artifacts()[:12]:
            try:
                parts.append(css_file.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
        return "\n".join(parts)

    def _collect_css_bundle_for_artifacts(self, candidate_paths: List[str]) -> str:
        parts: List[str] = []
        seen: set[str] = set()
        for raw_path in candidate_paths or []:
            path = Path(str(raw_path))
            if not path.exists() or not path.is_file():
                continue
            normalized = self._normalize_generated_path(str(path))
            if path.suffix.lower() == ".css":
                if normalized in seen:
                    continue
                try:
                    parts.append(path.read_text(encoding="utf-8", errors="ignore"))
                    seen.add(normalized)
                except Exception:
                    continue
                continue
            if path.suffix.lower() not in (".html", ".htm"):
                continue
            try:
                html_text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            css_text = str(collect_stylesheet_context(html_text, path).get("css_text") or "").strip()
            if css_text:
                parts.append(css_text)
        return "\n".join(part for part in parts if str(part).strip())

    def _parse_css_color_token(self, token: str) -> Optional[tuple[int, int, int]]:
        value = str(token or "").strip().lower()
        if not value:
            return None
        hex_match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", value)
        if hex_match:
            raw = hex_match.group(1)
            if len(raw) == 3:
                raw = "".join(ch * 2 for ch in raw)
            try:
                return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))
            except Exception:
                return None
        rgb_match = re.fullmatch(
            r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*[\d.]+)?\s*\)",
            value,
        )
        if rgb_match:
            try:
                return tuple(max(0, min(255, int(rgb_match.group(i)))) for i in (1, 2, 3))
            except Exception:
                return None
        hsl_match = re.fullmatch(
            r"hsla?\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)%\s*,\s*(\d+(?:\.\d+)?)%(?:\s*,\s*[\d.]+)?\s*\)",
            value,
        )
        if hsl_match:
            try:
                hue = float(hsl_match.group(1)) % 360.0 / 360.0
                saturation = max(0.0, min(1.0, float(hsl_match.group(2)) / 100.0))
                lightness = max(0.0, min(1.0, float(hsl_match.group(3)) / 100.0))
                red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
                return (
                    int(round(red * 255)),
                    int(round(green * 255)),
                    int(round(blue * 255)),
                )
            except Exception:
                return None
        return None

    def _css_palette_signal_summary(self, css_text: str) -> Dict[str, Any]:
        text = str(css_text or "")
        if not text.strip():
            return {
                "distinct_color_count": 0,
                "accent_color_count": 0,
                "gradient_count": 0,
                "surface_var_count": 0,
                "root_flat_black_white": False,
                "flat_monochrome_risk": False,
            }

        color_tokens = set(re.findall(
            r"#[0-9a-fA-F]{3,6}\b|rgba?\([^)]+\)|hsla?\([^)]+\)",
            text,
        ))
        parsed_colors = {
            rgb
            for rgb in (
                self._parse_css_color_token(token)
                for token in color_tokens
            )
            if rgb is not None
        }
        accent_colors = {
            rgb for rgb in parsed_colors
            if (max(rgb) - min(rgb)) > 18 and max(rgb) > 28 and min(rgb) < 245
        }
        gradient_count = len(re.findall(r"(?:linear|radial|conic)-gradient\(", text, re.IGNORECASE))
        surface_var_count = len(set(re.findall(
            r"--(?:bg|surface|card|panel|accent|glow|primary|secondary|tone|tint|ink|canvas|layer)[a-z0-9_-]*\s*:",
            text,
            re.IGNORECASE,
        )))
        root_flat_black_white = bool(re.search(
            r"(?:^|[}\s])(?:html|body)\b[^{}]*\{[^{}]{0,240}background(?:-color)?\s*:\s*"
            r"(?:#(?:000|000000|111|111111|fff|ffffff)\b|rgba?\(\s*(?:0|17|255)\s*,\s*(?:0|17|255)\s*,\s*(?:0|17|255)(?:\s*,\s*[\d.]+)?\)|white\b|black\b)",
            text,
            re.IGNORECASE | re.DOTALL,
        ))
        flat_monochrome_risk = bool(
            root_flat_black_white
            and len(accent_colors) < 2
            and gradient_count < 2
            and surface_var_count < 4
        )
        return {
            "distinct_color_count": len(parsed_colors),
            "accent_color_count": len(accent_colors),
            "gradient_count": gradient_count,
            "surface_var_count": surface_var_count,
            "root_flat_black_white": root_flat_black_white,
            "flat_monochrome_risk": flat_monochrome_risk,
        }

    def _capture_route_signal_snapshot(self, html_files: Optional[List[Path]] = None) -> Dict[str, Any]:
        files = list(html_files or self._current_run_html_artifacts())
        css_bundle = self._collect_css_bundle_for_artifacts([str(path) for path in files])
        known_routes: List[str] = []
        for html_file in files[:24]:
            try:
                rel_path = self._normalize_preview_path(str(html_file.relative_to(OUTPUT_DIR)))
            except Exception:
                rel_path = html_file.name
            if rel_path and rel_path not in known_routes:
                known_routes.append(rel_path)
        routes: Dict[str, Dict[str, int]] = {}
        totals = {
            "route_count": 0,
            "text_chars": 0,
            "heading_count": 0,
            "interactive_count": 0,
            "media_nodes": 0,
            "photo_like_count": 0,
            "visual_anchor_count": 0,
            "linked_route_count": 0,
            "missing_route_count": 0,
        }
        for html_file in files[:24]:
            if not html_file.exists() or not html_file.is_file():
                continue
            try:
                html_text = html_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            try:
                rel_path = self._normalize_preview_path(str(html_file.relative_to(OUTPUT_DIR)))
            except Exception:
                rel_path = html_file.name
            local_css = str(collect_stylesheet_context(html_text, html_file).get("css_text") or "")
            combined_css = f"{css_bundle}\n{local_css}"
            stripped = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html_text, flags=re.IGNORECASE | re.DOTALL)
            text_only = re.sub(r"<[^>]+>", " ", stripped)
            text_only = re.sub(r"\s+", " ", text_only).strip()
            heading_count = len(re.findall(r"<h[1-6]\b", html_text, re.IGNORECASE))
            interactive_count = len(re.findall(
                r"<(?:a|button|input|textarea|select|summary)\b",
                html_text,
                re.IGNORECASE,
            ))
            media_nodes = len(re.findall(r"<(?:img|picture|video|canvas|svg)\b", html_text, re.IGNORECASE))
            photo_like_count = len(re.findall(r"<(?:img|picture|video)\b", html_text, re.IGNORECASE))
            photo_like_count += len(re.findall(
                r"background(?:-image)?\s*:\s*[^;{]*url\(",
                f"{combined_css}\n{html_text}",
                re.IGNORECASE,
            ))
            local_targets = set(self._extract_local_nav_targets(html_text))
            linked_route_count = 0
            for candidate in known_routes:
                if not candidate or candidate == rel_path:
                    continue
                alt = candidate[:-11] if candidate.endswith("/index.html") else ""
                if candidate in local_targets or (alt and alt in local_targets):
                    linked_route_count += 1
            missing_route_count = max(len(known_routes) - 1 - linked_route_count, 0)
            visual_anchor_count = 1 if (
                media_nodes > 0
                or photo_like_count > 0
                or re.search(r"(?:linear|radial|conic)-gradient\(", f"{combined_css}\n{html_text}", re.IGNORECASE)
            ) else 0
            routes[rel_path] = {
                "text_chars": len(text_only),
                "heading_count": heading_count,
                "interactive_count": interactive_count,
                "media_nodes": media_nodes,
                "photo_like_count": photo_like_count,
                "visual_anchor_count": visual_anchor_count,
                "linked_route_count": linked_route_count,
                "missing_route_count": missing_route_count,
            }
            totals["route_count"] += 1
            totals["text_chars"] += len(text_only)
            totals["heading_count"] += heading_count
            totals["interactive_count"] += interactive_count
            totals["media_nodes"] += media_nodes
            totals["photo_like_count"] += photo_like_count
            totals["visual_anchor_count"] += visual_anchor_count
            totals["linked_route_count"] += linked_route_count
            totals["missing_route_count"] += missing_route_count
        return {
            "routes": routes,
            "totals": totals,
            "palette": self._css_palette_signal_summary(css_bundle),
        }

    def _polisher_signal_regression_reasons(
        self,
        before: Dict[str, Any],
        after: Dict[str, Any],
        changed_routes: List[str],
    ) -> List[str]:
        reasons: List[str] = []
        before_totals = dict(before.get("totals") or {})
        after_totals = dict(after.get("totals") or {})
        before_palette = dict(before.get("palette") or {})
        after_palette = dict(after.get("palette") or {})

        before_text = int(before_totals.get("text_chars", 0) or 0)
        after_text = int(after_totals.get("text_chars", 0) or 0)
        if before_text >= 1200 and after_text < int(before_text * 0.72):
            reasons.append(f"text density collapsed from {before_text} to {after_text} chars")

        before_media = int(before_totals.get("media_nodes", 0) or 0)
        after_media = int(after_totals.get("media_nodes", 0) or 0)
        if before_media >= 4 and after_media < max(1, int(before_media * 0.55)):
            reasons.append(f"media nodes dropped from {before_media} to {after_media}")

        before_photos = int(before_totals.get("photo_like_count", 0) or 0)
        after_photos = int(after_totals.get("photo_like_count", 0) or 0)
        if before_photos >= 3 and after_photos < max(1, int(before_photos * 0.5)):
            reasons.append(f"photo / image anchors dropped from {before_photos} to {after_photos}")

        before_missing_routes = int(before_totals.get("missing_route_count", 0) or 0)
        after_missing_routes = int(after_totals.get("missing_route_count", 0) or 0)
        if after_missing_routes > before_missing_routes:
            reasons.append(
                f"cross-route navigation coverage regressed from {before_missing_routes} missing links to {after_missing_routes}"
            )

        if after_palette.get("flat_monochrome_risk") and not before_palette.get("flat_monochrome_risk"):
            reasons.append("palette collapsed into flat monochrome / black-white surfaces")

        before_routes = dict(before.get("routes") or {})
        after_routes = dict(after.get("routes") or {})
        for route in changed_routes[:8]:
            before_route = dict(before_routes.get(route) or {})
            after_route = dict(after_routes.get(route) or {})
            if not before_route or not after_route:
                continue
            before_route_text = int(before_route.get("text_chars", 0) or 0)
            after_route_text = int(after_route.get("text_chars", 0) or 0)
            before_route_headings = int(before_route.get("heading_count", 0) or 0)
            after_route_headings = int(after_route.get("heading_count", 0) or 0)
            if (
                before_route_text >= 260
                and before_route_headings >= 2
                and after_route_text < int(before_route_text * 0.55)
                and after_route_headings < before_route_headings
            ):
                reasons.append(f"{route} lost too much content density")
            if (
                int(before_route.get("visual_anchor_count", 0) or 0) > 0
                and int(after_route.get("visual_anchor_count", 0) or 0) == 0
            ):
                reasons.append(f"{route} lost its visual anchor")
            if int(after_route.get("missing_route_count", 0) or 0) > int(before_route.get("missing_route_count", 0) or 0):
                reasons.append(f"{route} lost shared route coverage")
        return reasons[:6]

    def _html_attr_value(self, attrs: str, name: str) -> str:
        if not attrs or not name:
            return ""
        match = re.search(
            rf'\b{re.escape(name)}\s*=\s*(["\'])(.*?)\1',
            attrs,
            re.IGNORECASE | re.DOTALL,
        )
        return str(match.group(2) or "").strip() if match else ""

    def _extract_class_tokens_from_markup(self, markup: str) -> List[str]:
        tokens: List[str] = []
        seen: set[str] = set()
        for match in re.finditer(
            r'class\s*=\s*(["\'])(.*?)\1',
            markup or "",
            re.IGNORECASE | re.DOTALL,
        ):
            raw = str(match.group(2) or "")
            for token in re.split(r"\s+", raw.strip()):
                cleaned = token.strip()
                lowered = cleaned.lower()
                if not cleaned or lowered in seen:
                    continue
                seen.add(lowered)
                tokens.append(cleaned)
                if len(tokens) >= 48:
                    return tokens
        return tokens

    def _strip_html_text(self, fragment: str) -> str:
        if not fragment:
            return ""
        text = re.sub(r"<script\b[^>]*>.*?</script>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    def _visual_block_has_rich_media_payload(self, attrs: str, body: str, css_bundle: str) -> bool:
        attrs_text = str(attrs or "")
        body_text = str(body or "")
        combined = f"{attrs_text} {body_text}"
        valid_img = re.search(
            r'<img\b[^>]*\bsrc=["\'](?!\s*(?:#|about:blank|data:,|javascript:void\(0\)|placeholder[^"\']*))[^"\']+["\']',
            combined,
            re.IGNORECASE,
        )
        if valid_img:
            return True
        if re.search(r"<(?:picture|video|canvas|iframe)\b", combined, re.IGNORECASE):
            return True
        if re.search(r"background(?:-image)?\s*:\s*[^;]*url\(", combined, re.IGNORECASE):
            return True
        class_attr = self._html_attr_value(attrs_text, "class")
        class_names = [token for token in re.split(r"\s+", class_attr) if token]
        css_text = f"{css_bundle}\n{body_text}"
        for class_name in class_names[:10]:
            selector = re.escape(class_name)
            if re.search(
                rf"\.{selector}\b[^{{}}]*\{{[^{{}}]*(?:background(?:-image)?\s*:\s*[^{{}};]*url\(|content\s*:\s*url\(|mask-image\s*:\s*url\()",
                css_text,
                re.IGNORECASE | re.DOTALL,
            ):
                return True
        return False

    def _visual_block_has_media_payload(self, attrs: str, body: str, css_bundle: str) -> bool:
        if self._visual_block_has_rich_media_payload(attrs, body, css_bundle):
            return True
        combined = f"{str(attrs or '')} {str(body or '')}"
        return bool(re.search(r"<svg\b", combined, re.IGNORECASE))

    def _visual_block_is_icon_placeholder(
        self,
        attrs: str,
        body: str,
        css_bundle: str,
        text_only: str = "",
    ) -> bool:
        attrs_text = str(attrs or "")
        body_text = str(body or "")
        markup = f"<div {attrs_text}>{body_text}</div>" if attrs_text else f"<div>{body_text}</div>"
        class_scope = " ".join(self._extract_class_tokens_from_markup(markup))
        text_len = len(text_only or self._strip_html_text(body_text))
        if text_len >= 80:
            return False
        if self._visual_block_has_rich_media_payload(attrs_text, body_text, css_bundle):
            return False

        high_risk_shell = bool(re.search(
            r"\b(?:experience-icon|featured-image-placeholder|featured-icon(?:-large)?|hero-pattern|page-hero-pattern)\b",
            class_scope,
            re.IGNORECASE,
        ))
        visual_shell = bool(re.search(
            r"\b(?:visual|hero|media|showcase|image|photo|cover|gallery|map|experience|report)\b",
            class_scope,
            re.IGNORECASE,
        ))
        icon_shell = bool(re.search(
            r"\b(?:icon|glyph|seal|pattern)\b",
            class_scope,
            re.IGNORECASE,
        ))
        if high_risk_shell and text_len < 80:
            return True

        combined = f"{attrs_text} {body_text}"
        if not re.search(r"<svg\b", combined, re.IGNORECASE):
            return False
        if not visual_shell:
            return False
        if icon_shell:
            return True

        svg_count = len(re.findall(r"<svg\b", combined, re.IGNORECASE))
        shape_count = len(
            re.findall(r"<(?:path|circle|rect|ellipse|line|polyline|polygon)\b", combined, re.IGNORECASE)
        )
        return svg_count == 1 and shape_count <= 16 and text_len < 40

    def _scan_html_visual_gaps(self, html_text: str, css_bundle: str = "") -> Dict[str, int]:
        counts: Dict[str, int] = {}

        def _bump(label: str, amount: int = 1) -> None:
            if amount <= 0:
                return
            counts[label] = counts.get(label, 0) + amount

        visual_patterns = [
            ("showcase-image", re.compile(r"\bshowcase-image\b", re.IGNORECASE)),
            ("collection-card-image", re.compile(r"\bcollection-card-image\b", re.IGNORECASE)),
            ("story-image", re.compile(r"\bstory-image\b", re.IGNORECASE)),
            ("map-placeholder", re.compile(r"\bmap-placeholder\b", re.IGNORECASE)),
            ("experience-visual", re.compile(r"\bexperience-visual\b", re.IGNORECASE)),
            ("experience-icon", re.compile(r"\bexperience-icon\b", re.IGNORECASE)),
            ("featured-image-placeholder", re.compile(r"\bfeatured-image-placeholder\b", re.IGNORECASE)),
            ("hero-pattern", re.compile(r"\bhero-pattern\b", re.IGNORECASE)),
            ("page-hero-pattern", re.compile(r"\bpage-hero-pattern\b", re.IGNORECASE)),
            ("report-visual", re.compile(r"\breport-visual\b", re.IGNORECASE)),
            ("placeholder", re.compile(r"\bplaceholder\b", re.IGNORECASE)),
        ]
        visual_class_hint = re.compile(
            r"\b(image|visual|media|gallery|map|placeholder|photo|cover|showcase|story|collection|experience|report|hero)\b",
            re.IGNORECASE,
        )
        placeholder_copy_re = re.compile(
            r"(\[[^\]]{0,80}(?:image|photo|visual|map|video|gallery|hero|cover|图|图片|照片|视觉|地图|视频|插画|展示)[^\]]{0,80}\]|"
            r"\b(?:placeholder|replace me|todo|tbd|coming soon)\b)",
            re.IGNORECASE,
        )

        block_re = re.compile(
            r"<(?P<tag>div|figure|section|article|aside|span)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in block_re.finditer(html_text or ""):
            attrs = str(match.group("attrs") or "")
            class_attr = self._html_attr_value(attrs, "class")
            if not class_attr or not visual_class_hint.search(class_attr):
                continue
            body = str(match.group("body") or "")
            style_attr = self._html_attr_value(attrs, "style")
            text_only = self._strip_html_text(body)
            text_len = len(text_only)
            class_scope = " ".join(
                self._extract_class_tokens_from_markup(f"<div {attrs}>{body}</div>" if attrs else f"<div>{body}</div>")
            )
            has_media = self._visual_block_has_media_payload(attrs, body, css_bundle)
            gradient_only = (
                bool(style_attr)
                and "gradient" in style_attr.lower()
                and "url(" not in style_attr.lower()
                and not has_media
                and text_len < 80
            )
            placeholder_copy = bool(placeholder_copy_re.search(text_only))
            placeholder_class = bool(re.search(r"\bplaceholder\b", class_attr, re.IGNORECASE))
            icon_placeholder = self._visual_block_is_icon_placeholder(
                attrs,
                body,
                css_bundle,
                text_only=text_only,
            )
            emptyish_visual = (not has_media) and (
                text_len == 0
                or placeholder_copy
                or text_len < 40
            )
            map_like = bool(re.search(r"\bmap\b", class_attr, re.IGNORECASE))

            if gradient_only:
                _bump("gradient-only visual blocks")
            if placeholder_copy:
                _bump("visual blocks still showing placeholder copy")
            if placeholder_class and not has_media:
                _bump("placeholder visual blocks")
            if map_like and not has_media and text_len < 80:
                _bump("empty map/location blocks")
            if icon_placeholder:
                _bump("icon/pattern placeholder visuals")
            if emptyish_visual:
                _bump("empty visual/media blocks")
            if emptyish_visual or icon_placeholder:
                for label, pattern in visual_patterns:
                    if pattern.search(class_scope):
                        _bump(label)

        broken_img_count = len(re.findall(
            r'<img\b(?:(?!\bsrc=)[^>])*?>|<img\b[^>]*\bsrc=["\']\s*(?:|#|about:blank|data:,|javascript:void\(0\)|placeholder[^"\']*)["\']',
            html_text or "",
            re.IGNORECASE,
        ))
        if broken_img_count > 0:
            _bump("img tags missing real source", broken_img_count)

        empty_picture_count = len(re.findall(
            r"<picture\b[^>]*>\s*(?:<source\b[^>]*>\s*)*</picture>",
            html_text or "",
            re.IGNORECASE | re.DOTALL,
        ))
        if empty_picture_count > 0:
            _bump("picture tags missing real image", empty_picture_count)

        return counts

    def _current_polisher_visual_gap_entries(self) -> List[str]:
        html_files = self._current_run_html_artifacts()
        if not html_files and OUTPUT_DIR.exists():
            html_files = sorted(
                path for path in OUTPUT_DIR.glob("*.htm*")
                if path.is_file()
                and not self._is_internal_non_deliverable_html(path)
                and not self._is_task_preview_fallback_html(path)
            )
        if not html_files:
            return []

        css_bundle = self._collect_current_run_css_bundle()
        page_html_by_path: Dict[str, str] = {}
        issues_by_file: Dict[str, List[str]] = {}
        ordered_paths: List[str] = []
        for html_file in html_files[:16]:
            try:
                rel_path = self._normalize_preview_path(str(html_file.relative_to(OUTPUT_DIR)))
            except Exception:
                rel_path = html_file.name
            try:
                text = html_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if rel_path not in ordered_paths:
                ordered_paths.append(rel_path)
            page_html_by_path[rel_path] = text
            counts = self._scan_html_visual_gaps(text, css_bundle=css_bundle)
            if not counts:
                continue
            notes = [f"{label} x{count}" for label, count in counts.items() if count > 0]
            if notes:
                issues_by_file.setdefault(rel_path, []).extend(notes[:8])
        missing_local_routes = self._collect_missing_local_html_routes(page_html_by_path)
        for rel_path, targets in missing_local_routes.items():
            if rel_path not in ordered_paths:
                ordered_paths.append(rel_path)
            issues_by_file.setdefault(rel_path, []).append(
                "broken local routes -> " + ", ".join(targets[:8])
            )
        missing_cross_route_links = self._collect_missing_cross_route_links(page_html_by_path, ordered_paths)
        for rel_path, targets in missing_cross_route_links.items():
            if rel_path not in ordered_paths:
                ordered_paths.append(rel_path)
            issues_by_file.setdefault(rel_path, []).append(
                "incomplete route coverage -> " + ", ".join(targets[:8])
            )
        rendered: List[str] = []
        for rel_path in ordered_paths[:16]:
            notes = [str(item) for item in (issues_by_file.get(rel_path) or []) if str(item or "").strip()]
            if not notes:
                continue
            rendered.append(f"- {rel_path}: " + ", ".join(notes[:8]))
        return rendered

    def _polisher_visual_gap_report(self) -> str:
        rendered = self._current_polisher_visual_gap_entries()
        if not rendered:
            return ""
        return (
            "[Visual Gap Report — Mandatory Polish Targets]\n"
            "The current artifact still contains likely unfinished media/visual placeholders or broken local page routes:\n"
            + "\n".join(rendered[:12])
            + "\n"
            "Priority: replace these with finished premium visuals, richer composition, working local navigation, and stronger motion. "
            "Use shared CSS/JS upgrades first when possible, then patch the affected HTML routes.\n"
        )

    def _polisher_gap_gate_errors(self) -> List[str]:
        entries = self._current_polisher_visual_gap_entries()
        return entries[:6]

    def _visual_gap_entries_for_paths(self, candidate_paths: List[str]) -> List[str]:
        html_files: List[Path] = []
        css_files: List[Path] = []
        seen_html: set[str] = set()
        seen_css: set[str] = set()

        for raw_path in candidate_paths or []:
            path = Path(str(raw_path))
            if not path.exists() or not path.is_file():
                continue
            suffix = path.suffix.lower()
            try:
                normalized = str(path.resolve())
            except Exception:
                normalized = str(path)
            if suffix in (".html", ".htm") and normalized not in seen_html:
                seen_html.add(normalized)
                html_files.append(path)
            elif suffix == ".css" and normalized not in seen_css:
                seen_css.add(normalized)
                css_files.append(path)

        if not html_files:
            return []

        if not css_files:
            for html_file in html_files:
                sibling_css = html_file.parent / "styles.css"
                if sibling_css.exists() and sibling_css.is_file():
                    try:
                        normalized = str(sibling_css.resolve())
                    except Exception:
                        normalized = str(sibling_css)
                    if normalized not in seen_css:
                        seen_css.add(normalized)
                        css_files.append(sibling_css)

        css_bundle_parts: List[str] = []
        for css_file in css_files[:12]:
            try:
                css_bundle_parts.append(css_file.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
        css_bundle = "\n".join(css_bundle_parts)

        rendered: List[str] = []
        for html_file in html_files[:16]:
            try:
                rel_path = self._normalize_preview_path(str(html_file.relative_to(OUTPUT_DIR)))
            except Exception:
                rel_path = html_file.name
            try:
                text = html_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            counts = self._scan_html_visual_gaps(text, css_bundle=css_bundle)
            if not counts:
                continue
            notes = [f"{label} x{count}" for label, count in counts.items() if count > 0]
            if notes:
                rendered.append(f"- {rel_path}: " + ", ".join(notes[:8]))
        return rendered

    def _merge_builder_runtime_html_files(
        self,
        plan: Plan,
        subtask: SubTask,
        files_created: List[str],
    ) -> List[str]:
        if subtask.agent_type != "builder" or not self._is_multi_page_website_goal(plan.goal):
            return files_created

        cutoff = max((subtask.started_at or self._run_started_at) - 1.5, 0.0)
        if cutoff <= 0:
            return files_created

        merged: List[str] = []
        seen: set[str] = set()
        for item in files_created or []:
            normalized = self._normalize_generated_path(item)
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)

        allowed_names: set[str] = set()
        if self._builder_html_target_restriction_enabled(plan, subtask):
            allowed_names = {
                Path(name).name
                for name in self._builder_bootstrap_targets(plan, subtask)
                if str(name or "").strip()
            }

        for html_file in self._current_run_html_artifacts():
            try:
                if html_file.stat().st_mtime < cutoff:
                    continue
            except Exception:
                continue
            if allowed_names and html_file.name not in allowed_names:
                continue
            normalized = self._normalize_generated_path(str(html_file))
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
        return merged

    def _should_suppress_builder_browser(
        self,
        plan: Plan,
        subtask: SubTask,
        repo_context: Optional[Dict[str, Any]],
    ) -> bool:
        if subtask.agent_type != "builder":
            return False
        if repo_context:
            return False
        try:
            profile = task_classifier.classify(plan.goal)
        except Exception:
            return False
        if profile.task_type != "website":
            return False
        upstream_specialists = {"analyst", "uidesign", "scribe"}
        dep_tasks = [next((st for st in plan.subtasks if st.id == dep_id), None) for dep_id in subtask.depends_on]
        return any(task and task.agent_type in upstream_specialists for task in dep_tasks)

    def _fallback_dependency_handoff(self, dep_task: Optional[SubTask], goal: str) -> str:
        if dep_task is None:
            return ""
        page_count = max(task_classifier.requested_page_count(goal), 1)
        if dep_task.agent_type == "scribe":
            return (
                "[Scribe Fallback Handoff]\n"
                f"- Keep a clear page-by-page narrative for {page_count} page(s).\n"
                "- Each page must contain substantive headings, body copy, and supporting modules instead of thin placeholders.\n"
                "- The homepage should establish the premium story, middle pages should deepen products/craft/history, and the final page should close with trust/contact/CTA content.\n"
                "- Preserve consistent naming and navigation across all linked HTML pages."
            )
        if dep_task.agent_type == "uidesign":
            return (
                "[UI Design Fallback Handoff]\n"
                "- Maintain one coherent premium design system across every page.\n"
                "- Use restrained luxury styling: strong whitespace, premium typography, subtle cinematic motion, and consistent navigation.\n"
                "- Avoid blank bands, broken sections, or mismatched visual language between pages."
            )
        return ""

    def _extract_local_nav_targets(self, html: str) -> List[str]:
        targets: List[str] = []
        for href in re.findall(r'href=["\']([^"\']+)["\']', html or "", re.IGNORECASE):
            value = str(href or "").strip()
            if not value or value.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            parsed = urlparse(value)
            if parsed.scheme and parsed.scheme not in {"http", "https"}:
                continue
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                continue
            normalized = self._normalize_preview_path(value)
            if normalized and normalized not in targets:
                targets.append(normalized)
        return targets

    def _is_local_html_route_path(self, rel_path: str) -> bool:
        text = str(rel_path or "").strip().lower()
        return bool(text) and (
            text.endswith((".html", ".htm"))
            or text.endswith("/index.html")
        )

    def _collect_missing_local_html_routes(
        self,
        page_html_by_path: Dict[str, str],
        available_paths: Optional[List[str]] = None,
    ) -> Dict[str, List[str]]:
        route_set = {
            self._normalize_preview_path(path)
            for path in (available_paths or list(page_html_by_path.keys()))
            if self._is_local_html_route_path(path)
        }
        missing: Dict[str, List[str]] = {}
        for rel_path, html in page_html_by_path.items():
            missing_targets: List[str] = []
            for target in self._extract_local_nav_targets(html):
                if not self._is_local_html_route_path(target):
                    continue
                if target in route_set:
                    continue
                if target not in missing_targets:
                    missing_targets.append(target)
            if missing_targets:
                missing[rel_path] = missing_targets
        return missing

    def _collect_missing_cross_route_links(
        self,
        page_html_by_path: Dict[str, str],
        available_paths: Optional[List[str]] = None,
    ) -> Dict[str, List[str]]:
        route_paths = [
            self._normalize_preview_path(path)
            for path in (available_paths or list(page_html_by_path.keys()))
            if self._is_local_html_route_path(path)
        ]
        ordered_routes: List[str] = []
        for route in route_paths:
            if route and route not in ordered_routes:
                ordered_routes.append(route)

        missing: Dict[str, List[str]] = {}
        for rel_path, html in page_html_by_path.items():
            if not self._is_local_html_route_path(rel_path):
                continue
            targets = set(self._extract_local_nav_targets(html))
            missing_targets: List[str] = []
            for candidate in ordered_routes:
                if not candidate or candidate == rel_path:
                    continue
                alt = candidate[:-11] if candidate.endswith("/index.html") else ""
                if candidate in targets or (alt and alt in targets):
                    continue
                missing_targets.append(candidate)
            if missing_targets:
                missing[rel_path] = missing_targets
        return missing

    def _nav_label_for_preview_path(self, rel_path: str) -> str:
        path = OUTPUT_DIR / Path(str(rel_path or "")).name
        try:
            if path.exists() and path.is_file():
                page_html = path.read_text(encoding="utf-8", errors="ignore")
                for pattern in (
                    r"<title[^>]*>(.*?)</title>",
                    r"<h1[^>]*>(.*?)</h1>",
                ):
                    match = re.search(pattern, page_html, re.IGNORECASE | re.DOTALL)
                    if not match:
                        continue
                    raw = re.sub(r"<[^>]+>", " ", match.group(1))
                    raw = unescape(re.sub(r"\s+", " ", raw)).strip()
                    raw = re.split(r"\s*[|｜—–-]\s*", raw, maxsplit=1)[0].strip()
                    if 2 <= len(raw) <= 42:
                        return raw
        except Exception:
            pass

        stem = Path(str(rel_path or "")).stem.replace("-", " ").replace("_", " ").strip()
        if not stem:
            return str(rel_path or "").strip()
        if stem.lower() in {"index", "home"}:
            return "Home"
        return stem.title()

    def _auto_patch_root_navigation(self, gate: Dict[str, Any]) -> bool:
        existing_secondary = [
            str(item)
            for item in (gate.get("html_files") or [])
            if str(item or "").strip() and str(item) != "index.html"
        ]
        if not existing_secondary:
            return False

        index_path = OUTPUT_DIR / "index.html"
        if not index_path.exists() or not index_path.is_file():
            return False

        try:
            index_html = index_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return False

        nav_targets = self._extract_local_nav_targets(index_html)
        missing_pages: List[str] = []
        for rel_path in existing_secondary:
            alt = rel_path[:-11] if rel_path.endswith("/index.html") else ""
            if rel_path in nav_targets or (alt and alt in nav_targets):
                continue
            missing_pages.append(rel_path)
        broken_targets = [
            str(item)
            for item in (gate.get("missing_nav_targets") or [])
            if self._is_local_html_route_path(str(item or "")) and str(item) != "index.html"
        ]
        if not missing_pages and not broken_targets:
            return False

        is_zh = bool(re.search(r"[\u3400-\u9fff]", index_html or ""))
        heading = "更多页面导航" if is_zh else "Explore More Pages"
        subheading = (
            "快速进入站内所有已生成页面"
            if is_zh else
            "Quick access to every generated page in this site"
        )
        links_markup = "\n".join(
            f'      <a href="{rel_path}" class="evermind-site-map__link">{self._nav_label_for_preview_path(rel_path)}</a>'
            for rel_path in existing_secondary
        )
        nav_block = (
            f'\n<nav class="evermind-site-map" data-evermind-site-map aria-label="{heading}">\n'
            f'  <div class="evermind-site-map__meta">\n'
            f'    <span class="evermind-site-map__eyebrow">Site Map</span>\n'
            f'    <strong>{heading}</strong>\n'
            f'    <p>{subheading}</p>\n'
            f'  </div>\n'
            f'  <div class="evermind-site-map__links">\n{links_markup}\n  </div>\n'
            f'</nav>\n'
        )
        nav_style = (
            '\n<style data-evermind-site-map-style>\n'
            '.evermind-site-map{margin:32px 24px 0;padding:18px 20px;border:1px solid rgba(255,255,255,.12);'
            'border-radius:20px;background:rgba(255,255,255,.04);backdrop-filter:blur(12px)}\n'
            '.evermind-site-map__meta{display:grid;gap:6px;margin-bottom:14px}\n'
            '.evermind-site-map__eyebrow{text-transform:uppercase;letter-spacing:.18em;font-size:.72rem;opacity:.65}\n'
            '.evermind-site-map__meta p{margin:0;opacity:.78;font-size:.94rem}\n'
            '.evermind-site-map__links{display:flex;flex-wrap:wrap;gap:10px}\n'
            '.evermind-site-map__link{display:inline-flex;align-items:center;min-height:40px;padding:10px 14px;'
            'border-radius:999px;border:1px solid rgba(255,255,255,.12);text-decoration:none;color:inherit;'
            'background:rgba(255,255,255,.03);transition:transform .28s ease,background .28s ease,border-color .28s ease}\n'
            '.evermind-site-map__link:hover,.evermind-site-map__link:focus-visible{transform:translateY(-1px);'
            'background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.22)}\n'
            '@media (max-width:720px){.evermind-site-map{margin:24px 16px 0;padding:16px}.evermind-site-map__links{gap:8px}'
            '.evermind-site-map__link{width:100%;justify-content:center}}\n'
            '</style>\n'
        )

        updated_html = index_html
        for broken_target, replacement in zip(broken_targets, missing_pages):
            if broken_target == replacement:
                continue
            updated_html = re.sub(
                rf'(\bhref\s*=\s*["\']){re.escape(broken_target)}(["\'])',
                rf"\1{replacement}\2",
                updated_html,
                flags=re.IGNORECASE,
            )
        if broken_targets and not missing_pages:
            for broken_target in broken_targets:
                updated_html = re.sub(
                    rf"\s*<li\b[^>]*>\s*<a\b[^>]*href=[\"']{re.escape(broken_target)}[\"'][^>]*>.*?</a>\s*</li>",
                    "",
                    updated_html,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                updated_html = re.sub(
                    rf"\s*<a\b[^>]*href=[\"']{re.escape(broken_target)}[\"'][^>]*>.*?</a>",
                    "",
                    updated_html,
                    flags=re.IGNORECASE | re.DOTALL,
                )
        if "data-evermind-site-map-style" not in updated_html:
            if "</head>" in updated_html.lower():
                updated_html = re.sub(r"</head>", nav_style + "</head>", updated_html, count=1, flags=re.IGNORECASE)
            else:
                updated_html = nav_style + updated_html

        has_existing_site_map_nav = bool(
            re.search(
                r"<nav\b[^>]*\bdata-evermind-site-map(?:\b|=)",
                updated_html,
                re.IGNORECASE,
            )
        )
        if has_existing_site_map_nav:
            updated_html = re.sub(
                r"<nav[^>]*data-evermind-site-map[^>]*>.*?</nav>",
                nav_block.strip(),
                updated_html,
                count=1,
                flags=re.IGNORECASE | re.DOTALL,
            )
        elif "</footer>" in updated_html.lower():
            updated_html = re.sub(r"</footer>", nav_block + "</footer>", updated_html, count=1, flags=re.IGNORECASE)
        elif "</body>" in updated_html.lower():
            updated_html = re.sub(r"</body>", nav_block + "</body>", updated_html, count=1, flags=re.IGNORECASE)
        else:
            updated_html += nav_block

        if updated_html == index_html:
            return False

        try:
            index_path.write_text(updated_html, encoding="utf-8")
            logger.info(
                "Auto-patched root navigation sitemap for multi-page site: missing_links=%s",
                missing_pages,
            )
            return True
        except Exception as exc:
            logger.warning("Failed to auto-patch root navigation sitemap: %s", exc)
            return False

    def _auto_patch_secondary_navigation(self, gate: Dict[str, Any]) -> bool:
        """Repair secondary route coverage by fixing dead links and appending a site map when pages are missing."""
        existing_html = [
            str(item)
            for item in (gate.get("html_files") or [])
            if str(item or "").strip()
        ]
        if not existing_html or "index.html" not in existing_html:
            return False
        ordered_html = list(dict.fromkeys(existing_html))
        existing_html_set = set(ordered_html)

        patched_any = False
        for rel_path in ordered_html:
            if rel_path == "index.html":
                continue
            page_path = OUTPUT_DIR / rel_path
            if not page_path.exists():
                continue
            try:
                html = page_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            original_html = html
            # Find all local .html hrefs in this page
            local_hrefs = re.findall(r'href=["\']([^"\'#?]+\.html)["\']', html, re.IGNORECASE)
            for href in local_hrefs:
                normalized = href.split("/")[-1]  # strip any relative path prefix
                if normalized in existing_html_set or normalized == "index.html":
                    continue
                # This href points to a non-existent page — remove the link or replace with index.html
                html = re.sub(
                    rf'(\bhref\s*=\s*["\']){re.escape(href)}(["\'])',
                    r'\1index.html\2',
                    html,
                    flags=re.IGNORECASE,
                )

            targets = set(self._extract_local_nav_targets(html))
            missing_pages: List[str] = []
            for candidate in ordered_html:
                if candidate == rel_path:
                    continue
                alt = candidate[:-11] if candidate.endswith("/index.html") else ""
                if candidate in targets or (alt and alt in targets):
                    continue
                missing_pages.append(candidate)

            if missing_pages:
                is_zh = bool(re.search(r"[\u3400-\u9fff]", html or ""))
                heading = "继续浏览" if is_zh else "Continue Exploring"
                subheading = (
                    "快速前往本站其余已生成页面"
                    if is_zh else
                    "Quick access to the remaining generated pages in this site"
                )
                links_markup = "\n".join(
                    f'      <a href="{target}" class="evermind-site-map__link">{self._nav_label_for_preview_path(target)}</a>'
                    for target in ordered_html
                    if target != rel_path
                )
                nav_block = (
                    f'\n<nav class="evermind-site-map evermind-site-map--secondary" data-evermind-site-map aria-label="{heading}">\n'
                    f'  <div class="evermind-site-map__meta">\n'
                    f'    <span class="evermind-site-map__eyebrow">Site Map</span>\n'
                    f'    <strong>{heading}</strong>\n'
                    f'    <p>{subheading}</p>\n'
                    f'  </div>\n'
                    f'  <div class="evermind-site-map__links">\n{links_markup}\n  </div>\n'
                    f'</nav>\n'
                )
                nav_style = (
                    '\n<style data-evermind-site-map-style>\n'
                    '.evermind-site-map{margin:32px 24px 0;padding:18px 20px;border:1px solid rgba(255,255,255,.12);'
                    'border-radius:20px;background:rgba(255,255,255,.04);backdrop-filter:blur(12px)}\n'
                    '.evermind-site-map__meta{display:grid;gap:6px;margin-bottom:14px}\n'
                    '.evermind-site-map__eyebrow{text-transform:uppercase;letter-spacing:.18em;font-size:.72rem;opacity:.65}\n'
                    '.evermind-site-map__meta p{margin:0;opacity:.78;font-size:.94rem}\n'
                    '.evermind-site-map__links{display:flex;flex-wrap:wrap;gap:10px}\n'
                    '.evermind-site-map__link{display:inline-flex;align-items:center;min-height:40px;padding:10px 14px;'
                    'border-radius:999px;border:1px solid rgba(255,255,255,.12);text-decoration:none;color:inherit;'
                    'background:rgba(255,255,255,.03);transition:transform .28s ease,background .28s ease,border-color .28s ease}\n'
                    '.evermind-site-map__link:hover,.evermind-site-map__link:focus-visible{transform:translateY(-1px);'
                    'background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.22)}\n'
                    '@media (max-width:720px){.evermind-site-map{margin:24px 16px 0;padding:16px}.evermind-site-map__links{gap:8px}'
                    '.evermind-site-map__link{width:100%;justify-content:center}}\n'
                    '</style>\n'
                )
                if "data-evermind-site-map-style" not in html:
                    if "</head>" in html.lower():
                        html = re.sub(r"</head>", nav_style + "</head>", html, count=1, flags=re.IGNORECASE)
                    else:
                        html = nav_style + html
                if re.search(r"<nav\b[^>]*data-evermind-site-map[^>]*>.*?</nav>", html, re.IGNORECASE | re.DOTALL):
                    html = re.sub(
                        r"<nav\b[^>]*data-evermind-site-map[^>]*>.*?</nav>",
                        nav_block.strip(),
                        html,
                        count=1,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                elif "</footer>" in html.lower():
                    html = re.sub(r"</footer>", nav_block + "</footer>", html, count=1, flags=re.IGNORECASE)
                elif "</body>" in html.lower():
                    html = re.sub(r"</body>", nav_block + "</body>", html, count=1, flags=re.IGNORECASE)
                else:
                    html += nav_block

            if html != original_html:
                try:
                    page_path.write_text(html, encoding="utf-8")
                    logger.info(
                        "Auto-patched secondary navigation for %s: repaired dead links and added site coverage for %s",
                        rel_path,
                        missing_pages[:8],
                    )
                    patched_any = True
                except Exception as exc:
                    logger.warning("Failed to auto-patch secondary nav for %s: %s", rel_path, exc)

        return patched_any

    def _goal_language_mismatch_reason(self, goal: str, html: str) -> str:
        requested = task_classifier.requested_output_language(goal)
        if requested not in {"en", "zh"}:
            return ""
        visible = re.sub(
            r"<(style|script)\b[^>]*>.*?</\1>",
            " ",
            html or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
        visible = re.sub(r"<[^>]+>", " ", visible)
        visible = re.sub(r"\s+", " ", visible).strip()
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", visible))
        latin_count = len(re.findall(r"[A-Za-z]", visible))
        lower = (html or "").lower()

        if requested == "en":
            lang_conflict = bool(re.search(r"<html[^>]+lang=[\"']zh", lower))
            content_conflict = cjk_count >= 40 and cjk_count > max(24, int(latin_count * 0.45))
            if lang_conflict or content_conflict:
                return "Goal explicitly requested English, but the generated page is still largely Chinese/CJK."

        if requested == "zh":
            lang_conflict = bool(re.search(r"<html[^>]+lang=[\"']en", lower))
            content_conflict = latin_count >= 120 and latin_count > max(80, cjk_count * 3)
            if lang_conflict or content_conflict:
                return "Goal explicitly requested Chinese, but the generated page is still largely English."

        return ""

    def _salvage_builder_partial_output(self, plan: Plan, subtask: SubTask, partial_output: str) -> List[str]:
        if subtask.agent_type != "builder":
            return []
        text = str(partial_output or "").strip()
        if len(text) < 120:
            return []
        files = self._extract_and_save_code(
            text,
            subtask.id,
            allow_root_index_copy=self._builder_can_write_root_index(plan, subtask, plan.goal),
            multi_page_required=task_classifier.wants_multi_page(plan.goal),
            allowed_html_targets=self._builder_allowed_html_targets(plan, subtask) or None,
            allow_multi_page_raw_html_fallback=True,
            allow_named_shared_asset_blocks=not self._builder_nav_repair_retry_active(subtask),
        )
        deduped: List[str] = []
        seen = set()
        for item in files:
            normalized = self._normalize_generated_path(item)
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def _homepage_builder_task(self, plan: Optional[Plan]) -> Optional[SubTask]:
        if not plan:
            return None
        builders = [st for st in plan.subtasks if st.agent_type == "builder"]
        if not builders:
            return None
        for builder in builders:
            if self._builder_can_write_root_index(plan, builder, plan.goal):
                return builder
        return builders[0]

    def _is_internal_non_deliverable_html(self, html: Path) -> bool:
        path = html if isinstance(html, Path) else Path(str(html))
        if is_partial_html_artifact(path) or is_bootstrap_html_artifact(path):
            return True
        try:
            rel = path.resolve().relative_to(OUTPUT_DIR.resolve())
        except Exception:
            rel = path
        parts = rel.parts
        if parts and parts[0] == "_stable_previews":
            return True
        return False

    def _is_task_preview_fallback_html(self, html: Path) -> bool:
        path = html if isinstance(html, Path) else Path(str(html))
        try:
            rel = path.resolve().relative_to(OUTPUT_DIR.resolve())
        except Exception:
            rel = path
        parts = rel.parts
        return len(parts) >= 2 and parts[0].startswith("task_") and path.name == "index.html"

    def _evaluate_multi_page_artifacts(self, goal: str) -> Dict[str, Any]:
        if not task_classifier.wants_multi_page(goal):
            return {
                "ok": True,
                "expected_pages": 1,
                "html_files": [],
                "observed_html_files": [],
                "invalid_html_files": [],
                "errors": [],
                "warnings": [],
                "repair_scope": "none",
                "nav_targets": [],
                "matched_nav_targets": [],
                "missing_nav_targets": [],
                "unlinked_secondary_pages": [],
            }

        expected_pages = max(task_classifier.requested_page_count(goal), 2)
        html_files = self._current_run_html_artifacts()
        observed_html_paths = [self._normalize_preview_path(str(path.relative_to(OUTPUT_DIR))) for path in html_files]
        valid_html_paths: List[str] = []
        invalid_html_files: List[str] = []
        invalid_details: List[str] = []
        shared_script_errors: List[str] = []
        shared_script_warnings: List[str] = []
        seen_shared_script_errors: set[str] = set()
        seen_shared_script_warnings: set[str] = set()
        page_html_by_path: Dict[str, str] = {}
        index_html = ""
        index_is_valid = False

        for path in html_files:
            rel_path = self._normalize_preview_path(str(path.relative_to(OUTPUT_DIR)))
            try:
                page_html = path.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                invalid_html_files.append(rel_path)
                invalid_details.append(f"{rel_path} (Failed to read HTML: {str(e)[:120]})")
                continue

            integrity = inspect_html_integrity(page_html)
            if not integrity.get("ok", True):
                issues = "; ".join(str(item) for item in (integrity.get("errors") or [])[:3])
                invalid_html_files.append(rel_path)
                invalid_details.append(f"{rel_path} ({issues})")
                if rel_path == "index.html":
                    index_html = page_html
                continue

            valid_html_paths.append(rel_path)
            page_html_by_path[rel_path] = page_html
            script_safety = inspect_shared_local_script_safety(page_html, path)
            for item in script_safety.get("errors", []) or []:
                text = str(item or "").strip()
                if text and text not in seen_shared_script_errors:
                    seen_shared_script_errors.add(text)
                    shared_script_errors.append(text)
            for item in script_safety.get("warnings", []) or []:
                text = str(item or "").strip()
                if text and text not in seen_shared_script_warnings:
                    seen_shared_script_warnings.add(text)
                    shared_script_warnings.append(text)
            if rel_path == "index.html":
                index_html = page_html
                index_is_valid = True
        errors: List[str] = []
        warnings: List[str] = []
        nav_targets: List[str] = []
        matched_nav_targets: List[str] = []
        missing_nav_targets: List[str] = []
        unlinked_secondary_pages: List[str] = []
        broken_local_nav_entries: List[str] = []
        existing_secondary = [path for path in valid_html_paths if path != "index.html"]

        if invalid_details:
            errors.append(
                "Invalid or corrupted HTML pages detected: " + ", ".join(invalid_details[:8])
            )
        if shared_script_errors:
            errors.extend(shared_script_errors[:6])
        if shared_script_warnings:
            warnings.extend(shared_script_warnings[:4])

        if len(valid_html_paths) < expected_pages:
            errors.append(
                f"Multi-page delivery incomplete: found {len(valid_html_paths)}/{expected_pages} valid HTML pages in the current run."
            )

        missing_local_routes = self._collect_missing_local_html_routes(page_html_by_path, valid_html_paths)
        if index_is_valid:
            nav_targets = self._extract_local_nav_targets(index_html)
            required_nav_links = min(max(expected_pages - 1, 1), len(existing_secondary))
            for path in nav_targets:
                if not self._is_local_html_route_path(path):
                    continue
                if path == "index.html":
                    continue
                if path in existing_secondary or f"{path}/index.html" in existing_secondary:
                    matched_nav_targets.append(path)
                elif path not in missing_nav_targets:
                    missing_nav_targets.append(path)
            for secondary_path in existing_secondary:
                if secondary_path in nav_targets:
                    continue
                secondary_route = secondary_path[:-11] if secondary_path.endswith("/index.html") else secondary_path
                if secondary_route in nav_targets:
                    continue
                unlinked_secondary_pages.append(secondary_path)
            for missing_target in missing_local_routes.get("index.html", []):
                if missing_target not in missing_nav_targets:
                    missing_nav_targets.append(missing_target)
            if missing_nav_targets:
                errors.append(
                    "index.html references missing local pages: "
                    + ", ".join(str(item) for item in missing_nav_targets[:8])
                )
            if existing_secondary and len(matched_nav_targets) < required_nav_links:
                errors.append(
                    "index.html does not expose enough working local navigation links to the additional pages."
                )
        else:
            if "index.html" in invalid_html_files:
                errors.append("Multi-page delivery root index.html exists but is invalid or corrupted.")
            else:
                errors.append("Multi-page delivery missing root index.html entry page.")

        secondary_broken_routes = {
            rel_path: targets
            for rel_path, targets in missing_local_routes.items()
            if rel_path != "index.html" and targets
        }
        if secondary_broken_routes:
            broken_local_nav_entries = [
                f"{rel_path} -> {', '.join(targets[:4])}"
                for rel_path, targets in list(secondary_broken_routes.items())[:6]
            ]
            errors.append(
                "Broken local navigation links detected: " + "; ".join(broken_local_nav_entries)
            )

        secondary_missing_route_links = {
            rel_path: targets
            for rel_path, targets in self._collect_missing_cross_route_links(page_html_by_path, valid_html_paths).items()
            if rel_path != "index.html" and targets
        }
        secondary_missing_nav_entries: List[str] = []
        if secondary_missing_route_links:
            secondary_missing_nav_entries = [
                f"{rel_path} missing {', '.join(targets[:4])}"
                for rel_path, targets in list(secondary_missing_route_links.items())[:6]
            ]
            errors.append(
                "Shared navigation is incomplete on generated pages: " + "; ".join(secondary_missing_nav_entries)
            )

        repair_scope = "none"
        nav_only_error = any(
            (
                "index.html does not expose enough working local navigation links" in err
                or "index.html references missing local pages" in err
            )
            for err in errors
        )
        has_secondary_nav_breaks = any(
            "Broken local navigation links detected" in err
            or "Shared navigation is incomplete on generated pages" in err
            for err in errors
        )
        has_invalid_html_error = any(
            "Invalid or corrupted HTML pages detected" in err
            or "root index.html exists but is invalid or corrupted" in err
            for err in errors
        )
        page_count_or_root_error = any(
            ("Multi-page delivery incomplete" in err) or ("missing root index.html" in err)
            for err in errors
        )
        if errors:
            all_nav_errors = all(
                ("navigation" in err.lower() or "nav" in err.lower()
                 or "local pages" in err.lower() or "Broken local" in err)
                for err in errors
            )
            if (
                nav_only_error
                and not has_secondary_nav_breaks
                and not page_count_or_root_error
                and not has_invalid_html_error
                and len(valid_html_paths) >= expected_pages
                and existing_secondary
            ):
                repair_scope = "root_nav_only"
            elif (
                all_nav_errors
                and not page_count_or_root_error
                and not has_invalid_html_error
                and len(valid_html_paths) >= expected_pages
                and existing_secondary
            ):
                repair_scope = "nav_repair"
            else:
                repair_scope = "full_rebuild"

        return {
            "ok": not errors,
            "expected_pages": expected_pages,
            "html_files": valid_html_paths,
            "observed_html_files": observed_html_paths,
            "invalid_html_files": invalid_html_files,
            "errors": errors,
            "warnings": warnings,
            "repair_scope": repair_scope,
            "nav_targets": nav_targets,
            "matched_nav_targets": matched_nav_targets,
            "missing_nav_targets": missing_nav_targets,
            "unlinked_secondary_pages": unlinked_secondary_pages,
            "broken_local_nav_entries": broken_local_nav_entries,
            "secondary_missing_route_links": secondary_missing_route_links,
            "secondary_missing_nav_entries": secondary_missing_nav_entries,
        }

    async def _enforce_multi_page_builder_aggregate_gate(
        self,
        plan: Plan,
        results: Dict[str, Any],
        completed: set,
        succeeded: set,
        failed: set,
    ) -> bool:
        if not self._is_multi_page_website_goal(plan.goal):
            return False

        builders = [st for st in plan.subtasks if st.agent_type == "builder"]
        if len(builders) < 2:
            return False
        if any(st.id not in succeeded or st.status != TaskStatus.COMPLETED for st in builders):
            return False

        downstream_agents = {"reviewer", "deployer", "tester", "debugger"}
        downstream_started = any(
            st.agent_type in downstream_agents
            and st.status not in (TaskStatus.PENDING, TaskStatus.CANCELLED)
            for st in plan.subtasks
        )
        if downstream_started:
            return False

        multi_page_gate = self._evaluate_multi_page_artifacts(plan.goal)
        if multi_page_gate.get("ok"):
            return False

        # §P1-FIX: Before re-queuing builders, attempt auto-patch for root_nav_only issues.
        # This is the most common aggregate gate failure: Builder 1's index.html nav
        # doesn't link to Builder 2's pages. A simple nav patch fixes this without
        # wasting ~5min on a full builder re-run.
        repair_scope_pre = str(multi_page_gate.get("repair_scope") or "full_rebuild")
        if repair_scope_pre in ("root_nav_only", "nav_repair"):
            patched = self._auto_patch_root_navigation(multi_page_gate)
            if repair_scope_pre == "nav_repair":
                patched = self._auto_patch_secondary_navigation(multi_page_gate) or patched
            if patched:
                multi_page_gate = self._evaluate_multi_page_artifacts(plan.goal)
                if multi_page_gate.get("ok"):
                    logger.info(
                        "Aggregate gate: auto-patched navigation (%s) — gate now passes, skipping builder re-run",
                        repair_scope_pre,
                    )
                    return False

        gate_errors = multi_page_gate.get("errors", []) or [
            "Combined multi-page output failed the final site gate.",
        ]
        observed_files = multi_page_gate.get("html_files", []) or []
        missing_nav_targets = multi_page_gate.get("missing_nav_targets", []) or []
        unlinked_secondary_pages = multi_page_gate.get("unlinked_secondary_pages", []) or []
        repair_scope = str(multi_page_gate.get("repair_scope") or "full_rebuild")
        logger.warning(
            "Aggregate gate diagnostic: repair_scope=%s valid_files=%s expected=%s errors=%s",
            repair_scope,
            observed_files[:10],
            multi_page_gate.get("expected_pages"),
            gate_errors[:5],
        )
        missing_by_builder = {
            builder.id: self._builder_repair_targets(plan, builder, multi_page_gate)
            for builder in builders
        }
        target_builders = builders
        marker = "⚠️ SHARED MULTI-PAGE GATE FAILED."
        if repair_scope == "root_nav_only":
            root_builder = self._homepage_builder_task(plan)
            target_builders = [root_builder] if root_builder else builders[:1]
            marker = "⚠️ ROOT NAVIGATION PATCH REQUIRED."
        else:
            target_builders = [builder for builder in builders if missing_by_builder.get(builder.id)]
            if not target_builders:
                target_builders = builders
        shared_brief_lines = [
            "Shared multi-page integration failed after both builders finished.",
        ]
        shared_brief_lines.extend(f"- {item}" for item in gate_errors[:4])
        if repair_scope == "root_nav_only":
            preserve_pages = [item for item in observed_files if item != "index.html"]
            if preserve_pages:
                shared_brief_lines.append(
                    "Preserve these existing named pages exactly as-is: "
                    + ", ".join(str(item) for item in preserve_pages[:12])
                )
            if missing_nav_targets:
                shared_brief_lines.append(
                    "Broken or missing navigation targets currently referenced by the homepage: "
                    + ", ".join(str(item) for item in missing_nav_targets[:12])
                )
            if unlinked_secondary_pages:
                shared_brief_lines.append(
                    "Real pages currently missing from homepage/shared navigation: "
                    + ", ".join(str(item) for item in unlinked_secondary_pages[:12])
                )
            shared_brief_lines.append(
                "Repair mode: read the existing files first, then patch index.html and shared navigation only."
            )
            shared_brief_lines.append(
                "Do NOT rewrite secondary pages, do NOT reduce page count, and do NOT invent new slugs when a real page already exists."
            )
        elif observed_files:
            shared_brief_lines.append(
                "Observed HTML files: " + ", ".join(str(item) for item in observed_files[:12])
            )
            shared_brief_lines.append(
                "Do not restart from zero. Preserve the strongest existing pages, restore the correct homepage, "
                "and add or fix the missing linked pages so the final site satisfies the requested page count."
            )
        shared_brief = "\n".join(shared_brief_lines)

        # §P0-FIX: Hard cap on aggregate gate retries to prevent infinite loops.
        # The per-builder retries check alone was insufficient because retries
        # were never incremented in the original code path.
        _MAX_AGGREGATE_GATE_RETRIES = 2
        if all(st.retries < st.max_retries and st.retries < _MAX_AGGREGATE_GATE_RETRIES for st in target_builders):
            for builder_task in target_builders:
                # §P0-FIX: INCREMENT retries BEFORE re-queueing to prevent infinite loop.
                builder_task.retries += 1
                assigned_targets = self._builder_bootstrap_targets(plan, builder_task)
                missing_targets = missing_by_builder.get(builder_task.id, [])
                builder_specific_lines: List[str] = []
                repair_targets = self._builder_repair_targets(plan, builder_task, multi_page_gate)
                if assigned_targets:
                    builder_specific_lines.append(
                        "Your assigned HTML filenames remain: " + ", ".join(assigned_targets[:12])
                    )
                if missing_targets:
                    builder_specific_lines.append(
                        "Missing or invalid pages still owned by you: " + ", ".join(missing_targets[:12])
                    )
                if repair_targets:
                    builder_specific_lines.append(BUILDER_DIRECT_MULTIFILE_MARKER)
                    builder_specific_lines.append(
                        f"{BUILDER_TARGET_OVERRIDE_MARKER} {', '.join(repair_targets[:12])}"
                    )
                    builder_specific_lines.append(
                        "Return only fenced HTML blocks for the override targets. Do not return prose or restart the full site."
                    )
                if assigned_targets:
                    builder_specific_lines.append(
                        "Do NOT write any HTML filename outside your assigned list."
                    )
                builder_brief = shared_brief
                if builder_specific_lines:
                    builder_brief = shared_brief + "\n" + "\n".join(f"- {item}" for item in builder_specific_lines)
                builder_task.status = TaskStatus.PENDING
                builder_task.output = ""
                builder_task.error = builder_brief[:800]
                builder_task.completed_at = 0
                if marker not in builder_task.description:
                    builder_task.description = (
                        f"{builder_task.description}\n\n"
                        f"{marker}\n"
                        f"{builder_brief[:1200]}\n"
                    )
                results.pop(builder_task.id, None)
                completed.discard(builder_task.id)
                succeeded.discard(builder_task.id)
                failed.discard(builder_task.id)
                self._append_ne_activity(
                    builder_task.id,
                    f"共享多页总闸未通过（重试 {builder_task.retries}/{_MAX_AGGREGATE_GATE_RETRIES}），重新构建：{builder_brief[:400]}",
                    entry_type="warn",
                )
                await self.emit("subtask_progress", {
                    "subtask_id": builder_task.id,
                    "stage": "aggregate_multi_page_gate_failed",
                    "message": builder_brief[:500],
                    "requeue": True,
                    "retry": builder_task.retries,
                    "max_retries": min(builder_task.max_retries, _MAX_AGGREGATE_GATE_RETRIES),
                    "missing_targets": missing_targets[:12],
                })
            logger.warning(
                "Aggregate multi-page gate failed (retry %s/%s); re-running builders %s",
                target_builders[0].retries if target_builders else '?',
                _MAX_AGGREGATE_GATE_RETRIES,
                [st.id for st in target_builders],
            )
            return True

        hard_fail_msg = (
            "Final multi-page site gate failed and builder requeue budget is exhausted. "
            + " ".join(gate_errors[:3])
        )[:400]
        for builder_task in target_builders:
            builder_task.status = TaskStatus.FAILED
            builder_task.error = hard_fail_msg
            results[builder_task.id] = {
                "success": False,
                "error": hard_fail_msg,
                "non_retryable": True,
            }
            completed.add(builder_task.id)
            failed.add(builder_task.id)
            succeeded.discard(builder_task.id)
            self._append_ne_activity(
                builder_task.id,
                f"共享多页总闸最终失败：{hard_fail_msg}",
                entry_type="error",
            )
            await self._sync_ne_status(
                builder_task.id,
                "failed",
                output_summary=self._humanize_output_summary(builder_task.agent_type, hard_fail_msg, False),
                error_message=hard_fail_msg,
            )
            await self.emit("subtask_progress", {
                "subtask_id": builder_task.id,
                "stage": "aggregate_multi_page_gate_final_failure",
                "message": hard_fail_msg,
            })
        logger.warning("Aggregate multi-page gate failed terminally: %s", gate_errors[:3])
        return True

    def _build_reviewer_forced_rejection(
        self,
        *,
        interaction_error: str = "",
        preview_gate: Optional[Dict[str, Any]] = None,
        multi_page_gate: Optional[Dict[str, Any]] = None,
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
        largest_blank_gap = int(render_summary.get("largest_blank_gap", 0) or 0)
        blank_gap_count = int(render_summary.get("blank_gap_count", 0) or 0)

        blank_like = (
            body_text_len <= 20
            or any("blank" in str(item).lower() or "near-empty" in str(item).lower() for item in smoke.get("render_errors", []) or [])
        )
        unreadable_like = any("readable visible text" in str(item).lower() for item in smoke.get("render_errors", []) or [])
        mid_gap_like = any("blank vertical gap" in str(item).lower() for item in smoke.get("render_errors", []) or [])

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

        if smoke_status == "fail" and mid_gap_like:
            blank_sections_found = max(blank_sections_found, max(blank_gap_count, 2))
            add_unique(blocking_issues, "The page collapses into a large blank band while scrolling through the middle content.")
            add_unique(missing_deliverables, "Continuous mid-page sections between the hero and footer")
            add_unique(
                required_changes,
                (
                    f"Restore the missing middle sections and remove oversized empty spacers/min-height blocks so scrolling never hits a blank gap"
                    f"{f' of roughly {largest_blank_gap}px' if largest_blank_gap > 0 else ''}."
                ),
            )
            add_unique(
                acceptance_criteria,
                "Scrolling from hero to footer never passes through a large empty band without readable content, media, or meaningful UI.",
            )
            scores["layout"] = min(scores["layout"], 2)
            scores["responsive"] = min(scores["responsive"], 3)
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

        multi_gate = multi_page_gate if isinstance(multi_page_gate, dict) else {}
        if multi_gate and not multi_gate.get("ok", True):
            multi_errors = [str(item) for item in (multi_gate.get("errors") or []) if str(item or "").strip()]
            expected_pages = int(multi_gate.get("expected_pages", 0) or 0)
            observed_pages = [str(item) for item in (multi_gate.get("html_files") or []) if str(item or "").strip()]
            missing_nav_targets = [str(item) for item in (multi_gate.get("missing_nav_targets") or []) if str(item or "").strip()]
            unlinked_secondary_pages = [str(item) for item in (multi_gate.get("unlinked_secondary_pages") or []) if str(item or "").strip()]
            for err in multi_errors[:6]:
                add_unique(issues, err)
                add_unique(blocking_issues, err)
            if expected_pages:
                observed_count = len(observed_pages)
                if observed_count < expected_pages:
                    blank_sections_found = max(blank_sections_found, max(expected_pages - observed_count, 2))
                    add_unique(
                        missing_deliverables,
                        f"All {expected_pages} requested HTML pages with real content",
                    )
                    add_unique(
                        required_changes,
                        f"Restore the missing pages so the delivery contains {expected_pages}/{expected_pages} real routes instead of {observed_count}/{expected_pages}.",
                    )
                    add_unique(
                        acceptance_criteria,
                        f"Reviewer can open {expected_pages} real linked pages from the current build without relying on older snapshots or hidden leftovers.",
                    )
                    scores["functionality"] = min(scores["functionality"], 2)
                    scores["completeness"] = min(scores["completeness"], 1)
                    scores["originality"] = min(scores["originality"], 2)
            if missing_nav_targets or unlinked_secondary_pages:
                add_unique(
                    blocking_issues,
                    "Homepage/shared navigation no longer exposes the full multi-page site correctly.",
                )
                add_unique(
                    required_changes,
                    "Repair homepage/shared navigation so every existing real page is reachable through the intended links.",
                )
                add_unique(
                    acceptance_criteria,
                    "Reviewer can reach every requested page via the built navigation, with no dead links and no missing routes.",
                )
                scores["functionality"] = min(scores["functionality"], 2)
                scores["completeness"] = min(scores["completeness"], 2)
            if observed_pages and len(observed_pages) <= 1 and expected_pages >= 3:
                add_unique(blocking_issues, "The build regressed toward a homepage-only delivery.")
                add_unique(
                    required_changes,
                    "Do not collapse the site back to a homepage-only state; preserve and patch the previously stronger multi-page version.",
                )
                scores["layout"] = min(scores["layout"], 2)
                scores["responsive"] = min(scores["responsive"], 3)
                scores["originality"] = min(scores["originality"], 2)

        visual_regression = gate.get("visual_regression") if isinstance(gate.get("visual_regression"), dict) else {}
        visual_status = str(visual_regression.get("status", "") or "").strip().lower()
        visual_summary = str(visual_regression.get("summary", "") or "").strip()
        if visual_summary and visual_status in {"warn", "fail"}:
            add_unique(issues, visual_summary)
        for issue in (visual_regression.get("issues") or [])[:6]:
            add_unique(issues, issue)
            if visual_status == "fail":
                add_unique(blocking_issues, issue)
        for suggestion in (visual_regression.get("suggestions") or [])[:6]:
            add_unique(required_changes, suggestion)
        if visual_status == "fail":
            add_unique(
                acceptance_criteria,
                "Current screenshots stay close to the last approved baseline in the desktop first viewport, full-page flow, and mobile first viewport unless the brief explicitly changed them.",
            )
            scores["layout"] = min(scores["layout"], 3)
            scores["responsive"] = min(scores["responsive"], 3)
            scores["completeness"] = min(scores["completeness"], 3)
        elif visual_status == "warn":
            scores["layout"] = min(scores["layout"], 5)
            scores["responsive"] = min(scores["responsive"], 5)

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
                "   a. If browser_use is available, use its recorded gameplay session first; otherwise start with browser observe on the visible start/play controls and HUD\n"
                "   b. Click the visible start/play control and confirm the game leaves the title/idle state\n"
                "   c. Use press_sequence (preferred) or multiple press actions with Arrow keys, WASD, Space, Enter, mouse clicks, or the visible control scheme\n"
                "   d. Play for at least 10-15 seconds and cover the primary loop: move, act/attack/jump/use, react to enemies or targets, and verify restart/death/win feedback when available\n"
                "   e. Verify the visible state changed after gameplay input with browser observe or wait_for (camera/HUD/score/health/player position/enemy state/scene state)\n"
                "   f. Take at least one screenshot MID-GAMEPLAY (not just the title screen) and one screenshot of the HUD / overlay / mounted UI\n"
                "   g. Inspect whether characters/models/enemies/props/materials/UI mounts look coherent instead of placeholder-grade\n"
                "   h. REJECT if: game doesn't start, controls don't respond, the visible state never changes, the HUD/UI is detached or broken, visuals are placeholder-grade with no recovery plan, or browser runtime errors appear"
            )
        if task_type == "website":
            return (
                "2. MANDATORY INTERACTION TEST:\n"
                "   a. Call browser observe first to inspect visible navigation/buttons/forms\n"
                "   b. Use browser act to click key navigation links and buttons\n"
                "   b1. For multi-page sites, first verify at least one real internal navigation path through the UI\n"
                "   b2. After navigation is proven, cover the remaining known preview paths efficiently; direct page visits are acceptable when navigation is already validated or clearly broken\n"
                "   b3. After EACH interaction, you must call browser observe or wait_for before moving on\n"
                "   c. Prefer browser record_scroll on the homepage and one representative secondary page; if unavailable, scroll those pages manually until the bottom is confirmed\n"
                "   d. Test forms, inputs, or interactive elements with browser act\n"
                "   e. You MUST use wait_for or browser observe after interaction to verify visible state changed\n"
                "   f. A review is invalid if it lacks post-action verification evidence\n"
                "   g. Check hover effects and animations\n"
                "   h. Take screenshots of different sections\n"
                "3. IMAGE RELEVANCE CHECK (MANDATORY):\n"
                "   a. Verify that ALL visible images semantically match the site's topic\n"
                "   b. REJECT if images show unrelated content (e.g., European landmarks on a China travel site, random stock photos unrelated to the topic)\n"
                "   c. Check for broken images, missing alt text, or placeholder boxes\n"
                "   d. Verify images have CSS gradient fallbacks and are not raw SVG placeholder boxes\n"
                "4. NAVIGATION COMPLETENESS CHECK (MANDATORY):\n"
                "   a. Verify that ALL generated HTML pages are reachable from the homepage navigation\n"
                "   b. Count the number of nav links and compare against the number of generated page files\n"
                "   c. REJECT if any pages exist in the output directory but are NOT linked from the main navigation\n"
                "   d. REJECT if nav links point to non-existent pages (broken links)\n"
                "5. SKILL COMPLIANCE CHECK:\n"
                "   a. If the builder was given GSAP/AOS/animation skills, verify that scroll-triggered animations are actually implemented\n"
                "   b. Check for expected CDN library imports in the HTML source\n"
                "   c. Verify typography hierarchy follows the design system (not default browser fonts)\n"
                "   REJECT if: navigation is broken, buttons don't work, layout is clearly wrong, images are irrelevant, pages are unlinked, or browser runtime errors appear"
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
        multi_page = task_classifier.wants_multi_page(goal)
        page_count = max(task_classifier.requested_page_count(goal), 2) if multi_page else 1
        motion_rich = task_classifier.wants_motion_rich_experience(goal)
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
                "   - REJECT if character/enemy/weapon/environment visuals look mismatched, unmounted, stretched, or obviously stubbed\n"
                "   - REJECT if HUD / crosshair / health / score / inventory / action prompts are missing, floating incorrectly, or detached from the gameplay loop\n"
                "   - REJECT if controls, camera, hit feedback, or input latency make the game feel unresponsive even when it technically runs\n"
                "   - Treat weak playability as a ship blocker even if screenshots look acceptable\n"
            )
        elif profile.task_type == "website":
            task_specific_gate = (
                "8. WEBSITE-SPECIFIC GATE:\n"
                "   - REJECT if hero media slots, collection cards, testimonial avatars, gallery blocks, or map/media placeholders are blank or visually broken\n"
                "   - REJECT if layout rhythm, typography hierarchy, CTA clarity, or motion quality falls below commercial landing-page quality\n"
                "   - REJECT if the site relies on giant placeholder icons, broken aspect ratios, or decorative junk that damages credibility\n"
                "   - REJECT if major pages collapse into flat black/white slabs without layered palette treatment or supporting surfaces\n"
                "   - REJECT if key routes lose a meaningful visual anchor above the fold or replace topic-matched imagery with weaker filler\n"
            )
        elif profile.task_type == "dashboard":
            task_specific_gate = (
                "8. DASHBOARD-SPECIFIC GATE:\n"
                "   - REJECT if key metrics, charts, filters, or tables render as empty shells without usable signal\n"
                "   - REJECT if dense data views are visually attractive but operationally unreadable or missing state handling\n"
            )
        elif profile.task_type == "tool":
            task_specific_gate = (
                "8. TOOL-SPECIFIC GATE:\n"
                "   - REJECT if the core input → transform → output loop is incomplete, confusing, or blocked by broken state transitions\n"
                "   - REJECT if validation, empty states, copy/download actions, or error recovery are missing for the primary workflow\n"
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
        multi_page_step = ""
        if multi_page:
            multi_page_step = (
                "5A. MULTI-PAGE COVERAGE (CRITICAL):\n"
                f"   - This brief requests {page_count} distinct pages/routes.\n"
                "   - You MUST visit every requested page via the real navigation, not by assumption.\n"
                "   - REJECT if any requested page is missing, blank, stub-like, or unreachable from the built navigation.\n"
                "   - Include the visited page paths in `visited_pages`.\n"
            )
        motion_step = ""
        if motion_rich:
            motion_step = (
                "5B. MOTION / TRANSITION GATE (CRITICAL):\n"
                "   - This brief explicitly expects premium motion, not a mostly static site.\n"
                "   - REJECT if the hero/focal object is static when motion was clearly requested.\n"
                "   - REJECT if the site only has trivial hover states but no meaningful motion system.\n"
                + (
                    "   - REJECT if multi-page navigation hard-cuts between routes with no transition treatment or continuity cue.\n"
                    if multi_page else
                    ""
                )
            )
        return (
            f"{prefix} for: {goal[:200]}.\n"
            "Use browser to navigate to http://127.0.0.1:8765/preview/ and perform THOROUGH testing:\n"
            "1. Take screenshots of the initial state (hero/first viewport)\n"
            f"{interaction_instructions}\n"
            "3. MANDATORY SCROLL CHECK: Prefer browser record_scroll so the whole scroll path is captured as evidence.\n"
            "   If record_scroll cannot be used, scroll to the BOTTOM of the page slowly.\n"
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
            f"{multi_page_step}"
            f"{motion_step}"
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
            '"required_changes": ["exact builder/polisher changes with route names when possible"], "acceptance_criteria": ["how re-review will pass"], '
            '"blank_sections_found": <number>, "interactions_tested": ["list what you clicked/tested"], '
            '"visited_pages": ["list each page/path you checked"], '
            '"strengths": ["what is already good enough to preserve"]}}.'
        )

    def _interaction_gate_error(
        self,
        agent_type: str,
        task_type: str,
        browser_actions: List[Dict[str, Any]],
        goal: str = "",
    ) -> Optional[str]:
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
            if action == "record_scroll":
                return "scroll"
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

        def _collect_failed_request_entries() -> List[Dict[str, str]]:
            entries: List[Dict[str, str]] = []
            for item in successful_actions:
                action_url = str(item.get("url") or "").strip()
                for failure in item.get("recent_failed_requests") or []:
                    if not isinstance(failure, dict):
                        continue
                    entries.append({
                        "url": str(failure.get("url") or "").strip(),
                        "error": str(failure.get("error") or "").strip(),
                        "resource_type": str(failure.get("resource_type") or "").strip().lower(),
                        "action_url": action_url,
                    })
            return entries

        def _is_blocking_failed_request(entry: Dict[str, str]) -> bool:
            request_url = str(entry.get("url") or "").strip()
            if not request_url:
                return True
            parsed = urlparse(request_url)
            action_parsed = urlparse(str(entry.get("action_url") or "").strip())
            same_origin = bool(
                parsed.scheme
                and action_parsed.scheme
                and parsed.scheme == action_parsed.scheme
                and parsed.netloc == action_parsed.netloc
            )
            resource_type = str(entry.get("resource_type") or "").strip().lower()
            if resource_type in {"image", "media", "font"}:
                return False
            if resource_type in {"document", "stylesheet", "script", "fetch", "xhr", "websocket"}:
                return True
            path = str(parsed.path or "").strip().lower()
            if path.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif", ".bmp", ".ico", ".mp4", ".webm", ".ogg", ".mp3", ".wav")):
                return False
            if path.endswith((".css", ".js", ".mjs", ".cjs", ".json", ".html", ".htm", ".wasm", ".map")):
                return True
            return same_origin

        failed_request_entries = _collect_failed_request_entries()
        blocking_failed_request_count = (
            sum(1 for entry in failed_request_entries if _is_blocking_failed_request(entry))
            if failed_request_entries
            else max_failed_requests
        )
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
        if blocking_failed_request_count > FAILED_REQUEST_TOLERANCE:
            return (
                f"{agent_label} interaction gate failed: browser session reported "
                f"{blocking_failed_request_count} blocking failed network request(s) "
                f"(tolerance={FAILED_REQUEST_TOLERANCE})."
            )

        interactive_actions = {"click", "fill", "press", "press_sequence"}
        verification_actions = {"snapshot", "wait_for"}

        def _is_verification_action(item: Dict[str, Any]) -> bool:
            raw_action = str(item.get("action") or "").strip().lower()
            action = _normalized_action(item)
            return action in verification_actions or raw_action == "record_scroll"

        scroll_actions = [
            item for item in successful_actions
            if _normalized_action(item) == "scroll"
        ]
        scroll_boundary_known = any(
            bool(item.get("at_bottom"))
            or bool(item.get("at_top"))
            or item.get("is_scrollable") is not None
            for item in scroll_actions
        )
        reached_scroll_boundary = any(
            bool(item.get("at_bottom"))
            or bool(item.get("at_top"))
            or item.get("is_scrollable") is False
            for item in scroll_actions
        )
        scrollable_page_detected = any(
            item.get("is_scrollable") is True
            or int(item.get("page_height", 0) or 0) > int(item.get("viewport_height", 0) or 0) + 4
            for item in successful_actions
        )
        bottom_verified = any(
            _is_verification_action(item)
            and (
                bool(item.get("at_page_bottom"))
                or bool(item.get("at_bottom"))
                or (
                    int(item.get("page_height", 0) or 0) > int(item.get("viewport_height", 0) or 0) + 4
                    and int(item.get("scroll_y", 0) or 0) >= max(
                        int(item.get("page_height", 0) or 0) - int(item.get("viewport_height", 0) or 0) - 24,
                        0,
                    )
                )
            )
            for item in successful_actions
        )

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
            if seen_interaction and _is_verification_action(item):
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
                return (
                    f"{agent_label} interaction gate failed: dashboard tasks must verify the UI after interaction via "
                    "browser snapshot/observe, wait_for, or record_scroll."
                )
            if not has_post_interaction_state_change:
                return f"{agent_label} interaction gate failed: dashboard tasks must prove interaction changed the visible state."
            return None

        if task_type == "website":
            wants_multi_page = task_classifier.wants_multi_page(goal)
            artifact_pages: List[str] = []
            if wants_multi_page:
                artifact_pages = [
                    self._normalize_preview_path(str(path.relative_to(OUTPUT_DIR)))
                    for path in self._current_run_html_artifacts()
                ]
                artifact_pages = [item for item in artifact_pages if item]
            requested_pages = max(task_classifier.requested_page_count(goal), 2) if wants_multi_page else 1
            expected_pages = (
                max(requested_pages, len(artifact_pages))
                if wants_multi_page
                else 1
            )
            visited_pages = {
                self._normalize_preview_path(item.get("url", ""))
                for item in successful_actions
                if self._normalize_preview_path(item.get("url", ""))
            }
            if agent_type == "reviewer":
                if "snapshot" not in successful:
                    return f"{agent_label} interaction gate failed: website reviews must inspect the page via browser snapshot."
                if "scroll" not in successful:
                    return f"{agent_label} interaction gate failed: website reviews must scroll the page."
                if scroll_actions and scroll_boundary_known and not reached_scroll_boundary:
                    return (
                        f"{agent_label} interaction gate failed: website reviews must keep scrolling until the bottom "
                        "of the page or prove the page is non-scrollable."
                    )
                if scroll_actions and scrollable_page_detected and reached_scroll_boundary and not bottom_verified:
                    return (
                        f"{agent_label} interaction gate failed: website reviews must take a bottom-of-page "
                        "snapshot/observation after scrolling so below-the-fold content is actually inspected."
                    )
                if "click" not in successful and "fill" not in successful:
                    return f"{agent_label} interaction gate failed: website reviews must click or fill at least one interactive element."
                if not has_post_interaction_verification:
                    return (
                        f"{agent_label} interaction gate failed: website reviews must verify the post-click state via "
                        "browser snapshot/observe, wait_for, or record_scroll."
                    )
                if not has_post_interaction_state_change:
                    return f"{agent_label} interaction gate failed: website reviews must prove at least one interaction changed the visible state."
            elif agent_type == "tester":
                if "snapshot" not in successful:
                    return f"{agent_label} interaction gate failed: website tests must inspect the page via browser snapshot."
                if "scroll" not in successful:
                    return f"{agent_label} interaction gate failed: website tests must scroll the page."
                if scroll_actions and scroll_boundary_known and not reached_scroll_boundary:
                    return (
                        f"{agent_label} interaction gate failed: website tests must keep scrolling until the bottom "
                        "of the page or prove the page is non-scrollable."
                    )
                if scroll_actions and scrollable_page_detected and reached_scroll_boundary and not bottom_verified:
                    return (
                        f"{agent_label} interaction gate failed: website tests must take a bottom-of-page "
                        "snapshot/observation after scrolling so below-the-fold content is actually inspected."
                    )
                if "click" not in successful and "fill" not in successful:
                    return f"{agent_label} interaction gate failed: website tests must click or fill at least one interactive element."
                if not has_post_interaction_verification:
                    return (
                        f"{agent_label} interaction gate failed: website tests must verify the post-click state via "
                        "browser snapshot/observe, wait_for, or record_scroll."
                    )
                if not has_post_interaction_state_change:
                    return f"{agent_label} interaction gate failed: website tests must prove at least one interaction changed the visible state."
            if wants_multi_page and len(visited_pages) < expected_pages:
                target_pages: List[str] = []
                for page in ["index.html", *artifact_pages, *self._multi_page_fallback_html_names(goal)]:
                    normalized_page = self._normalize_preview_path(page)
                    if normalized_page and normalized_page not in target_pages:
                        target_pages.append(normalized_page)
                    if len(target_pages) >= expected_pages:
                        break
                missing_pages = [
                    page for page in target_pages
                    if page and page not in visited_pages
                ]
                missing_suffix = (
                    " Missing pages: " + ", ".join(missing_pages[:8]) + "."
                    if missing_pages else
                    ""
                )
                return (
                    f"{agent_label} interaction gate failed: multi-page website tasks must visit every requested page. "
                    f"Distinct pages visited: {len(visited_pages)}/{expected_pages}."
                    f"{missing_suffix}"
                )
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

    def _heartbeat_partial_output(
        self,
        agent_type: str,
        elapsed: int,
        loaded_skills: List[str] = None,
        task_desc: str = "",
        streaming_text: str = "",
        has_file_write: bool = False,
    ) -> str:
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
            if not has_file_write:
                if elapsed < 20:
                    return f"正在规划页面结构并准备首次写入真实HTML文件{skill_text}{task_hint} ({elapsed}s)"
                if elapsed < 60:
                    return f"仍在生成首批页面，尚未检测到真实HTML文件落盘{skill_text}{task_hint} ({elapsed}s)"
                if elapsed < 120:
                    return f"构建速度偏慢，仍未检测到真实HTML文件落盘{skill_text}{task_hint} ({elapsed}s)"
                return f"构建耗时过长，仍未检测到真实HTML文件落盘{skill_text}{task_hint} ({elapsed}s)"
            if streaming_hint:
                return f"{streaming_hint}{skill_text} ({elapsed}s)"
            if elapsed < 20:
                return f"已开始写入页面文件，正在构建页面结构和核心组件{skill_text}{task_hint} ({elapsed}s)"
            if elapsed < 60:
                return f"正在编写样式和交互逻辑{skill_text}{task_hint} ({elapsed}s)"
            if elapsed < 120:
                return f"正在完善细节，添加动画效果{skill_text}{task_hint} ({elapsed}s)"
            if elapsed < 240:
                return f"代码量较大，仍在生成中{skill_text}{task_hint} ({elapsed}s)"
            return f"构建时间较长，接近完成{skill_text}{task_hint} ({elapsed}s)"
        if agent_type == "polisher":
            if elapsed < 20:
                return f"正在读取已有页面并整理高级化改造清单{skill_text}{task_hint} ({elapsed}s)"
            if elapsed < 60:
                return f"正在强化排版、留白、层次和动效连续性{streaming_detail}{skill_text} ({elapsed}s)"
            if elapsed < 120:
                return f"正在补强页面转场、滚动节奏和高级质感{streaming_detail}{skill_text} ({elapsed}s)"
            return f"正在做最后一轮成品级抛光{streaming_detail}{skill_text} ({elapsed}s)"
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

        if agent_type == "polisher":
            lower = raw_output.lower() if raw_output else ""
            improvements = []
            if 'animation' in lower or '动画' in lower or 'transition' in lower:
                improvements.append("动效")
            if 'typography' in lower or '字体' in lower:
                improvements.append("排版")
            if 'spacing' in lower or '留白' in lower:
                improvements.append("层次")
            if 'scroll' in lower or '滚动' in lower:
                improvements.append("滚动体验")
            detail = f"（强化了{'、'.join(improvements[:4])}）" if improvements else ""
            return f"精修完成，已对现有页面做成品级抛光{detail}。"

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
        persist_error_message: bool = False,
    ) -> None:
        """Update canonical node details for a failed attempt without forcing terminal status."""
        ne_id = self._ne_id_for_subtask(subtask_id)
        if not ne_id:
            return
        try:
            nes = get_node_execution_store()
            update_data: Dict[str, Any] = {
                "output_summary": str(output_summary or "")[:2000],
                "phase": "attempt_failed",
            }
            if persist_error_message:
                update_data["error_message"] = str(error_message or "")[:500]
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
        repaired = repair_html_structure(normalized)
        if repaired != normalized:
            normalized = repaired
            logger.info(f"Auto-closed truncated HTML ({len(normalized)} chars)")
        return normalized

    def _parse_code_block_header(self, header: str) -> tuple[str, Optional[Path]]:
        raw = str(header or "").strip()
        if not raw:
            return "txt", None

        tokens = [token.strip().strip(",") for token in raw.split() if token.strip()]
        lang = ""
        filename = ""
        if tokens and re.fullmatch(r"[\w+-]+", tokens[0]):
            lang = tokens.pop(0).lower()

        for token in tokens:
            cleaned = token.strip().strip("\"'`")
            lower = cleaned.lower()
            if lower.startswith(("file=", "filename=", "path=")):
                filename = cleaned.split("=", 1)[1].strip().strip("\"'`")
                continue
            if lower.startswith(("file:", "filename:", "path:")):
                filename = cleaned.split(":", 1)[1].strip().strip("\"'`")
                continue
            if "." in cleaned:
                filename = cleaned

        def _sanitize_rel_path(candidate: Path) -> Optional[Path]:
            parts = [str(part).strip() for part in candidate.parts if str(part).strip()]
            if parts and re.fullmatch(r"task_\d+", parts[0], re.IGNORECASE):
                parts = parts[1:]
            if not parts or any(part in {"", ".", ".."} for part in parts):
                return None
            return Path(*parts)

        rel_path: Optional[Path] = None
        if filename:
            candidate = Path(filename)
            if candidate.is_absolute():
                try:
                    output_root = OUTPUT_DIR.resolve()
                    resolved_candidate = candidate.resolve()
                    if resolved_candidate == output_root or output_root in resolved_candidate.parents:
                        rel_candidate = resolved_candidate.relative_to(output_root)
                        rel_path = _sanitize_rel_path(rel_candidate)
                except Exception:
                    rel_path = None
            elif candidate.parts:
                rel_path = _sanitize_rel_path(candidate)

        if not lang and rel_path is not None:
            lang = rel_path.suffix.lower().lstrip(".")
        return (lang or "txt"), rel_path

    def _save_extracted_code_block(
        self,
        *,
        task_dir: Path,
        rel_path: Path,
        code: str,
        files: List[str],
        allow_root_index_copy: bool,
        allowed_html_targets: Optional[set[str]] = None,
        is_retry: bool = False,
        merge_owner: Optional[str] = None,
    ) -> None:
        if rel_path.suffix.lower() in (".html", ".htm"):
            if allowed_html_targets and rel_path.name not in allowed_html_targets:
                logger.info(f"Skipping extracted HTML block outside assigned targets: {rel_path.name}")
                return
            if rel_path.name == "index.html" and not allow_root_index_copy:
                logger.info("Skipping extracted secondary builder index.html block")
                return
        task_type = getattr(self, "_current_task_type", "website")
        code = postprocess_generated_text(code, filename=rel_path.name, task_type=task_type)
        target = OUTPUT_DIR / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)

        def _shared_asset_markers(owner: str) -> tuple[str, str]:
            safe_owner = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(owner or "").strip()) or "builder"
            if rel_path.suffix.lower() == ".css":
                return (
                    f"/* ── Builder Asset Start: {safe_owner} ── */",
                    f"/* ── Builder Asset End: {safe_owner} ── */",
                )
            return (
                f"// ── Builder Asset Start: {safe_owner} ──",
                f"// ── Builder Asset End: {safe_owner} ──",
            )

        def _render_shared_asset_section(owner: str, payload: str) -> str:
            start, end = _shared_asset_markers(owner)
            return f"{start}\n{payload.strip()}\n{end}"

        def _shared_asset_section_pattern(owner: Optional[str] = None) -> re.Pattern[str]:
            if rel_path.suffix.lower() == ".css":
                if owner:
                    start, end = _shared_asset_markers(owner)
                    return re.compile(
                        re.escape(start) + r"\s*(?P<payload>.*?)\s*" + re.escape(end),
                        re.DOTALL,
                    )
                return re.compile(
                    r"/\*\s*── Builder Asset Start: [^*]+── \*/\s*(?P<payload>.*?)\s*/\*\s*── Builder Asset End: [^*]+── \*/",
                    re.DOTALL,
                )
            if owner:
                start, end = _shared_asset_markers(owner)
                return re.compile(
                    re.escape(start) + r"\s*(?P<payload>.*?)\s*" + re.escape(end),
                    re.DOTALL,
                )
            return re.compile(
                r"//\s*── Builder Asset Start: .+?──\s*(?P<payload>.*?)\s*//\s*── Builder Asset End: .+?──",
                re.DOTALL,
            )

        def _extract_owner_scoped_payload(payload: str, owner: str) -> Optional[str]:
            match = _shared_asset_section_pattern(owner).search(str(payload or ""))
            extracted = str(match.group("payload") or "").strip() if match else ""
            return extracted or None

        def _strip_shared_asset_wrappers(payload: str) -> str:
            text = str(payload or "")
            text = _shared_asset_section_pattern().sub(
                lambda match: str(match.group("payload") or "").strip() + "\n",
                text,
            )
            text = re.sub(
                r"(?:/\*\s*── Builder Merge: Additional Styles ── \*/|//\s*── Builder Merge: Additional Scripts ──)\s*",
                "",
                text,
                flags=re.IGNORECASE,
            )
            return text.strip()

        def _upsert_shared_asset_section(existing: str, owner: str, payload: str) -> tuple[str, str]:
            marker_token = "Builder Asset Start:"
            start, end = _shared_asset_markers(owner)
            replacement = _render_shared_asset_section(owner, payload)
            normalized_existing = str(existing or "").strip()
            normalized_payload = str(payload or "").strip()

            if not normalized_existing:
                return replacement, "created"

            base_text = existing
            if marker_token not in base_text:
                if normalized_existing == normalized_payload:
                    return base_text, "duplicate"
                base_text = _render_shared_asset_section("legacy-base", existing)

            section_pattern = re.compile(
                re.escape(start) + r".*?" + re.escape(end),
                re.DOTALL,
            )
            updated, replaced = section_pattern.subn(replacement, base_text, count=1)
            if replaced:
                if updated == base_text:
                    return base_text, "duplicate"
                return updated, "replaced"

            if normalized_payload and normalized_payload in base_text:
                return base_text, "duplicate"

            return base_text.rstrip() + "\n\n" + replacement, "appended"

        shared_asset_names = {"styles.css", "app.js", "style.css", "main.css", "main.js", "script.js"}
        if rel_path.name.lower() in shared_asset_names and merge_owner:
            try:
                existing = target.read_text(encoding="utf-8", errors="ignore") if target.exists() else ""
                owner_payload = code
                if is_retry:
                    extracted_payload = _extract_owner_scoped_payload(code, merge_owner)
                    if extracted_payload:
                        owner_payload = extracted_payload
                    else:
                        owner_payload = _strip_shared_asset_wrappers(code) or code
                merged, merge_state = _upsert_shared_asset_section(existing, merge_owner, owner_payload)
                if target.exists() and merged == existing:
                    logger.info("Skipping duplicate shared asset section for %s (%s)", target.name, merge_owner)
                    files.append(str(target))
                    return
                target.write_text(merged, encoding="utf-8")
                logger.info(
                    "%s shared asset %s for %s (%d chars)",
                    merge_state.capitalize(),
                    target.name,
                    merge_owner,
                    len(merged),
                )
                files.append(str(target))
                return
            except Exception as merge_err:
                logger.warning(
                    "Owner-scoped shared asset merge failed for %s (%s), falling back: %s",
                    target.name,
                    merge_owner,
                    merge_err,
                )

        if (
            rel_path.name.lower() in shared_asset_names
            and target.exists()
            and target.stat().st_size > 100
            and not is_retry
        ):
            try:
                existing = target.read_text(encoding="utf-8", errors="ignore")
                normalized_existing = existing.strip()
                normalized_new = code.strip()
                if normalized_new and normalized_new in existing:
                    logger.info("Skipping duplicate shared asset append for %s", target.name)
                    files.append(str(target))
                    return
                # Cap: do not merge more than 3 sections to prevent runaway growth
                merge_marker = "Builder Merge:"
                if existing.count(merge_marker) >= 3:
                    logger.info(
                        "Shared asset %s already has %d merge sections; overwriting to prevent runaway growth",
                        target.name, existing.count(merge_marker),
                    )
                elif normalized_new != normalized_existing and len(normalized_new) > 50:
                    # For CSS: concatenate with a separator comment
                    # For JS: concatenate with a safety separator
                    if rel_path.suffix.lower() == ".css":
                        merged = existing.rstrip() + "\n\n/* ── Builder Merge: Additional Styles ── */\n\n" + code.lstrip()
                    else:
                        merged = existing.rstrip() + "\n\n// ── Builder Merge: Additional Scripts ──\n\n" + code.lstrip()
                    target.write_text(merged, encoding="utf-8")
                    logger.info(
                        "Merged shared asset %s: existing=%d + new=%d = %d chars",
                        target.name, len(existing), len(code), len(merged),
                    )
                    files.append(str(target))
                    return
            except Exception as merge_err:
                logger.warning("Shared asset merge failed for %s, falling back to overwrite: %s", target.name, merge_err)
        target.write_text(code, encoding="utf-8")
        files.append(str(target))
        logger.info(f"Saved code to {target} ({len(code)} chars)")

    def _remap_skipped_html_blocks(
        self,
        *,
        task_dir: Path,
        files: List[str],
        skipped_html_blocks: List[tuple[str, str]],
        allowed_html_targets_ordered: List[str],
        allow_root_index_copy: bool,
    ) -> None:
        if not skipped_html_blocks or not allowed_html_targets_ordered:
            return
        saved_html_names = {
            Path(item).name
            for item in files
            if Path(item).suffix.lower() in (".html", ".htm")
        }
        outstanding_targets = [
            name for name in allowed_html_targets_ordered
            if name not in saved_html_names
        ]
        if not outstanding_targets or len(skipped_html_blocks) != len(outstanding_targets):
            return

        remapped_any = False
        for (original_name, code), target_name in zip(skipped_html_blocks, outstanding_targets):
            if original_name == "index.html" and target_name != "index.html":
                logger.info(
                    "Skipping unsafe remap from skipped index.html block to assigned target %s",
                    target_name,
                )
                continue
            self._save_extracted_code_block(
                task_dir=task_dir,
                rel_path=Path(target_name),
                code=code,
                files=files,
                allow_root_index_copy=allow_root_index_copy,
                allowed_html_targets=set(allowed_html_targets_ordered),
            )
            logger.info(
                "Remapped skipped HTML block %s -> assigned target %s",
                original_name,
                target_name,
            )
            remapped_any = True
        if remapped_any:
            logger.info("Recovered assigned HTML pages by remapping skipped named blocks")

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

    def _parallel_builder_task_descriptions(self, goal: str) -> tuple[str, str]:
        """Return builder descriptions for parallel ownership without conflicting delivery rules."""
        primary_desc = self._builder_task_description(goal)
        builder_note = self._builder_asset_pipeline_note(goal)
        if builder_note:
            primary_desc = f"{primary_desc} {builder_note}"

        if not (
            task_classifier.classify(goal).task_type == "website"
            and task_classifier.wants_multi_page(goal)
        ):
            return primary_desc, primary_desc

        secondary_desc = primary_desc
        secondary_delivery = (
            "Do NOT write /tmp/evermind_output/index.html. "
            "Create only your assigned secondary linked HTML page(s) via file_ops write. "
        )
        secondary_desc = re.sub(
            r"Create index\.html plus at least \d+ additional linked HTML page\(s\) via file_ops write\.\s*",
            secondary_delivery,
            secondary_desc,
            count=1,
        )
        secondary_desc = re.sub(
            r"Create index\.html plus the additional linked HTML pages required by the brief via file_ops write\.\s*",
            secondary_delivery,
            secondary_desc,
            count=1,
        )
        if secondary_desc == primary_desc:
            secondary_desc = f"{secondary_delivery}{secondary_desc}"

        primary_desc = (
            f"{primary_desc} "
            "You own /tmp/evermind_output/index.html for this parallel build and must preserve shared navigation quality."
        )
        secondary_desc = (
            f"{secondary_desc} "
            "Treat /tmp/evermind_output/index.html as Builder 1-owned unless the orchestrator explicitly reassigns it."
        )
        return primary_desc, secondary_desc

    def _multi_page_fallback_html_names(self, goal: str = "") -> List[str]:
        semantic = task_classifier.suggested_multi_page_route_filenames(goal)
        defaults = [
            "pricing.html",
            "features.html",
            "solutions.html",
            "platform.html",
            "contact.html",
            "about.html",
            "faq.html",
            "security.html",
            "docs.html",
            "case-studies.html",
            "careers.html",
            "blog.html",
        ]
        merged: List[str] = []
        for name in [*semantic, *defaults]:
            clean = str(name or "").strip().lower()
            if not clean or clean == "index.html":
                continue
            if clean not in merged:
                merged.append(clean)
        return merged

    def _single_builder_handoff_focus(self, goal: str) -> str:
        profile = task_classifier.classify(goal)
        if profile.task_type == "website" and task_classifier.wants_multi_page(goal):
            expected_pages = max(task_classifier.requested_page_count(goal), 2)
            fallback_names = self._multi_page_fallback_html_names(goal)[: max(expected_pages - 1, 0)]
            fallback_line = (
                f" Otherwise use this stable fallback set under /tmp/evermind_output/: {', '.join(fallback_names)}."
                if fallback_names else
                ""
            )
            motion_line = (
                " Preserve route-to-route continuity so the site never feels like hard cuts."
                if task_classifier.wants_motion_rich_experience(goal)
                else ""
            )
            return (
                "YOUR JOB: This is a MULTI-PAGE website request and you own the ENTIRE routed experience. "
                f"You MUST create /tmp/evermind_output/index.html and at least {max(expected_pages - 1, 1)} additional linked page(s). "
                "If the brief or analyst handoff names exact pages/routes, follow those filenames. "
                + fallback_line
                + " Keep the filenames consistent across retries. "
                "Build full standalone HTML pages via file_ops write; do NOT create page fragments and do NOT ship only the homepage. "
                "Every page must share one coherent design system, working navigation, real content density, and polished mobile behavior."
                + motion_line
            )
        return (
            "YOUR JOB: Own the full end-to-end deliverable. Ship the complete requested product with the final interactions, "
            "polish, and working navigation/state instead of leaving work for a missing sibling builder."
        )

    def _builder_html_target_restriction_enabled(
        self,
        plan: Optional[Plan],
        subtask: Optional[SubTask] = None,
    ) -> bool:
        if not plan or not self._is_multi_page_website_goal(plan.goal):
            return False
        if subtask is not None and subtask.agent_type != "builder":
            return False
        builders = [st for st in plan.subtasks if st.agent_type == "builder"]
        return len(builders) > 1

    def _builder_target_override_targets(
        self,
        text: str,
        *,
        can_write_root_index: bool,
    ) -> List[str]:
        raw = str(text or "")
        if not raw or BUILDER_TARGET_OVERRIDE_MARKER.lower() not in raw.lower():
            return []
        targets: List[str] = []

        def _add(raw_name: str) -> None:
            name = Path(str(raw_name or "").strip()).name
            if not name or not name.lower().endswith((".html", ".htm")):
                return
            if name.lower().startswith("index_part"):
                return
            if name == "index.html" and not can_write_root_index:
                return
            if name not in targets:
                targets.append(name)

        for block in re.findall(
            r"html target override(?:[^\n:]*)?:\s*([^\n]+)",
            raw,
            re.IGNORECASE,
        ):
            for match in re.findall(r"([A-Za-z0-9][A-Za-z0-9._/-]*\.html?)", block, re.IGNORECASE):
                _add(match)
        return targets

    def _pro_multi_page_builder_focus(self, goal: str) -> tuple[str, str]:
        """Split multi-page website ownership by real pages, not half-page fragments."""
        expected_pages = max(task_classifier.requested_page_count(goal), 2)
        builder1_total = max(1, (expected_pages + 1) // 2)
        builder2_total = max(expected_pages - builder1_total, 1)
        builder1_secondary = max(builder1_total - 1, 0)
        fallback_names = self._multi_page_fallback_html_names(goal)
        builder1_pages = fallback_names[:builder1_secondary]
        builder2_pages = fallback_names[builder1_secondary:builder1_secondary + builder2_total]
        builder1_fallback = ", ".join(builder1_pages) or "none"
        builder2_fallback = ", ".join(builder2_pages) or "contact.html"

        # Build the canonical navigation list that BOTH builders must use identically
        all_pages = ["index.html"] + builder1_pages + builder2_pages
        nav_contract = (
            "\n\nNAVIGATION CONTRACT (MANDATORY — DO NOT DEVIATE):\n"
            f"The COMPLETE site page list is: {', '.join(all_pages)}.\n"
            "Every page you create MUST have a <nav> with links to ALL pages in this list.\n"
            "Do NOT invent, rename, add, or omit any page from this navigation list.\n"
            "Do NOT reference pages that are not in this list (e.g., heritage.html, landscapes.html).\n"
            "Use the EXACT filenames shown above for all href values.\n"
        )

        focus_1 = (
            "YOUR JOB: This is a MULTI-PAGE website request. Own the entry experience plus the first "
            f"{builder1_total} page(s). You MUST create /tmp/evermind_output/index.html and "
            f"{builder1_secondary} additional linked page(s). "
            "If the brief or analyst handoff names exact pages/routes, follow those filenames exactly. "
            f"Otherwise use this non-overlapping fallback set for your secondary pages: {builder1_fallback}. "
            "Build full standalone HTML pages via file_ops write; do NOT create page fragments, do NOT write "
            "/tmp/evermind_output/index_part1.html, and do NOT collapse the site into one long page."
            f"{nav_contract}"
        )
        focus_2 = (
            "YOUR JOB: This is a MULTI-PAGE website request. Own the remaining "
            f"{builder2_total} page(s). "
            "Do NOT build the bottom half of index.html and do NOT write /tmp/evermind_output/index_part2.html. "
            "If the brief or analyst handoff names exact pages/routes, use the remaining filenames not claimed by Builder 1. "
            f"Otherwise use this non-overlapping fallback set: {builder2_fallback}. "
            "Save each deliverable as a real HTML page under /tmp/evermind_output/ (for example contact.html). "
            "Keep the same visual system and real copy density across every page."
            f"{nav_contract}"
        )
        return focus_1, focus_2

    def _builder_bootstrap_targets(self, plan: Plan, subtask: SubTask) -> List[str]:
        targets: List[str] = []
        is_homepage_owner = self._builder_slot_index(plan, subtask.id) == 1
        builders = [st for st in plan.subtasks if st.agent_type == "builder"]
        single_builder = len(builders) <= 1
        multi_page = self._is_multi_page_website_goal(plan.goal)
        expected_pages = max(task_classifier.requested_page_count(plan.goal), 2) if multi_page else 1

        override_targets = self._builder_target_override_targets(
            subtask.description or "",
            can_write_root_index=is_homepage_owner,
        )
        if override_targets:
            return override_targets

        def _add(name: str) -> None:
            clean = Path(str(name or "").strip()).name
            if not clean or not clean.lower().endswith((".html", ".htm")):
                return
            if clean.lower().startswith("index_part"):
                return
            if clean == "index.html" and not is_homepage_owner:
                return
            if clean not in targets:
                targets.append(clean)

        parser = getattr(self.ai_bridge, "_builder_assigned_html_targets", None)
        if callable(parser):
            try:
                for item in parser(subtask.description or ""):
                    _add(item)
            except Exception:
                pass
        if single_builder:
            if not is_homepage_owner:
                return []
            if "index.html" not in targets:
                targets.insert(0, "index.html")
            if not multi_page:
                return ["index.html"]
            fallback_pool = self._multi_page_fallback_html_names(plan.goal)[: max(expected_pages - 1, 0)]
            while len(targets) < expected_pages and fallback_pool:
                _add(fallback_pool.pop(0))
            if len(targets) > expected_pages:
                targets = targets[:expected_pages]
            return targets
        lead_total = max(1, (expected_pages + 1) // 2)
        lead_secondary = max(lead_total - 1, 0)
        target_count = lead_total if is_homepage_owner else max(expected_pages - lead_total, 1)
        fallback_names = self._multi_page_fallback_html_names(plan.goal)

        if is_homepage_owner:
            if "index.html" not in targets:
                targets.insert(0, "index.html")
            fallback_pool = fallback_names[: max(target_count - 1, 0)]
        else:
            fallback_pool = fallback_names[lead_secondary : lead_secondary + target_count]

        while len(targets) < target_count and fallback_pool:
            _add(fallback_pool.pop(0))

        if is_homepage_owner:
            targets = ["index.html"] + [name for name in targets if name != "index.html"]
        else:
            if "index.html" in targets:
                logger.warning(
                    "Pruned illegal secondary builder bootstrap target index.html "
                    f"for subtask {subtask.id}: {targets}"
                )
            targets = [name for name in targets if name != "index.html"]

        if target_count > 0 and len(targets) > target_count:
            dropped = targets[target_count:]
            # §P0-FIX: Dedup log to prevent heartbeat spam. Only log once per
            # unique (subtask_id, dropped_tuple) combination per session.
            _dedup_key = (subtask.id, tuple(dropped))
            if not hasattr(self, '_bootstrap_trim_logged'):
                self._bootstrap_trim_logged: set = set()
            if _dedup_key not in self._bootstrap_trim_logged:
                self._bootstrap_trim_logged.add(_dedup_key)
                logger.debug(
                    f"Trimmed builder bootstrap targets for subtask {subtask.id} to {target_count}; "
                    f"dropped={dropped}"
                )
            targets = targets[:target_count]

        if not targets and is_homepage_owner:
            targets = ["index.html"]
        return targets

    def _builder_allowed_html_targets(self, plan: Optional[Plan], subtask: Optional[SubTask]) -> List[str]:
        if not plan or not subtask or subtask.agent_type != "builder":
            return []
        if not self._is_multi_page_website_goal(plan.goal):
            return []
        return self._builder_bootstrap_targets(plan, subtask)

    def _all_builder_bootstrap_targets(self, plan: Plan) -> List[str]:
        targets: List[str] = []
        for builder in [st for st in plan.subtasks if st.agent_type == "builder"]:
            for name in self._builder_bootstrap_targets(plan, builder):
                if name not in targets:
                    targets.append(name)
        if "index.html" in targets:
            targets = ["index.html"] + [name for name in targets if name != "index.html"]
        return targets

    def _collect_recent_builder_disk_scan_files(
        self,
        plan: Plan,
        subtask: SubTask,
        *,
        scan_cutoff: float,
    ) -> List[str]:
        allowed_names = {
            Path(name).name
            for name in self._builder_allowed_html_targets(plan, subtask)
            if str(name or "").strip()
        }

        found: List[str] = []
        for html_file in OUTPUT_DIR.rglob("*.html"):
            try:
                if self._is_internal_non_deliverable_html(html_file) or self._is_task_preview_fallback_html(html_file):
                    continue
                if allowed_names and html_file.name not in allowed_names:
                    continue
                if html_file.stat().st_mtime >= scan_cutoff:
                    found.append(str(html_file))
            except Exception:
                continue
        return found

    def _bootstrap_multi_page_html(self, target_name: str, nav_targets: List[str]) -> str:
        title = Path(target_name).stem.replace("-", " ").replace("_", " ").title() or "Draft Page"
        nav_items: List[str] = []
        for name in nav_targets or ["index.html"]:
            label = Path(name).stem.replace("-", " ").replace("_", " ").title() or "Home"
            current_attr = ' aria-current="page"' if name == target_name else ""
            nav_items.append(f'          <a href="{name}"{current_attr}>{label}</a>')
        nav_links = "\n".join(nav_items)
        return (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <meta charset=\"UTF-8\">\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
            "  <meta name=\"evermind-bootstrap\" content=\"pending\">\n"
            f"  <title>{title} Draft</title>\n"
            "  <style>\n"
            "    :root { color-scheme: light; --bg:#f6f4ef; --ink:#161616; --muted:#6f6a62; --line:rgba(22,22,22,0.12); }\n"
            "    * { box-sizing: border-box; }\n"
            "    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif; background: var(--bg); color: var(--ink); }\n"
            "    header, main { width: min(1120px, calc(100% - 48px)); margin: 0 auto; }\n"
            "    header { display:flex; justify-content:space-between; gap:16px; padding:24px 0 0; }\n"
            "    nav { display:flex; flex-wrap:wrap; gap:10px; }\n"
            "    nav a { text-decoration:none; color:var(--muted); border:1px solid var(--line); border-radius:999px; padding:8px 12px; }\n"
            "    nav a[aria-current=\"page\"] { color:var(--ink); border-color:rgba(22,22,22,0.28); }\n"
            "    main { padding: 56px 0 72px; }\n"
            "    section { border:1px solid var(--line); border-radius:28px; background:rgba(255,255,255,0.62); padding:28px; margin-bottom:18px; }\n"
            "    .eyebrow { margin:0 0 12px; text-transform:uppercase; letter-spacing:0.16em; font-size:12px; color:var(--muted); }\n"
            "    h1, h2, p { margin:0; }\n"
            "    h1 { font-size:clamp(32px, 5vw, 64px); line-height:1; margin-bottom:14px; }\n"
            "    h2 { font-size:20px; margin-bottom:10px; }\n"
            "    p { max-width:72ch; color:var(--muted); line-height:1.7; }\n"
            "  </style>\n"
            "</head>\n"
            "<body data-evermind-bootstrap=\"pending\">\n"
            "<!-- evermind-bootstrap scaffold -->\n"
            "  <header>\n"
            "    <strong>Evermind Draft Scaffold</strong>\n"
            "    <nav>\n"
            f"{nav_links}\n"
            "    </nav>\n"
            "  </header>\n"
            "  <main>\n"
            "    <section>\n"
            "      <p class=\"eyebrow\">Internal Draft</p>\n"
            f"      <h1>{title}</h1>\n"
            "      <p>This file is an internal continuation-safe scaffold. A builder must overwrite it with the final routed page before reviewer/tester may ship it.</p>\n"
            "    </section>\n"
            "    <section>\n"
            "      <h2>Why this exists</h2>\n"
            "      <p>It prevents a long multi-page build from ending with an empty workspace after a timeout or crash, while staying invisible to preview promotion and quality gates.</p>\n"
            "    </section>\n"
            "  </main>\n"
            "</body>\n"
            "</html>\n"
        )

    def _ensure_builder_bootstrap_scaffold(self, plan: Plan, subtask: SubTask) -> List[str]:
        if subtask.agent_type != "builder" or not self._is_multi_page_website_goal(plan.goal):
            return []
        targets = self._builder_bootstrap_targets(plan, subtask)
        if not targets:
            return []
        nav_targets = self._all_builder_bootstrap_targets(plan) or targets
        written: List[str] = []
        for name in targets:
            path = OUTPUT_DIR / name
            if path.exists() and path.is_file():
                if is_bootstrap_html_artifact(path):
                    pass
                else:
                    try:
                        current_html = path.read_text(encoding="utf-8", errors="ignore")
                        integrity = inspect_html_integrity(current_html)
                        language_mismatch = self._goal_language_mismatch_reason(plan.goal, current_html)
                        if language_mismatch:
                            logger.warning(
                                "Re-seeding goal-mismatched builder page %s for subtask %s: %s",
                                path,
                                subtask.id,
                                language_mismatch,
                            )
                            raise ValueError(language_mismatch)
                        if integrity.get("ok", True):
                            continue
                        quality_report = self._html_quality_report(current_html, source=str(path))
                        preserves_visual_progress = (
                            not has_truncation_marker(current_html)
                            and "<!-- evermind-bootstrap scaffold -->" not in current_html.lower()
                            and int(quality_report.get("score", 0) or 0) >= 60
                        )
                        if preserves_visual_progress:
                            logger.info(
                                "Preserving in-progress builder page %s for subtask %s despite minor HTML issues",
                                path,
                                subtask.id,
                            )
                            continue
                        logger.warning(
                            "Re-seeding corrupted builder scaffold target %s for subtask %s",
                            path,
                            subtask.id,
                        )
                    except Exception:
                        pass
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self._bootstrap_multi_page_html(name, nav_targets), encoding="utf-8")
                written.append(str(path))
            except Exception as e:
                logger.warning(f"Failed to seed bootstrap scaffold for {name}: {e}")
        if written:
            logger.info(f"Seeded multi-page builder scaffold for subtask {subtask.id}: {written}")
            self._append_ne_activity(
                subtask.id,
                f"已预写内部骨架文件：{', '.join(Path(item).name for item in written[:8])}",
                entry_type="sys",
            )
        return written

    def _pro_builder_focus(self, goal: str) -> tuple[str, str]:
        """Return parallel builder focus for pro mode. Each builder creates an independent part.
        Website parts save separately; Evermind assembles index.html preview automatically."""
        task_type = task_classifier.classify(goal).task_type
        if task_type == "website" and task_classifier.wants_multi_page(goal):
            return self._pro_multi_page_builder_focus(goal)
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
                "YOUR JOB: Refine the existing game in place — preserve the strongest gameplay shell, "
                "then add HUD density, score/ammo state, game-over/restart, sound/visual feedback "
                "(particles, hit flash, shake), mobile touch support, and richer level/encounter clarity. "
                "Do NOT restart from scratch. Do NOT replace good sections with empty wrappers. "
                "Read the existing /tmp/evermind_output/index.html mentally from the injected refinement context, "
                "rewrite the FULL improved index.html, and write it back.",
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
        return task_classifier.wants_generated_assets(goal)

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
        asset_mode = task_classifier.game_asset_pipeline_mode(goal)
        if asset_mode == "3d":
            return (
                self._custom_node_task_desc("imagegen", "Image Gen", goal)
                + " Save concept sheets, orthographic prompts, material/texture directions, and style-lock artifacts under /tmp/evermind_output/assets/.",
                self._custom_node_task_desc("spritesheet", "Spritesheet", goal)
                + " For this 3D-oriented goal, output model_targets, rig_or_animation_clips, material_rules, LOD/export guidance, and builder_replacement_rules instead of a literal 2D sprite atlas.",
                self._custom_node_task_desc("assetimport", "Asset Import", goal)
                + " Normalize model/texture/animation manifests, runtime lookup keys, fallback placeholders, and replacement rules for the builders.",
            )
        return (
            self._custom_node_task_desc("imagegen", "Image Gen", goal)
            + " Save generated assets or prompt-pack artifacts under /tmp/evermind_output/assets/.",
            self._custom_node_task_desc("spritesheet", "Spritesheet", goal)
            + " Produce a concrete frame map / sprite manifest that downstream builders can wire in immediately.",
            self._custom_node_task_desc("assetimport", "Asset Import", goal)
            + " Normalize filenames, manifest keys, runtime lookup paths, and replacement rules for the builders.",
        )

    def _deep_mode_profile(self, goal: str) -> Dict[str, Any]:
        return pro_template_profile(goal)

    def _deep_mode_builder_intro(self, strategy: Dict[str, Any]) -> str:
        if strategy.get("include_asset_pipeline"):
            return "ADVANCED MODE — Use analyst notes and asset manifest.\n"
        if strategy.get("include_uidesign") and strategy.get("include_scribe"):
            if strategy.get("parallel_builders", True) and not strategy.get("scribe_blocks_builders", True):
                return (
                    "ADVANCED MODE — Start from analyst research and the UI design brief immediately. "
                    "Fold the content architecture handoff into the build as soon as it arrives, but do not wait on it before the first page batch.\n"
                )
            if strategy.get("parallel_builders", True):
                return "ADVANCED MODE — Use analyst research, UI design brief, and content architecture handoff to build.\n"
            return (
                "ADVANCED MODE — Start from analyst research and the UI design brief immediately. "
                "Use the content architecture handoff as a background refinement input instead of waiting on it before the first build pass.\n"
            )
        if strategy.get("include_uidesign"):
            return "ADVANCED MODE — Use analyst research and UI design brief to build.\n"
        return "ADVANCED MODE — Use analyst notes and build.\n"

    def _build_pro_plan_subtasks(self, goal: str, analyst_desc: Optional[str] = None) -> List[SubTask]:
        strategy = self._deep_mode_profile(goal)
        profile = task_classifier.classify(goal)
        is_website = profile.task_type == "website"
        parallel_builders = bool(strategy.get("parallel_builders", True))
        scribe_blocks_builders = bool(strategy.get("scribe_blocks_builders", True))
        include_polisher = bool(strategy.get("include_polisher"))
        focus_1, focus_2 = self._pro_builder_focus(goal)
        builder_desc_base_primary, builder_desc_base_secondary = self._parallel_builder_task_descriptions(goal)
        builder_intro = self._deep_mode_builder_intro(strategy)
        deployer_desc = "List generated files and provide local preview URL http://127.0.0.1:8765/preview/"
        reviewer_desc = self._reviewer_task_description(goal, pro=True)
        tester_desc = profile.tester_hint
        analyst_desc = analyst_desc or task_classifier.analyst_description(goal)
        polisher_desc = self._custom_node_task_desc("polisher", "Polisher", goal)
        extra_quality = (
            f"Extra pro requirements for {profile.task_type}: advanced polish, "
            "smooth transitions, and attention to detail."
        )

        subtasks: List[SubTask] = [
            SubTask(id="1", agent_type="analyst", description=analyst_desc, depends_on=[]),
        ]
        builder_dep_ids = ["1"]
        post_builder_quality_dep_ids: List[str] = []
        next_id = 2

        if strategy.get("include_asset_pipeline"):
            imagegen_desc, spritesheet_desc, assetimport_desc = self._asset_pipeline_descriptions(goal)
            subtasks.extend([
                SubTask(id="2", agent_type="imagegen", description=imagegen_desc, depends_on=["1"]),
                SubTask(id="3", agent_type="spritesheet", description=spritesheet_desc, depends_on=["1", "2"]),
                SubTask(id="4", agent_type="assetimport", description=assetimport_desc, depends_on=["1", "2", "3"]),
            ])
            builder_dep_ids = ["1", "4"]
            next_id = 5
        else:
            if strategy.get("include_uidesign"):
                uidesign_id = str(next_id)
                subtasks.append(
                    SubTask(
                        id=uidesign_id,
                        agent_type="uidesign",
                        description=self._custom_node_task_desc("uidesign", "UI Design", goal),
                        depends_on=["1"],
                    )
                )
                builder_dep_ids.append(uidesign_id)
                next_id += 1
            if strategy.get("include_scribe"):
                scribe_id = str(next_id)
                subtasks.append(
                    SubTask(
                        id=scribe_id,
                        agent_type="scribe",
                        description=self._custom_node_task_desc("scribe", "Scribe", goal),
                        depends_on=["1"],
                    )
                )
                if scribe_blocks_builders or not is_website:
                    builder_dep_ids.append(scribe_id)
                else:
                    post_builder_quality_dep_ids.append(scribe_id)
                next_id += 1

        if not parallel_builders:
            builder_id = str(next_id)
            polisher_id = str(next_id + 1) if include_polisher else ""
            reviewer_id = str(next_id + (2 if include_polisher else 1))
            deployer_id = str(next_id + (3 if include_polisher else 2))
            tester_id = str(next_id + (4 if include_polisher else 3))
            debugger_id = str(next_id + (5 if include_polisher else 4))
            builder_desc = (
                f"{builder_intro}"
                f"{self._builder_task_description(goal)}\n"
                f"{self._single_builder_handoff_focus(goal)}\n"
                f"{extra_quality}"
            )
            subtasks.append(
                SubTask(
                    id=builder_id,
                    agent_type="builder",
                    description=builder_desc,
                    depends_on=list(builder_dep_ids),
                )
            )
            polish_depends = [builder_id]
            if include_polisher:
                subtasks.append(
                    SubTask(
                        id=polisher_id,
                        agent_type="polisher",
                        description=f"{polisher_desc}\n{extra_quality}",
                        depends_on=list(dict.fromkeys([builder_id] + post_builder_quality_dep_ids)),
                    )
                )
                polish_depends = [polisher_id]
            subtasks.extend([
                SubTask(
                    id=reviewer_id,
                    agent_type="reviewer",
                    description=reviewer_desc,
                    depends_on=polish_depends,
                ),
                SubTask(
                    id=deployer_id,
                    agent_type="deployer",
                    description=deployer_desc,
                    depends_on=polish_depends,
                ),
                SubTask(
                    id=tester_id,
                    agent_type="tester",
                    description=tester_desc,
                    depends_on=[reviewer_id, deployer_id],
                ),
                SubTask(
                    id=debugger_id,
                    agent_type="debugger",
                    description=(
                        "Fix any issues found by reviewer/tester. "
                        "Use file_ops list to discover ALL HTML pages in /tmp/evermind_output/, "
                        "then file_ops read EACH page and fix every issue found. "
                        "Save corrected versions via file_ops write. "
                        "Check EVERY page, not just index.html — secondary pages are equally important. "
                        "If no issues were found, confirm everything is good."
                    ),
                    depends_on=[tester_id],
                ),
            ])
            return subtasks

        builder1_id = str(next_id)
        builder2_id = str(next_id + 1)
        polisher_id = str(next_id + 2) if include_polisher else ""
        reviewer_id = str(next_id + (3 if include_polisher else 2))
        deployer_id = str(next_id + (4 if include_polisher else 3))
        tester_id = str(next_id + (5 if include_polisher else 4))
        debugger_id = str(next_id + (6 if include_polisher else 5))

        builder_desc_primary = (
            f"{builder_intro}"
            f"{builder_desc_base_primary}\n"
            f"{extra_quality}"
        )
        builder_desc_secondary = (
            f"{builder_intro}"
            f"{builder_desc_base_secondary}\n"
            f"{extra_quality}"
        )
        builder2_dep_ids = list(builder_dep_ids) if is_website else [builder1_id]

        subtasks.extend([
            SubTask(
                id=builder1_id,
                agent_type="builder",
                description=f"{builder_desc_primary}\n{focus_1}",
                depends_on=list(builder_dep_ids),
            ),
            SubTask(
                id=builder2_id,
                agent_type="builder",
                description=f"{builder_desc_secondary}\n{focus_2}",
                depends_on=builder2_dep_ids,
            ),
        ])
        polish_depends = [builder1_id, builder2_id]
        if include_polisher:
            subtasks.append(
                SubTask(
                    id=polisher_id,
                    agent_type="polisher",
                    description=f"{polisher_desc}\n{extra_quality}",
                    depends_on=list(dict.fromkeys([builder1_id, builder2_id] + post_builder_quality_dep_ids)),
                )
            )
            polish_depends = [polisher_id]
        subtasks.extend([
            SubTask(
                id=reviewer_id,
                agent_type="reviewer",
                description=reviewer_desc,
                depends_on=polish_depends,
            ),
            SubTask(
                id=deployer_id,
                agent_type="deployer",
                description=deployer_desc,
                depends_on=polish_depends,
            ),
            SubTask(
                id=tester_id,
                agent_type="tester",
                description=tester_desc,
                depends_on=[reviewer_id, deployer_id],
            ),
            SubTask(
                id=debugger_id,
                agent_type="debugger",
                description=(
                    "Fix any issues found by reviewer/tester. "
                    + ("Refine the assembled pages generated from parallel builder parts if needed. " if is_website else "")
                    + "Use file_ops list to discover ALL HTML pages in /tmp/evermind_output/, "
                    "then file_ops read EACH page and fix every issue found. "
                    "Save corrected versions via file_ops write. "
                    "Check EVERY page, not just index.html — secondary pages are equally important. "
                    "If no issues were found, confirm everything is good."
                ),
                depends_on=[tester_id],
            ),
        ])
        return subtasks

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
            if path.exists() and path.is_file() and not self._is_internal_non_deliverable_html(path)
        ]
        if not final_candidates:
            return None
        normalized_root = self._normalize_generated_path(str(OUTPUT_DIR / "index.html"))

        def _sort_key(path: Path) -> tuple[int, int, int, str]:
            normalized = self._normalize_generated_path(str(path))
            try:
                rel = path.resolve().relative_to(OUTPUT_DIR.resolve())
                depth = len(rel.parts)
            except Exception:
                depth = len(path.parts)
            return (
                0 if normalized == normalized_root else 1,
                0 if path.name == "index.html" else 1,
                depth,
                normalized,
            )

        final_candidates.sort(key=_sort_key)
        return final_candidates[0]

    def _stable_preview_root(self) -> Path:
        run_label = f"run_{int(self._run_started_at * 1000)}" if self._run_started_at > 0 else "run_manual"
        return OUTPUT_DIR / "_stable_previews" / run_label

    def _promote_stable_preview(
        self,
        *,
        subtask_id: str,
        stage: str,
        files_created: List[str],
        preview_artifact: Optional[Path],
    ) -> Optional[Dict[str, Any]]:
        if preview_artifact is None or not preview_artifact.exists():
            return None

        output_root = OUTPUT_DIR.resolve()
        snapshot_root = self._stable_preview_root() / f"{int(time.time() * 1000)}_{stage}_task_{subtask_id}"
        copied_files: List[str] = []
        normalized_inputs = list(files_created or [])
        preview_norm = self._normalize_generated_path(str(preview_artifact))
        if preview_norm not in normalized_inputs:
            normalized_inputs.append(preview_norm)

        for raw_path in normalized_inputs:
            try:
                source = Path(raw_path).expanduser().resolve()
                rel = source.relative_to(output_root)
            except Exception:
                continue
            if rel.parts and rel.parts[0] == "_stable_previews":
                continue
            if not source.exists() or not source.is_file() or self._is_internal_non_deliverable_html(source):
                continue
            destination = snapshot_root / rel
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied_files.append(str(destination))
            except Exception as e:
                logger.warning(f"Failed to promote stable preview artifact {source}: {e}")

        try:
            preview_rel = preview_artifact.resolve().relative_to(output_root)
        except Exception:
            return None

        stable_preview = snapshot_root / preview_rel
        if not stable_preview.exists():
            return None

        self._stable_preview_path = stable_preview
        self._stable_preview_files = copied_files
        self._stable_preview_stage = stage
        return {
            "preview_url": build_preview_url_for_file(stable_preview, output_dir=OUTPUT_DIR),
            "files": copied_files,
            "output_dir": str(OUTPUT_DIR),
            "stable_preview": True,
            "stage": stage,
        }

    def _html_quality_report(self, html: str, source: str = "") -> Dict:
        lower = (html or "").lower()
        errors: List[str] = []
        warnings: List[str] = []
        score = 100
        source_file: Optional[Path] = None
        if source and source not in {"inline", "output_text"}:
            try:
                candidate = Path(source)
                if candidate.exists() and candidate.is_file():
                    source_file = candidate
            except Exception:
                source_file = None
        stylesheet_ctx = collect_stylesheet_context(html, source_file)
        script_ctx = collect_script_context(html, source_file)
        script_safety = inspect_shared_local_script_safety(html, source_file)
        body_structure = inspect_body_structure(html)

        if has_truncation_marker(html):
            errors.append("HTML contains a literal truncation marker, so the page is corrupted")
            score -= 30

        if "<!-- evermind-bootstrap scaffold -->" in lower or "name=\"evermind-bootstrap\"" in lower:
            errors.append("HTML still contains Evermind bootstrap scaffold markers")
            score -= 30

        required_tags = [
            ("<!doctype html>", "Missing <!DOCTYPE html>"),
            ("<html", "Missing <html> tag"),
            ("<head", "Missing <head> tag"),
            ("<body", "Missing <body> tag"),
            ("</html>", "Missing </html> closing tag"),
            ("meta name=\"viewport\"", "Missing mobile viewport meta tag"),
        ]
        for token, message in required_tags:
            if token not in lower:
                errors.append(message)
                score -= 18

        if not stylesheet_ctx.get("has_inline_style") and not stylesheet_ctx.get("has_local_stylesheet"):
            errors.append("Missing inline <style> block or local linked stylesheet")
            score -= 18
        elif stylesheet_ctx.get("missing_local_stylesheets") and not stylesheet_ctx.get("has_inline_style"):
            warnings.append("Linked local stylesheet could not be resolved during validation")
            score -= 8

        for err in script_safety.get("errors", []) or []:
            errors.append(err)
            score -= 20
        for warn in script_safety.get("warnings", []) or []:
            warnings.append(warn)
            score -= 8

        for err in body_structure.get("errors", []) or []:
            errors.append(err)
            score -= 20
        for warn in body_structure.get("warnings", []) or []:
            warnings.append(warn)
            score -= 10

        html_bytes = len((html or "").encode("utf-8"))
        if html_bytes < MIN_COMMERCIAL_HTML_BYTES:
            if stylesheet_ctx.get("has_local_stylesheet") or script_ctx.get("has_local_script"):
                warnings.append(
                    f"HTML shell is lean ({html_bytes} bytes), but shared local assets provide substantial structure."
                )
                score -= 10
            else:
                errors.append(f"HTML output too small ({html_bytes} bytes), likely low-quality")
                score -= 25

        css_text = str(stylesheet_ctx.get("css_text") or "")
        css_lower = css_text.lower()
        css_rules = css_text.count("{")
        if css_rules < MIN_COMMERCIAL_CSS_RULES:
            warnings.append(f"Too few CSS rules ({css_rules}); design may look basic")
            score -= 12

        semantic_hits = sum(1 for t in ("<header", "<main", "<section", "<footer", "<nav") if t in lower)
        if semantic_hits < MIN_SEMANTIC_BLOCKS:
            warnings.append(f"Low semantic structure ({semantic_hits} sections)")
            score -= 10

        # ── Content-completeness gate: use structural parsing so nested HUD /
        # FX layers do not get falsely counted as blank sections.
        completeness = self._html_content_completeness_stats(html)
        total_containers = int(completeness.get("considered", 0) or 0)
        empty_containers = int(completeness.get("empty", 0) or 0)
        if total_containers:
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

        if (
            "display:flex" not in lower and "display: flex" not in lower
            and "display:grid" not in lower and "display: grid" not in lower
            and "display:flex" not in css_lower and "display: flex" not in css_lower
            and "display:grid" not in css_lower and "display: grid" not in css_lower
        ):
            warnings.append("No flex/grid layout detected")
            score -= 8

        if "@media" not in lower and "@media" not in css_lower:
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
            "semantic_blocks": semantic_hits,
            "meaningful_body_tags": body_structure.get("meaningful_tag_count", 0),
            "visible_body_text_len": body_structure.get("visible_text_len", 0),
        }

    def _html_text_is_meaningful(self, text: str) -> bool:
        cleaned = re.sub(r"\s+", "", str(text or ""))
        if not cleaned:
            return False
        if re.search(r"[\u4e00-\u9fff]", cleaned):
            return len(cleaned) >= 2
        if re.search(r"[A-Za-z]", cleaned):
            return len(cleaned) >= 3
        if re.search(r"\d", cleaned):
            return len(cleaned) >= 1
        return len(cleaned) >= 3

    def _html_container_is_utility_shell(
        self,
        tag: str,
        attrs: Dict[str, str],
        *,
        text: str,
        descendant_tags: set[str],
    ) -> bool:
        if tag in {"p", "h1", "h2", "h3", "h4", "h5", "h6"}:
            return False
        if self._html_text_is_meaningful(text):
            return False
        if descendant_tags & {
            "img", "picture", "video", "canvas", "svg", "model-viewer",
            "iframe", "button", "input", "textarea", "select", "progress",
        }:
            return False

        class_id_scope = " ".join([
            str(attrs.get("class") or ""),
            str(attrs.get("id") or ""),
            str(attrs.get("role") or ""),
            str(attrs.get("aria-hidden") or ""),
            str(attrs.get("data-role") or ""),
            str(attrs.get("data-layer") or ""),
        ]).lower()
        style_scope = str(attrs.get("style") or "").lower()

        if attrs.get("aria-hidden", "").lower() == "true":
            return True
        if attrs.get("role", "").lower() in {"presentation", "none"}:
            return True
        if "pointer-events:none" in style_scope or "pointer-events: none" in style_scope:
            return True
        if (
            "position:absolute" in style_scope
            or "position: absolute" in style_scope
            or "position:fixed" in style_scope
            or "position: fixed" in style_scope
        ):
            return True

        return bool(re.search(
            r"(?:^|[\s_-])("
            r"overlay|backdrop|mask|cursor|crosshair|reticle|particle|spark|glow|shine|noise|"
            r"scanline|gridline|beam|trail|flash|fx|effect|decor|divider|separator|"
            r"health-bar|ammo|meter|gauge|progress|fill|ring|pulse|ripple|"
            r"minimap|radar|notification|toast|indicator|mount|portal|layer|background|canvas-overlay"
            r")(?:$|[\s_-])",
            class_id_scope,
            re.IGNORECASE,
        ))

    def _html_content_completeness_stats(self, html: str) -> Dict[str, int]:
        target_tags = {"section", "div", "article", "main", "p", "h1", "h2", "h3", "h4", "h5", "h6"}
        signal_tags = {
            "img", "picture", "video", "canvas", "svg", "model-viewer",
            "iframe", "button", "input", "textarea", "select", "progress",
        }

        _skip_text_tags = {"style", "script", "noscript"}

        class _CompletenessParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.stack: List[Dict[str, Any]] = []
                self.nodes: List[Dict[str, Any]] = []
                self._skip_depth: int = 0  # P1 FIX: suppress style/script text

            def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
                lower_tag = str(tag or "").lower()
                if lower_tag in _skip_text_tags:
                    self._skip_depth += 1
                attr_map = {str(k or "").lower(): str(v or "") for k, v in attrs}
                for node in self.stack:
                    node["descendant_tags"].add(lower_tag)
                    if lower_tag in signal_tags:
                        node["has_signal_descendant"] = True
                if lower_tag in target_tags:
                    self.stack.append({
                        "tag": lower_tag,
                        "attrs": attr_map,
                        "text_parts": [],
                        "descendant_tags": set(),
                        "has_signal_descendant": False,
                    })

            def handle_startendtag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
                self.handle_starttag(tag, attrs)

            def handle_endtag(self, tag: str) -> None:
                lower_tag = str(tag or "").lower()
                if lower_tag in _skip_text_tags:
                    self._skip_depth = max(0, self._skip_depth - 1)
                if lower_tag not in target_tags:
                    return
                for idx in range(len(self.stack) - 1, -1, -1):
                    if self.stack[idx]["tag"] == lower_tag:
                        node = self.stack.pop(idx)
                        node["text"] = "".join(node["text_parts"]).strip()
                        self.nodes.append(node)
                        return

            def handle_data(self, data: str) -> None:
                if self._skip_depth > 0:
                    return  # P1 FIX: inside <style>/<script>, not real content
                text = str(data or "")
                if not text.strip():
                    return
                for node in self.stack:
                    node["text_parts"].append(text)

        parser = _CompletenessParser()
        try:
            parser.feed(str(html or ""))
            parser.close()
        except Exception:
            return {"considered": 0, "empty": 0}

        considered = 0
        empty = 0
        for node in parser.nodes:
            text = str(node.get("text") or "")
            descendant_tags = set(node.get("descendant_tags") or set())
            has_signal_descendant = bool(node.get("has_signal_descendant"))
            if self._html_container_is_utility_shell(
                str(node.get("tag") or ""),
                dict(node.get("attrs") or {}),
                text=text,
                descendant_tags=descendant_tags,
            ):
                continue
            considered += 1
            if not self._html_text_is_meaningful(text) and not has_signal_descendant:
                empty += 1
        return {"considered": considered, "empty": empty}

    def _builder_quality_candidate_files(
        self,
        files_created: List[str],
        *,
        goal: str = "",
        plan: Optional[Plan] = None,
        subtask: Optional[SubTask] = None,
    ) -> List[str]:
        candidates: List[str] = []

        def _add(path_str: str) -> None:
            normalized = self._normalize_generated_path(path_str)
            if not normalized or normalized in candidates:
                return
            candidates.append(normalized)

        for item in files_created or []:
            _add(str(item))

        if not task_classifier.wants_multi_page(goal):
            return candidates

        if plan is not None and subtask is not None:
            for name in self._builder_bootstrap_targets(plan, subtask):
                path = OUTPUT_DIR / Path(str(name)).name
                if path.exists() and path.is_file():
                    _add(str(path))
        else:
            for path in self._current_run_html_artifacts():
                _add(str(path))
            for asset_name in ("styles.css", "app.js"):
                asset = OUTPUT_DIR / asset_name
                if asset.exists() and asset.is_file():
                    _add(str(asset))

        return candidates

    def _postprocess_builder_quality_candidates(self, quality_files: List[str]) -> None:
        task_type = getattr(self, "_current_task_type", "website")
        for item in quality_files or []:
            path = Path(str(item))
            if not path.exists() or not path.is_file():
                continue
            if path.suffix.lower() not in {".html", ".htm", ".js"}:
                continue
            try:
                current_text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            fixed_text = postprocess_generated_text(
                current_text,
                filename=path.name,
                task_type=task_type,
            )
            if fixed_text == current_text:
                continue
            try:
                path.write_text(fixed_text, encoding="utf-8")
                logger.info("Post-processed generated asset: %s", path)
            except Exception as exc:
                logger.warning("Post-process write failed for %s: %s", path, exc)

    def _strip_emoji_glyphs(self, html: str) -> str:
        if not html:
            return html
        return re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", html)

    def _validate_builder_quality(
        self,
        files_created: List[str],
        output: str,
        *,
        goal: str = "",
        plan: Optional[Plan] = None,
        subtask: Optional[SubTask] = None,
    ) -> Dict:
        quality_files = self._builder_quality_candidate_files(
            files_created,
            goal=goal,
            plan=plan,
            subtask=subtask,
        )
        self._postprocess_builder_quality_candidates(quality_files)
        html_path = ""
        preview_artifact = self._select_preview_artifact_for_files(quality_files)
        if preview_artifact is not None:
            html_path = str(preview_artifact)
        else:
            for f in quality_files:
                if f.endswith(".html") or f.endswith(".htm"):
                    html_path = f
                    break

        html = ""
        if html_path and Path(html_path).exists():
            if is_partial_html_artifact(Path(html_path)) and preview_artifact is None:
                if self._is_multi_page_website_goal(goal):
                    return {
                        "pass": False,
                        "score": 20,
                        "errors": [
                            "Partial index_part artifacts are not valid deliverables for a multi-page website. Write real linked pages instead.",
                        ],
                        "warnings": [],
                        "source": html_path,
                    }
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

        language_mismatch = self._goal_language_mismatch_reason(goal, html or output or "")
        if language_mismatch:
            report.setdefault("errors", []).append(language_mismatch)
            report["pass"] = False
            report["score"] = max(int(report.get("score", 0) or 0) - 18, 0)

        pending_builder_siblings = any(
            st.agent_type == "builder"
            and (subtask is None or st.id != subtask.id)
            and st.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED)
            for st in (plan.subtasks if plan else [])
        )

        if task_classifier.classify(goal).task_type == "website":
            palette_summary = self._css_palette_signal_summary(
                self._collect_css_bundle_for_artifacts(quality_files)
            )
            intentional_monochrome = bool(re.search(
                r"(brutalist|minimal(?:ist)?|极简|单色|monochrome|纯黑|纯白)",
                goal or "",
                re.IGNORECASE,
            ))
            if palette_summary.get("flat_monochrome_risk") and not intentional_monochrome:
                palette_msg = (
                    "Website palette is too flat/monochrome: pure black/white root surfaces need coordinated secondary tones, "
                    "surface layers, and accent treatment."
                )
                if pending_builder_siblings:
                    report.setdefault("warnings", []).append(palette_msg)
                else:
                    report.setdefault("errors", []).append(palette_msg)
                    report["pass"] = False
                    report["score"] = max(int(report.get("score", 0) or 0) - 12, 0)

        if task_classifier.wants_motion_rich_experience(goal):
            motion_fragments: List[str] = []
            candidate_paths = [
                Path(item)
                for item in (quality_files or [])
                if str(item).lower().endswith((".html", ".htm", ".css", ".js"))
            ]
            for path in candidate_paths[:12]:
                try:
                    if not path.exists() or not path.is_file():
                        continue
                    motion_fragments.append(path.read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    continue
            if not motion_fragments and html:
                motion_fragments.append(html)
            motion_blob = "\n".join(motion_fragments)
            has_motion_system = bool(re.search(
                r"@keyframes|animation\s*:|animation-name|requestanimationframe|intersectionobserver|"
                r"view-transition|startviewtransition|scroll-timeline|gsap|lenis|"
                r"transition\s*:\s*[^;]*(transform|opacity|clip-path|filter)",
                motion_blob,
                re.IGNORECASE,
            ))
            has_page_transition = bool(re.search(
                r"view-transition|startviewtransition|page-transition|route-transition|transition-overlay|"
                r"page-enter|page-exit|pagereveal|pageswap|sessionstorage.*transition",
                motion_blob,
                re.IGNORECASE,
            ))
            if not has_motion_system:
                report.setdefault("errors", []).append(
                    "Motion-rich brief requires a real motion system; basic static sections or simple hover styling are not enough."
                )
                report["pass"] = False
                report["score"] = max(int(report.get("score", 0) or 0) - 16, 0)
            if task_classifier.wants_multi_page(goal) and not has_page_transition:
                message = (
                    "Multi-page motion brief requires page-to-page transition treatment or continuity choreography; hard cuts are not enough."
                )
                if pending_builder_siblings:
                    report.setdefault("warnings", []).append(message)
                else:
                    report.setdefault("errors", []).append(message)
                    report["pass"] = False
                    report["score"] = max(int(report.get("score", 0) or 0) - 10, 0)

        if task_classifier.wants_multi_page(goal):
            expected_pages = max(task_classifier.requested_page_count(goal), 2)
            real_pages: List[str] = []
            invalid_pages: List[str] = []
            for created in quality_files:
                path = Path(created)
                if path.suffix.lower() not in (".html", ".htm"):
                    continue
                try:
                    rel = path.resolve().relative_to(OUTPUT_DIR.resolve())
                except Exception:
                    continue
                if rel.parts and rel.parts[0] == "_stable_previews":
                    continue
                if len(rel.parts) >= 2 and rel.parts[0].startswith("task_") and rel.name == "index.html":
                    continue
                if is_partial_html_artifact(path):
                    continue
                try:
                    page_html = path.read_text(encoding="utf-8", errors="ignore")
                except Exception as e:
                    invalid_pages.append(f"{rel.as_posix()} (Failed to read HTML: {str(e)[:120]})")
                    continue
                integrity = inspect_html_integrity(page_html)
                if not integrity.get("ok", True):
                    issues = "; ".join(str(item) for item in (integrity.get("errors") or [])[:3])
                    invalid_pages.append(f"{rel.as_posix()} ({issues})")
                    continue
                page_language_mismatch = self._goal_language_mismatch_reason(goal, page_html)
                if page_language_mismatch:
                    invalid_pages.append(f"{rel.as_posix()} ({page_language_mismatch})")
                    continue
                real_pages.append(rel.as_posix())

            if invalid_pages:
                report.setdefault("errors", []).append(
                    "Builder saved invalid or corrupted HTML pages: " + ", ".join(invalid_pages[:8])
                )
                report["pass"] = False
                report["score"] = max(
                    int(report.get("score", 0) or 0) - min(30, 8 * len(invalid_pages)),
                    0,
                )

            if not real_pages:
                report.setdefault("errors", []).append(
                    "Multi-page builder did not save any real named HTML page; task_x/index.html preview fallbacks are not valid deliverables."
                )
                report["pass"] = False
                report["score"] = max(int(report.get("score", 0) or 0) - 24, 0)
            elif (
                subtask is not None
                and not self._builder_can_write_root_index(plan, subtask, goal)
                and not any(page != "index.html" for page in real_pages)
            ):
                report.setdefault("errors", []).append(
                    "Secondary builder must save named pages like brand.html/contact.html instead of only index.html."
                )
                report["pass"] = False
                report["score"] = max(int(report.get("score", 0) or 0) - 24, 0)

            assigned_targets: List[str] = []
            missing_assigned_targets: List[str] = []
            if plan is not None and subtask is not None:
                assigned_targets = self._builder_bootstrap_targets(plan, subtask)
                observed_html = self._evaluate_multi_page_artifacts(goal).get("html_files", []) or []
                missing_assigned_targets = self._missing_builder_targets(
                    plan,
                    subtask,
                    observed_html,
                )
            if missing_assigned_targets and len(assigned_targets) >= 3:
                delivered_ratio = 1.0 - (len(missing_assigned_targets) / max(len(assigned_targets), 1))
                missing_list = ", ".join(str(item) for item in missing_assigned_targets[:8])
                if delivered_ratio >= 0.5:
                    # ≥50% of assigned pages delivered — downgrade to warning.
                    # Let the debugger fill in the missing pages rather than
                    # wasting another full builder cycle.
                    report.setdefault("warnings", []).append(
                        f"Builder delivered {int(delivered_ratio * 100)}% of assigned pages. "
                        f"Missing: {missing_list}. Debugger should complete them."
                    )
                    report["score"] = max(
                        int(report.get("score", 0) or 0) - min(10, 3 * len(missing_assigned_targets)),
                        0,
                    )
                else:
                    report.setdefault("errors", []).append(
                        "Builder did not finish its assigned HTML pages: " + missing_list
                    )
                    report["pass"] = False
                    report["score"] = max(
                        int(report.get("score", 0) or 0) - min(28, 7 * len(missing_assigned_targets)),
                        0,
                )

            page_quality_targets = list(dict.fromkeys(real_pages))
            if page_quality_targets:
                min_page_bytes = (
                    MOTION_MULTI_PAGE_MIN_HTML_BYTES
                    if task_classifier.wants_motion_rich_experience(goal) or expected_pages >= 6
                    else MULTI_PAGE_MIN_HTML_BYTES
                )
                thin_pages: List[str] = []
                for rel_path in page_quality_targets[:16]:
                    page_path = OUTPUT_DIR / rel_path
                    try:
                        page_html = page_path.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        continue
                    page_report = self._html_quality_report(page_html, source=str(page_path))
                    page_bytes = int(page_report.get("bytes", 0) or 0)
                    semantic_blocks = int(page_report.get("semantic_blocks", 0) or 0)
                    visible_html = re.sub(
                        r"<(style|script)\b[^>]*>.*?</\1>",
                        " ",
                        page_html or "",
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    page_text = re.sub(r"<[^>]+>", " ", visible_html)
                    page_text = re.sub(r"\s+", " ", page_text).strip()
                    text_chars = len(page_text)
                    paragraph_count = len(re.findall(r"<p\b", page_html, re.IGNORECASE))
                    heading_count = len(re.findall(r"<h[1-6]\b", page_html, re.IGNORECASE))
                    low_content = text_chars < (180 if min_page_bytes >= MOTION_MULTI_PAGE_MIN_HTML_BYTES else 130)
                    low_structure = semantic_blocks < 3 or heading_count < 2 or paragraph_count < 2
                    too_small = page_bytes < min_page_bytes
                    if (too_small and low_content) or (low_structure and low_content):
                        thin_pages.append(f"{rel_path} ({page_bytes} bytes)")
                if thin_pages:
                    report.setdefault("errors", []).append(
                        "Some multi-page routes are still too thin / stub-like for shipment: "
                        + ", ".join(thin_pages[:8])
                    )
                    report["pass"] = False
                    report["score"] = max(
                        int(report.get("score", 0) or 0) - min(24, 6 * len(thin_pages)),
                        0,
                    )

            multi_page_gate = self._evaluate_multi_page_artifacts(goal)
            if (
                not multi_page_gate.get("ok")
                and not pending_builder_siblings
            ):
                repair_scope = str(multi_page_gate.get("repair_scope") or "")
                can_patch_root = subtask is None or self._builder_can_write_root_index(plan, subtask, goal)
                patched = False
                warning_message = "Auto-patched homepage navigation to expose the full generated page set."
                if repair_scope == "root_nav_only" and can_patch_root:
                    patched = self._auto_patch_root_navigation(multi_page_gate)
                elif repair_scope == "nav_repair":
                    if can_patch_root:
                        patched = self._auto_patch_root_navigation(multi_page_gate) or patched
                    patched = self._auto_patch_secondary_navigation(multi_page_gate) or patched
                    warning_message = "Auto-patched generated navigation to expose the full multi-page route set."
                if patched:
                    multi_page_gate = self._evaluate_multi_page_artifacts(goal)
                    if multi_page_gate.get("ok"):
                        report.setdefault("warnings", []).append(warning_message)
            if not multi_page_gate.get("ok"):
                gate_errors = multi_page_gate.get("errors", []) or []
                repair_scope = str(multi_page_gate.get("repair_scope") or "")
                if pending_builder_siblings:
                    report.setdefault("warnings", []).append(
                        "Multi-page delivery is still incomplete; waiting for sibling builder artifacts before enforcing the final page-count gate."
                    )
                    report.setdefault("warnings", []).extend(gate_errors[:3])
                elif (
                    repair_scope == "root_nav_only"
                    and subtask is not None
                    and not self._builder_can_write_root_index(plan, subtask, goal)
                ):
                    report.setdefault("warnings", []).append(
                        "Homepage navigation still needs repair by Builder 1; preserving secondary builder pages instead of failing them."
                    )
                    report.setdefault("warnings", []).extend(gate_errors[:3])
                else:
                    report.setdefault("errors", []).extend(gate_errors)
                    report["pass"] = False
                    report["score"] = max(int(report.get("score", 0) or 0) - 20, 0)

        gap_candidate_paths = list(quality_files or [])
        if task_classifier.wants_multi_page(goal) and not pending_builder_siblings:
            for path in self._current_run_html_artifacts():
                normalized = str(path)
                if normalized not in gap_candidate_paths:
                    gap_candidate_paths.append(normalized)
            for css_path in self._current_run_css_artifacts():
                normalized = str(css_path)
                if normalized not in gap_candidate_paths:
                    gap_candidate_paths.append(normalized)
        visual_gap_entries = self._visual_gap_entries_for_paths(gap_candidate_paths)
        if visual_gap_entries:
            visual_gap_message = (
                "Generated site still contains unfinished visual placeholders or weak premium media treatment: "
                + "; ".join(str(item).lstrip("- ").strip() for item in visual_gap_entries[:3])
            )
            if pending_builder_siblings:
                report.setdefault("warnings", []).append(visual_gap_message)
            else:
                report.setdefault("errors", []).append(visual_gap_message)
                report["pass"] = False
                report["score"] = max(
                    int(report.get("score", 0) or 0) - min(24, 6 * len(visual_gap_entries)),
                    0,
                )
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
        F7-1: In continuation mode, preserve existing files for iterative improvement.
        """
        clean_enabled = os.getenv("EVERMIND_CLEAN_OUTPUT_ON_RUN", "1").strip().lower() not in ("0", "false", "no")
        if not clean_enabled:
            return
        # F7-1: If the goal signals continuation, preserve existing artifacts
        goal_text = getattr(self, '_current_goal', '') or ''
        conversation_history = getattr(self, "_current_conversation_history", []) or []
        if self._is_continuation_request(goal_text, conversation_history):
            logger.info("Continuation mode detected — preserving existing output files for iterative improvement")
            return
        removed = 0
        stale_root_dirs = {"assets", "browser_records", "_browser_records", "_builder_backups"}
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            for item in OUTPUT_DIR.iterdir():
                if item.is_dir() and item.name.startswith("task_"):
                    shutil.rmtree(item, ignore_errors=True)
                    removed += 1
                    continue
                if item.is_dir() and item.name in stale_root_dirs:
                    shutil.rmtree(item, ignore_errors=True)
                    removed += 1
                    continue
                if item.is_file() and item.suffix.lower() in (".html", ".htm", ".css", ".js", ".json"):
                    try:
                        item.unlink()
                        removed += 1
                    except Exception:
                        pass
                    continue
                if (
                    item.is_file()
                    and item.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                    and re.match(r"^(tmp|temp|screenshot)", item.stem, re.IGNORECASE)
                ):
                    try:
                        item.unlink()
                        removed += 1
                    except Exception:
                        pass
            if removed:
                logger.info(f"Cleaned stale output artifacts before run: removed={removed}")
        except Exception as e:
            logger.warning(f"prepare_output_dir_for_run failed: {e}")

    def _has_existing_output_artifacts(self) -> bool:
        try:
            if not OUTPUT_DIR.exists():
                return False
            for item in OUTPUT_DIR.iterdir():
                if item.is_file() and item.suffix.lower() in (".html", ".htm", ".css", ".js", ".json"):
                    return True
                if item.is_dir() and item.name.startswith("task_"):
                    nested = next(item.glob("*.htm*"), None)
                    if nested and nested.is_file():
                        return True
            return False
        except Exception:
            return False

    def _is_continuation_request(self, goal: str, conversation_history: Optional[List[Dict]] = None) -> bool:
        goal_text = str(goal or "").strip()
        if not goal_text:
            return False
        if _CONTINUATION_HINT_RE.search(goal_text):
            return True
        if _NEW_PROJECT_HINT_RE.search(goal_text):
            return False
        history = conversation_history or []
        if not history:
            return False
        if not _ITERATIVE_EDIT_HINT_RE.search(goal_text):
            return False
        if _DEICTIC_PROJECT_HINT_RE.search(goal_text):
            return True
        return self._has_existing_output_artifacts()

    # ═══════════════════════════════════════════
    # Main entry point
    # ═══════════════════════════════════════════
    async def run(self, goal: str, model: str = "kimi-coding", conversation_history: Optional[List[Dict]] = None, difficulty: str = "standard", canonical_context: Optional[Dict[str, Any]] = None) -> Dict:
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
        self._current_goal = goal  # F7-1: Store for continuation detection
        history = conversation_history or []
        self._current_conversation_history = history
        difficulty = difficulty if difficulty in ("simple", "standard", "pro") else "standard"
        self.difficulty = difficulty
        self._reviewer_requeues = 0
        self._stable_preview_path = None
        self._stable_preview_files = []
        self._stable_preview_stage = ""
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
        self._hydrate_stable_preview_from_disk()
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
                    "builder1": "builder",
                    "builder2": "builder",
                    "builder_structure": "builder",
                    "builder_ui": "builder",
                    "builder_copy": "builder",
                    "builder_animation": "builder",
                    "builder_responsive": "builder",
                    "polisher": "polisher",
                    "uidesign": "uidesign",
                    "scribe": "scribe",
                    "imagegen": "imagegen",
                    "spritesheet": "spritesheet",
                    "assetimport": "assetimport",
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

            reconciled_drift = self._reconcile_canonical_context_with_plan(plan)
            if reconciled_drift:
                await self.emit("plan_reconciled", {
                    "drift_count": len(reconciled_drift),
                    "drift_preview": reconciled_drift[:6],
                })

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
            report = self._build_report(plan, result)
            # Guarantee preview_ready fires only for stable / successful final artifacts.
            await self._emit_final_preview(report_success=bool(report.get("success")))
            if report.get("success"):
                await self._refresh_visual_baseline_for_success(plan.goal, report)
            await self.emit("orchestrator_complete", report)
            return report

        except Exception as e:
            logger.error(f"Orchestrator error: {e}")
            await self.emit("orchestrator_error", {"error": str(e)})
            return {"success": False, "error": str(e)}

    # ═══════════════════════════════════════════
    # Phase 1: PLAN — AI decomposes the goal
    # ═══════════════════════════════════════════
    def _build_context_summary(self, goal: str, conversation_history: Optional[List[Dict]] = None) -> str:
        """Build a condensed summary of recent conversation for planner context.

        To avoid cross-task contamination, only reuse history when the new goal
        explicitly signals that it is continuing the current project.
        """
        history = conversation_history or []
        if not history:
            return ""
        if not self._is_continuation_request(goal, history):
            return ""
        # Take last 8 messages, summarize concisely
        recent = history[-8:]
        lines = []
        for msg in recent:
            role = msg.get("role", "unknown")
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            # P2 FIX (Opus): Only filter raw tracebacks, not timeout/failure mentions
            # which often contain valuable context (e.g. "previous attempt timed out, so...")
            if role == "agent" and re.search(
                r"(traceback|stack trace|internalservererror|Traceback \(most recent call last\))",
                content,
                re.IGNORECASE,
            ):
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
                "- In deep mode, you MAY still add imagegen, spritesheet, and assetimport when the brief materially benefits from them, "
                "but those nodes must output prompt packs, manifests, naming rules, and fallback art direction rather than pretending real renders exist.\n"
                "- Do NOT add them as decorative filler. If they are omitted, instruct builders to use high-quality placeholders, SVG/pixel stand-ins, and a clear asset manifest.\n"
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
            strategy = self._deep_mode_profile(goal)
            node_count = int(strategy.get("node_count", 7) or 7)
            parallel_builders = bool(strategy.get("parallel_builders", True))
            scribe_blocks_builders = bool(strategy.get("scribe_blocks_builders", True))
            builder_policy_line = (
                "Pro mode MUST have 2 builders.\n"
                if parallel_builders else
                "For THIS goal, use exactly 1 builder after the specialist handoff. Do NOT split the final build across parallel builders.\n"
            )
            prelude = (
                "Deep mode dynamically expands to 7-10 subtasks depending on goal complexity.\n"
                f"For THIS goal, keep the plan to {node_count} subtasks.\n"
                f"{builder_policy_line}"
            )
            if strategy.get("include_asset_pipeline"):
                return base_rules + prelude + (
                    "This brief is asset-heavy, so include the asset pipeline ahead of the builders.\n"
                    "If no real image backend is configured, those nodes must still output prompt packs, manifests, and fallback art-direction guidance.\n"
                    "REQUIRED structure for this deep-mode goal:\n"
                    "- #1 analyst → research and design brief\n"
                    "- #2 imagegen → prompt pack / asset concept generation\n"
                    "- #3 spritesheet → frame map / asset packaging plan\n"
                    "- #4 assetimport → normalize manifest / runtime paths for builders\n"
                    "- #5 builder → build ownership set A — depends on #1 and #4\n"
                    "- #6 builder → build ownership set B — depends on #1 and #4\n"
                    "- #7 reviewer → browser review with screenshots (depends on #5, #6)\n"
                    "- #8 deployer → confirm files and preview URL (depends on #5, #6)\n"
                    "- #9 tester → full visual/browser test (depends on #7, #8)\n"
                    "- #10 debugger → fix issues from reviewer/tester (depends on #9)\n\n"
                    "Output format:\n"
                    '{"subtasks": [\n'
                    '  {"id": "1", "agent": "analyst", "task": "Research design references", "depends_on": []},\n'
                    '  {"id": "2", "agent": "imagegen", "task": "Create prompt packs or concrete asset concepts", "depends_on": ["1"]},\n'
                    '  {"id": "3", "agent": "spritesheet", "task": "Prepare asset packaging / frame map", "depends_on": ["1","2"]},\n'
                    '  {"id": "4", "agent": "assetimport", "task": "Normalize manifest and runtime asset paths", "depends_on": ["1","2","3"]},\n'
                    '  {"id": "5", "agent": "builder", "task": "Build the first ownership set of deliverables", "depends_on": ["1","4"]},\n'
                    '  {"id": "6", "agent": "builder", "task": "Build the second ownership set of deliverables", "depends_on": ["1","4"]},\n'
                    '  {"id": "7", "agent": "reviewer", "task": "Open preview in browser, take screenshots, output APPROVED or REJECTED.", "depends_on": ["5","6"]},\n'
                    '  {"id": "8", "agent": "deployer", "task": "Confirm files and preview URL", "depends_on": ["5","6"]},\n'
                    '  {"id": "9", "agent": "tester", "task": "Full browser visual test", "depends_on": ["7","8"]},\n'
                    '  {"id": "10", "agent": "debugger", "task": "Fix issues found by reviewer/tester", "depends_on": ["9"]}\n'
                    "]}\n"
                )
            if strategy.get("include_uidesign") and strategy.get("include_scribe") and not parallel_builders:
                if strategy.get("include_polisher"):
                    return base_rules + prelude + (
                        "This brief needs stronger design direction before a single continuity-first build. "
                        "Keep scribe as a parallel content-architecture input and let polisher merge it in during the finish pass.\n"
                        "REQUIRED structure for this deep-mode goal:\n"
                        "- #1 analyst → research and design brief\n"
                        "- #2 uidesign → UI system, motion direction, page hierarchy\n"
                        "- #3 scribe → content architecture, narrative flow, copy deck\n"
                        "- #4 builder → build the complete routed deliverable — depends on #1 and #2\n"
                        "- #5 polisher → refine motion, spacing, hierarchy, and premium finish (depends on #4 and #3)\n"
                        "- #6 reviewer → browser review with screenshots (depends on #5)\n"
                        "- #7 deployer → confirm files and preview URL (depends on #5)\n"
                        "- #8 tester → full visual/browser test (depends on #6, #7)\n"
                        "- #9 debugger → fix issues from reviewer/tester (depends on #8)\n\n"
                        "Output format:\n"
                        '{"subtasks": [\n'
                        '  {"id": "1", "agent": "analyst", "task": "Research references and extract constraints", "depends_on": []},\n'
                        '  {"id": "2", "agent": "uidesign", "task": "Define layout system, motion direction, and visual hierarchy", "depends_on": ["1"]},\n'
                        '  {"id": "3", "agent": "scribe", "task": "Draft page architecture and content plan", "depends_on": ["1"]},\n'
                        '  {"id": "4", "agent": "builder", "task": "Build the complete requested deliverable with all pages/routes and interactions", "depends_on": ["1","2"]},\n'
                        '  {"id": "5", "agent": "polisher", "task": "Refine the strongest existing build with stronger motion, transitions, hierarchy, and premium finish", "depends_on": ["4","3"]},\n'
                        '  {"id": "6", "agent": "reviewer", "task": "Open preview in browser, take screenshots, output APPROVED or REJECTED.", "depends_on": ["5"]},\n'
                        '  {"id": "7", "agent": "deployer", "task": "Confirm files and preview URL", "depends_on": ["5"]},\n'
                        '  {"id": "8", "agent": "tester", "task": "Full browser visual test", "depends_on": ["6","7"]},\n'
                        '  {"id": "9", "agent": "debugger", "task": "Fix issues found by reviewer/tester", "depends_on": ["8"]}\n'
                        "]}\n"
                    )
                return base_rules + prelude + (
                    "This brief needs stronger design direction before a single continuity-first build. "
                    "Keep scribe as a parallel content-architecture input instead of blocking the first build pass.\n"
                    "REQUIRED structure for this deep-mode goal:\n"
                    "- #1 analyst → research and design brief\n"
                    "- #2 uidesign → UI system, motion direction, page hierarchy\n"
                    "- #3 scribe → content architecture, narrative flow, copy deck\n"
                    "- #4 builder → build the complete routed deliverable — depends on #1 and #2\n"
                    "- #5 reviewer → browser review with screenshots (depends on #4)\n"
                    "- #6 deployer → confirm files and preview URL (depends on #4)\n"
                    "- #7 tester → full visual/browser test (depends on #5, #6)\n"
                    "- #8 debugger → fix issues from reviewer/tester (depends on #7)\n\n"
                    "Output format:\n"
                    '{"subtasks": [\n'
                    '  {"id": "1", "agent": "analyst", "task": "Research references and extract constraints", "depends_on": []},\n'
                    '  {"id": "2", "agent": "uidesign", "task": "Define layout system, motion direction, and visual hierarchy", "depends_on": ["1"]},\n'
                    '  {"id": "3", "agent": "scribe", "task": "Draft page architecture and content plan", "depends_on": ["1"]},\n'
                    '  {"id": "4", "agent": "builder", "task": "Build the complete requested deliverable with all pages/routes and interactions", "depends_on": ["1","2"]},\n'
                    '  {"id": "5", "agent": "reviewer", "task": "Open preview in browser, take screenshots, output APPROVED or REJECTED.", "depends_on": ["4"]},\n'
                    '  {"id": "6", "agent": "deployer", "task": "Confirm files and preview URL", "depends_on": ["4"]},\n'
                    '  {"id": "7", "agent": "tester", "task": "Full browser visual test", "depends_on": ["5","6"]},\n'
                    '  {"id": "8", "agent": "debugger", "task": "Fix issues found by reviewer/tester", "depends_on": ["7"]}\n'
                    "]}\n"
                )
            if strategy.get("include_uidesign") and strategy.get("include_scribe"):
                if strategy.get("include_polisher"):
                    builder_dep_line = (
                        "depends on #1, #2, #3"
                        if scribe_blocks_builders else
                        "depends on #1 and #2"
                    )
                    polisher_dep_line = (
                        "depends on #4, #5"
                        if scribe_blocks_builders else
                        "depends on #4, #5, #3"
                    )
                    builder_dep_json = '["1","2","3"]' if scribe_blocks_builders else '["1","2"]'
                    polisher_dep_json = '["4","5"]' if scribe_blocks_builders else '["4","5","3"]'
                    return base_rules + prelude + (
                        "This brief needs stronger design direction and content architecture, but the builders should still start fast in parallel.\n"
                        "REQUIRED structure for this deep-mode goal:\n"
                        "- #1 analyst → research and design brief\n"
                        "- #2 uidesign → UI system, motion direction, page hierarchy\n"
                        "- #3 scribe → content architecture, narrative flow, copy deck\n"
                        f"- #4 builder → build ownership set A — {builder_dep_line}\n"
                        f"- #5 builder → build ownership set B — {builder_dep_line}\n"
                        f"- #6 polisher → merge/refine the strongest builder output and premium finish it ({polisher_dep_line})\n"
                        "- #7 reviewer → browser review with screenshots (depends on #6)\n"
                        "- #8 deployer → confirm files and preview URL (depends on #6)\n"
                        "- #9 tester → full visual/browser test (depends on #7, #8)\n"
                        "- #10 debugger → fix issues from reviewer/tester (depends on #9)\n\n"
                        "Output format:\n"
                        '{"subtasks": [\n'
                        '  {"id": "1", "agent": "analyst", "task": "Research references and extract constraints", "depends_on": []},\n'
                        '  {"id": "2", "agent": "uidesign", "task": "Define layout system, motion direction, and visual hierarchy", "depends_on": ["1"]},\n'
                        '  {"id": "3", "agent": "scribe", "task": "Draft page architecture and content plan", "depends_on": ["1"]},\n'
                        f'  {{"id": "4", "agent": "builder", "task": "Build the first ownership set of deliverables", "depends_on": {builder_dep_json}}},\n'
                        f'  {{"id": "5", "agent": "builder", "task": "Build the second ownership set of deliverables", "depends_on": {builder_dep_json}}},\n'
                        f'  {{"id": "6", "agent": "polisher", "task": "Refine and merge the strongest builder output with better motion and premium finish", "depends_on": {polisher_dep_json}}},\n'
                        '  {"id": "7", "agent": "reviewer", "task": "Open preview in browser, take screenshots, output APPROVED or REJECTED.", "depends_on": ["6"]},\n'
                        '  {"id": "8", "agent": "deployer", "task": "Confirm files and preview URL", "depends_on": ["6"]},\n'
                        '  {"id": "9", "agent": "tester", "task": "Full browser visual test", "depends_on": ["7","8"]},\n'
                        '  {"id": "10", "agent": "debugger", "task": "Fix issues found by reviewer/tester", "depends_on": ["9"]}\n'
                        "]}\n"
                    )
                builder_dep_line = (
                    "depends on #1, #2, #3"
                    if scribe_blocks_builders else
                    "depends on #1 and #2"
                )
                builder_dep_json = '["1","2","3"]' if scribe_blocks_builders else '["1","2"]'
                return base_rules + prelude + (
                    "This brief needs stronger design direction and content architecture, but the builders should still start fast in parallel.\n"
                    "REQUIRED structure for this deep-mode goal:\n"
                    "- #1 analyst → research and design brief\n"
                    "- #2 uidesign → UI system, motion direction, page hierarchy\n"
                    "- #3 scribe → content architecture, narrative flow, copy deck\n"
                    f"- #4 builder → build ownership set A — {builder_dep_line}\n"
                    f"- #5 builder → build ownership set B — {builder_dep_line}\n"
                    "- #6 reviewer → browser review with screenshots (depends on #4, #5)\n"
                    "- #7 deployer → confirm files and preview URL (depends on #4, #5)\n"
                    "- #8 tester → full visual/browser test (depends on #6, #7)\n"
                    "- #9 debugger → fix issues from reviewer/tester (depends on #8)\n\n"
                    "Output format:\n"
                    '{"subtasks": [\n'
                    '  {"id": "1", "agent": "analyst", "task": "Research references and extract constraints", "depends_on": []},\n'
                    '  {"id": "2", "agent": "uidesign", "task": "Define layout system, motion direction, and visual hierarchy", "depends_on": ["1"]},\n'
                    '  {"id": "3", "agent": "scribe", "task": "Draft page architecture and content plan", "depends_on": ["1"]},\n'
                    f'  {{"id": "4", "agent": "builder", "task": "Build the first ownership set of deliverables", "depends_on": {builder_dep_json}}},\n'
                    f'  {{"id": "5", "agent": "builder", "task": "Build the second ownership set of deliverables", "depends_on": {builder_dep_json}}},\n'
                    '  {"id": "6", "agent": "reviewer", "task": "Open preview in browser, take screenshots, output APPROVED or REJECTED.", "depends_on": ["4","5"]},\n'
                    '  {"id": "7", "agent": "deployer", "task": "Confirm files and preview URL", "depends_on": ["4","5"]},\n'
                    '  {"id": "8", "agent": "tester", "task": "Full browser visual test", "depends_on": ["6","7"]},\n'
                    '  {"id": "9", "agent": "debugger", "task": "Fix issues found by reviewer/tester", "depends_on": ["8"]}\n'
                    "]}\n"
                )
            if strategy.get("include_uidesign"):
                if strategy.get("include_polisher"):
                    return base_rules + prelude + (
                        "This brief needs explicit UI design direction before the parallel builders start, followed by a dedicated finish pass.\n"
                        "REQUIRED structure for this deep-mode goal:\n"
                        "- #1 analyst → research and design brief\n"
                        "- #2 uidesign → UI system, motion direction, page hierarchy\n"
                        "- #3 builder → build ownership set A — depends on #1 and #2\n"
                        "- #4 builder → build ownership set B — depends on #1 and #2\n"
                        "- #5 polisher → refine/merge the strongest builder output and premium finish it (depends on #3, #4)\n"
                        "- #6 reviewer → browser review with screenshots (depends on #5)\n"
                        "- #7 deployer → confirm files and preview URL (depends on #5)\n"
                        "- #8 tester → full visual/browser test (depends on #6, #7)\n"
                        "- #9 debugger → fix issues from reviewer/tester (depends on #8)\n\n"
                        "Output format:\n"
                        '{"subtasks": [\n'
                        '  {"id": "1", "agent": "analyst", "task": "Research references and extract constraints", "depends_on": []},\n'
                        '  {"id": "2", "agent": "uidesign", "task": "Define layout system, motion direction, and visual hierarchy", "depends_on": ["1"]},\n'
                        '  {"id": "3", "agent": "builder", "task": "Build the first ownership set of deliverables", "depends_on": ["1","2"]},\n'
                        '  {"id": "4", "agent": "builder", "task": "Build the second ownership set of deliverables", "depends_on": ["1","2"]},\n'
                        '  {"id": "5", "agent": "polisher", "task": "Refine and merge the strongest builder output with better motion and premium finish", "depends_on": ["3","4"]},\n'
                        '  {"id": "6", "agent": "reviewer", "task": "Open preview in browser, take screenshots, output APPROVED or REJECTED.", "depends_on": ["5"]},\n'
                        '  {"id": "7", "agent": "deployer", "task": "Confirm files and preview URL", "depends_on": ["5"]},\n'
                        '  {"id": "8", "agent": "tester", "task": "Full browser visual test", "depends_on": ["6","7"]},\n'
                        '  {"id": "9", "agent": "debugger", "task": "Fix issues found by reviewer/tester", "depends_on": ["8"]}\n'
                        "]}\n"
                    )
                return base_rules + prelude + (
                    "This brief needs explicit UI design direction before the parallel builders start.\n"
                    "REQUIRED structure for this deep-mode goal:\n"
                    "- #1 analyst → research and design brief\n"
                    "- #2 uidesign → UI system, motion direction, page hierarchy\n"
                    "- #3 builder → build ownership set A — depends on #1 and #2\n"
                    "- #4 builder → build ownership set B — depends on #1 and #2\n"
                    "- #5 reviewer → browser review with screenshots (depends on #3, #4)\n"
                    "- #6 deployer → confirm files and preview URL (depends on #3, #4)\n"
                    "- #7 tester → full visual/browser test (depends on #5, #6)\n"
                    "- #8 debugger → fix issues from reviewer/tester (depends on #7)\n\n"
                    "Output format:\n"
                    '{"subtasks": [\n'
                    '  {"id": "1", "agent": "analyst", "task": "Research references and extract constraints", "depends_on": []},\n'
                    '  {"id": "2", "agent": "uidesign", "task": "Define layout system, motion direction, and visual hierarchy", "depends_on": ["1"]},\n'
                    '  {"id": "3", "agent": "builder", "task": "Build the first ownership set of deliverables", "depends_on": ["1","2"]},\n'
                    '  {"id": "4", "agent": "builder", "task": "Build the second ownership set of deliverables", "depends_on": ["1","2"]},\n'
                    '  {"id": "5", "agent": "reviewer", "task": "Open preview in browser, take screenshots, output APPROVED or REJECTED.", "depends_on": ["3","4"]},\n'
                    '  {"id": "6", "agent": "deployer", "task": "Confirm files and preview URL", "depends_on": ["3","4"]},\n'
                    '  {"id": "7", "agent": "tester", "task": "Full browser visual test", "depends_on": ["5","6"]},\n'
                    '  {"id": "8", "agent": "debugger", "task": "Fix issues found by reviewer/tester", "depends_on": ["7"]}\n'
                    "]}\n"
                )
            if strategy.get("include_polisher"):
                return base_rules + prelude + (
                    "REQUIRED structure for pro mode:\n"
                    "- #1 analyst → research and design brief\n"
                    "- #2 builder → build ownership set A — depends on #1\n"
                    "- #3 builder → build ownership set B — depends on #1 (PARALLEL with #2!)\n"
                    "- #4 polisher → refine/merge the strongest builder output and premium finish it (depends on #2, #3)\n"
                    "- #5 reviewer → open browser, take screenshots, output APPROVED or REJECTED with detailed reasons (depends on #4)\n"
                    "- #6 deployer → confirm files and preview URL (depends on #4)\n"
                    "- #7 tester → full visual test (depends on #5, #6)\n"
                    "- #8 debugger → fix issues from reviewer/tester (depends on #7)\n\n"
                    "CRITICAL: Both builders (#2 and #3) MUST depend on #1 ONLY. This makes them run in PARALLEL.\n"
                    "For SINGLE-PAGE website tasks, builders may split by sections and save to separate partial files. "
                    "For MULTI-PAGE website tasks, builders MUST split by full pages/routes, not top-vs-bottom fragments. "
                    "Builder #2 should own index.html plus part of the requested page set; Builder #3 should own the remaining linked pages. "
                    "Planner MUST assign DISTINCT, non-overlapping ownership to each builder.\n"
                    "Polisher MUST preserve the best structure while upgrading motion and premium finish.\n"
                    "Reviewer MUST open the browser preview, take screenshots, and output APPROVED or REJECTED.\n\n"
                    "Output format:\n"
                    '{"subtasks": [\n'
                    '  {"id": "1", "agent": "analyst", "task": "Research design references", "depends_on": []},\n'
                    '  {"id": "2", "agent": "builder", "task": "Build the home page plus the first ownership set of deliverables. Save full HTML files under /tmp/evermind_output/", "depends_on": ["1"]},\n'
                    '  {"id": "3", "agent": "builder", "task": "Build the remaining ownership set of linked deliverables. Save full HTML files under /tmp/evermind_output/", "depends_on": ["1"]},\n'
                    '  {"id": "4", "agent": "polisher", "task": "Refine and merge the strongest builder output with stronger motion and premium finish", "depends_on": ["2","3"]},\n'
                    '  {"id": "5", "agent": "reviewer", "task": "Open http://127.0.0.1:8765/preview/ in browser, take screenshots, check quality. Output APPROVED or REJECTED with reasons.", "depends_on": ["4"]},\n'
                    '  {"id": "6", "agent": "deployer", "task": "Confirm files and preview URL", "depends_on": ["4"]},\n'
                    '  {"id": "7", "agent": "tester", "task": "Full browser visual test", "depends_on": ["5","6"]},\n'
                    '  {"id": "8", "agent": "debugger", "task": "Fix issues found by reviewer/tester", "depends_on": ["7"]}\n'
                    "]}\n"
                )
            return base_rules + prelude + (
                "REQUIRED structure for pro mode:\n"
                "- #1 analyst → research and design brief\n"
                "- #2 builder → build ownership set A — depends on #1\n"
                "- #3 builder → build ownership set B — depends on #1 (PARALLEL with #2!)\n"
                "- #4 reviewer → open browser, take screenshots, output APPROVED or REJECTED with detailed reasons (depends on #2, #3)\n"
                "- #5 deployer → confirm files and preview URL (depends on #2, #3)\n"
                "- #6 tester → full visual test (depends on #4, #5)\n"
                "- #7 debugger → fix issues from reviewer/tester (depends on #6)\n\n"
                "CRITICAL: Both builders (#2 and #3) MUST depend on #1 ONLY. This makes them run in PARALLEL.\n"
                "For SINGLE-PAGE website tasks, builders may split by sections and save to separate partial files. "
                "For MULTI-PAGE website tasks, builders MUST split by full pages/routes, not top-vs-bottom fragments. "
                "Builder #2 should own index.html plus part of the requested page set; Builder #3 should own the remaining linked pages. "
                "Planner MUST assign DISTINCT, non-overlapping ownership to each builder.\n"
                "Reviewer MUST open the browser preview, take screenshots, and output APPROVED or REJECTED.\n\n"
                "Output format:\n"
                '{"subtasks": [\n'
                '  {"id": "1", "agent": "analyst", "task": "Research design references", "depends_on": []},\n'
                '  {"id": "2", "agent": "builder", "task": "Build the home page plus the first ownership set of deliverables. Save full HTML files under /tmp/evermind_output/", "depends_on": ["1"]},\n'
                '  {"id": "3", "agent": "builder", "task": "Build the remaining ownership set of linked deliverables. Save full HTML files under /tmp/evermind_output/", "depends_on": ["1"]},\n'
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
        builder_desc = self._builder_task_description(goal)
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
            return self._build_pro_plan_subtasks(
                goal,
                analyst_desc=f"ADVANCED MODE — {task_classifier.analyst_description(goal)}",
            )
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
        asset_pipeline_enabled = self._asset_pipeline_enabled_for_goal(goal)
        builder_desc_base_primary, _ = self._parallel_builder_task_descriptions(goal)
        builder_desc_base = builder_desc_base_primary
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
            analyst_desc = ""
            for st in plan.subtasks:
                if st.agent_type == "analyst" and st.description.strip() and not analyst_desc:
                    analyst_desc = st.description.strip()

            if not analyst_desc:
                analyst_desc = task_classifier.analyst_description(goal)
            plan.subtasks = self._build_pro_plan_subtasks(goal, analyst_desc=analyst_desc)
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
        context_summary = self._build_context_summary(goal, conversation_history)

        planner_node = {
            "type": "router",
            "prompt": self._planner_prompt_for_difficulty(goal, difficulty),
            "model": model,
            "model_is_default": True,
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
            # Enforce the canonical topology even on fallback so deep mode does not
            # drift into an invalid builder/reviewer/tester chain.
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
                    st.status = TaskStatus.BLOCKED
                    st.error = f"Blocked by failed dependencies (not executed): {', '.join(failed_deps)}"
                    results[st.id] = {"success": False, "error": st.error, "blocked_by": failed_deps, "blocked": True}
                    completed.add(st.id)
                    failed.add(st.id)
                    blocked.append(st.id)
                    await self._sync_ne_status(
                        st.id,
                        "blocked",
                        output_summary=self._humanize_output_summary(st.agent_type, st.error, False),
                        error_message=st.error,
                    )
                    await self.emit("subtask_progress", {
                        "subtask_id": st.id,
                        "stage": "blocked",
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
                if blocked:
                    logger.warning(f"Blocked subtasks due to failed dependencies: {blocked}")
                if newly_ready:
                    # Optional upstream nodes failed, but these downstream tasks are
                    # now runnable. Execute them in this iteration instead of
                    # misclassifying them as "stuck" before the loop reaches the
                    # normal ready-subtask execution path.
                    ready = newly_ready
                elif blocked:
                    if not newly_ready:
                        continue
                else:
                    # Check if done or stuck only when no optional-dependency bypass
                    # has produced fresh runnable work in this iteration.
                    all_done = all(
                        st.id in completed or st.status in (TaskStatus.COMPLETED, TaskStatus.BLOCKED, TaskStatus.CANCELLED)
                        for st in plan.subtasks
                    )
                    if all_done:
                        break
                    # Stuck — dependencies can't be satisfied
                    stuck = [
                        st for st in plan.subtasks
                        if st.id not in completed and st.status not in (TaskStatus.BLOCKED, TaskStatus.CANCELLED)
                    ]
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
                            await self._sync_ne_status(
                                st.id,
                                "failed",
                                output_summary=self._humanize_output_summary(st.agent_type, st.error, False),
                                error_message=st.error,
                            )
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

            # F3-3: Stagger parallel builders to avoid simultaneous API rate limit hits
            builder_count = sum(1 for st in ready if st.agent_type == "builder")
            async def _staggered_execute(st, stagger_idx):
                if st.agent_type == "builder" and builder_count > 1 and stagger_idx > 0:
                    stagger = random.uniform(1.5, 3.5)
                    logger.info(f"Staggering builder {st.id} start by {stagger:.1f}s to avoid API rate collision")
                    await asyncio.sleep(stagger)
                return await self._execute_subtask(st, plan, model, results)
            builder_idx = 0
            tasks = []
            for st in ready:
                sidx = builder_idx if st.agent_type == "builder" else 0
                if st.agent_type == "builder":
                    builder_idx += 1
                tasks.append(_staggered_execute(st, sidx))
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

                    # Invalidate the entire downstream chain independent of plan order.
                    # Custom/canonical plans are not guaranteed to be topologically sorted.
                    downstream_invalidated = self._collect_transitive_downstream_ids(plan, requeue_ids)
                    all_invalidated = list(dict.fromkeys(requeue_ids + downstream_invalidated))
                    if downstream_invalidated:
                        logger.info(
                            "Requeue chain: also invalidating downstream %s",
                            downstream_invalidated,
                        )
                    await self.emit("subtask_progress", {
                        "subtask_id": st.id,
                        "stage": "requeue_downstream",
                        "message": f"Reset downstream tasks for requeue: {', '.join(all_invalidated)}",
                        "requeue_subtasks": all_invalidated,
                    })

                    for inv_id in all_invalidated:
                        target = next((task for task in plan.subtasks if str(task.id) == inv_id), None)
                        if target:
                            target.status = TaskStatus.PENDING
                            target.output = ""
                            target.error = ""
                            target.completed_at = 0
                            self._reset_progress_tracking(target.id)
                            # Reset NE progress so UI shows the node is re-queued
                            try:
                                await self._sync_ne_status(
                                    target.id,
                                    "queued",
                                    progress=0,
                                    phase="requeued",
                                    reset_started_at=True,
                                    error_message="",
                                    output_summary="",
                                )
                            except Exception:
                                pass
                        results.pop(inv_id, None)
                        completed.discard(inv_id)
                        succeeded.discard(inv_id)
                        failed.discard(inv_id)
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
                    if result.get("retryable", True) is False:
                        st.status = TaskStatus.FAILED
                        st.error = str(result.get("error") or st.error or "Non-retryable failure").strip()[:400]
                        results[st.id] = {
                            **result,
                            "success": False,
                            "non_retryable": True,
                            "error": st.error,
                        }
                        completed.add(st.id)
                        failed.add(st.id)
                        succeeded.discard(st.id)
                        await self._sync_ne_status(
                            st.id,
                            "failed",
                            output_summary=self._humanize_output_summary(st.agent_type, st.error, False),
                            error_message=st.error,
                        )
                        await self.emit("subtask_progress", {
                            "subtask_id": st.id,
                            "stage": "non_retryable_failure",
                            "message": st.error[:300],
                        })
                    else:
                        # Failed — attempt retry
                        retry_ok = await self._handle_failure(st, plan, model, results)
                        if retry_ok:
                            results[st.id] = {"success": True, "output": st.output, "retried": True}
                            completed.add(st.id)
                            succeeded.add(st.id)
                        else:
                            if st.status == TaskStatus.FAILED:
                                results[st.id] = {"success": False, "error": st.error}
                                completed.add(st.id)
                                failed.add(st.id)
                            else:
                                results.pop(st.id, None)
                                completed.discard(st.id)
                                failed.discard(st.id)
                                succeeded.discard(st.id)

            if await self._enforce_multi_page_builder_aggregate_gate(
                plan,
                results,
                completed,
                succeeded,
                failed,
            ):
                continue

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
        subtask.started_at = time.time()
        subtask.ended_at = 0.0
        logger.info(
            f"Subtask start: id={subtask.id} agent={subtask.agent_type} retries={subtask.retries} "
            f"task={subtask.description[:140]}"
        )

        await self.emit("subtask_start", {
            "subtask_id": subtask.id,
            "agent": subtask.agent_type,
            "task": subtask.description[:200],
        })
        if subtask.agent_type in ("polisher", "reviewer", "tester", "deployer"):
            self._materialize_parallel_builder_preview()

        # ── Emit loaded skills for UI visibility ──
        loaded_skills: List[str] = []
        loaded_skill_records: List[Dict[str, Any]] = []
        try:
            from agent_skills import resolve_skill_records_for_goal
            goal_text = plan.goal if hasattr(plan, 'goal') else str(subtask.description)
            loaded_skill_records = resolve_skill_records_for_goal(subtask.agent_type, goal_text)
            loaded_skills = [
                str(record.get("name") or "").strip()
                for record in loaded_skill_records
                if str(record.get("name") or "").strip()
            ]
            if loaded_skills:
                self._update_ne_context(subtask.id, loaded_skills=loaded_skills)
                self._append_ne_activity(
                    subtask.id,
                    f"已加载技能：{', '.join(loaded_skills[:6])}",
                    entry_type="sys",
                )
                resource_bits: List[str] = []
                for record in loaded_skill_records[:6]:
                    title = str(record.get("title") or record.get("name") or "").strip()
                    source_name = str(record.get("source_name") or "").strip()
                    if title and source_name and source_name not in {"Evermind Built-in", "Community Skill"}:
                        resource_bits.append(f"{title}（{source_name}）")
                    elif title:
                        resource_bits.append(title)
                if resource_bits:
                    self._append_ne_activity(
                        subtask.id,
                        f"已激活资源包：{'; '.join(resource_bits)}",
                        entry_type="info",
                    )
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "skills_loaded",
                    "skills": loaded_skills,
                    "skill_records": loaded_skill_records[:8],
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
        if subtask.agent_type == "builder":
            self._snapshot_root_index_for_secondary_builder(plan, subtask)
            self._ensure_builder_bootstrap_scaffold(plan, subtask)
        initial_progress = 12 if getattr(subtask, "retries", 0) > 0 else 5
        await self._emit_ne_progress(subtask.id, progress=initial_progress, phase="starting", partial_output=f"Starting {subtask.agent_type}...")
        try:
            task_profile = task_classifier.classify(plan.goal)
        except Exception:
            task_profile = type("TaskProfile", (), {"task_type": "website"})()
        desktop_qa_session: Dict[str, Any] = {}

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
                # Tighter condensation for scribe outputs to reduce builder prompt bloat
                if dep_task.agent_type == "scribe" and subtask.agent_type == "builder":
                    condensed = self._condense_handoff_seed(dep_output, limit=600)
                    context_parts.append(
                        f"[Result from scribe #{dep_id}]:\n{condensed}"
                    )
                    continue
                # Debugger and builder need full reviewer/tester feedback to fix issues
                if dep_task.agent_type in ("reviewer", "tester") and subtask.agent_type in ("debugger", "builder"):
                    context_parts.append(
                        f"[Result from {dep_task.agent_type} #{dep_id}]:\n{dep_output[:MAX_DEP_CONTEXT_CHARS * 3]}"
                    )
                    continue
                context_parts.append(
                    f"[Result from {dep_task.agent_type} #{dep_id}]:\n{dep_output[:MAX_DEP_CONTEXT_CHARS]}"
                )
            elif dep_task:
                fallback_handoff = self._fallback_dependency_handoff(dep_task, plan.goal)
                if fallback_handoff:
                    context_parts.append(
                        f"[Fallback from {dep_task.agent_type} #{dep_id}]:\n{fallback_handoff[:MAX_DEP_CONTEXT_CHARS]}"
                    )

        context = "\n\n".join(context_parts)
        debugger_noop_reason = self._debugger_noop_reason(plan, subtask, prev_results)
        if debugger_noop_reason:
            result = {
                "success": True,
                "output": debugger_noop_reason,
                "error": "",
                "tool_results": [],
                "tool_call_stats": {},
                "files_created": [],
                "mode": "debugger_noop",
            }
            subtask.status = TaskStatus.COMPLETED
            subtask.output = debugger_noop_reason
            subtask.error = ""
            subtask.completed_at = time.time()
            subtask.ended_at = subtask.completed_at
            self._append_ne_activity(subtask.id, debugger_noop_reason, entry_type="info")
            await self.emit("subtask_progress", {
                "subtask_id": subtask.id,
                "stage": "debugger_noop",
                "message": debugger_noop_reason,
            })
            await self._emit_ne_progress(
                subtask.id,
                progress=100,
                phase="no_changes_required",
                partial_output=debugger_noop_reason,
            )
            await self._sync_ne_status(
                subtask.id,
                "passed",
                output_summary=debugger_noop_reason[:200],
                phase="no_changes_required",
            )
            await self.emit("subtask_complete", {
                "subtask_id": subtask.id,
                "agent": subtask.agent_type,
                "success": True,
                "output_preview": debugger_noop_reason[:2000],
                "full_output": debugger_noop_reason,
                "files_created": [],
                "error": "",
                "tokens_used": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost": 0.0,
            })
            logger.info(
                "Subtask done: id=%s agent=%s success=True mode=debugger_noop files=0",
                subtask.id,
                subtask.agent_type,
            )
            return result
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
        builder_direct_multifile_mode = False
        builder_direct_text_mode = False
        builder_refinement_context = ""
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
            builder_direct_multifile_mode = self._builder_execution_direct_multifile_mode(plan, subtask, model)
            builder_direct_text_mode = self._builder_execution_direct_text_mode(plan, subtask)
            builder_refinement_context = self._builder_refinement_context(plan, subtask)
            # Builder should focus on deterministic local file writes, not preview navigation.
            assigned_targets = self._builder_bootstrap_targets(plan, subtask)
            assigned_line = (
                "Assigned HTML filenames for this builder: "
                + ", ".join(assigned_targets)
                + ".\n"
                if assigned_targets else
                ""
            )
            if builder_direct_multifile_mode:
                output_info = (
                    f"[System Context]\n"
                    f"Output directory: {str(OUTPUT_DIR)}\n"
                    f"Files must target: {str(OUTPUT_DIR)}/\n"
                    f"{assigned_line}"
                    "If bootstrap draft files already exist, overwrite those exact filenames with the final pages rather than inventing new slugs.\n"
                    "This builder run is in DIRECT MULTI-FILE DELIVERY mode.\n"
                    "Return fenced HTML blocks for the assigned filenames directly in the model response.\n"
                    "Do not start with browser research, file_ops list, or file_ops read unless a later retry explicitly requires it.\n"
                    "Deliver the assigned pages in continuation-safe batches and keep the filenames exact.\n"
                    "A single-page draft is considered incomplete delivery.\n"
                )
            elif builder_direct_text_mode:
                output_info = (
                    f"[System Context]\n"
                    f"Output directory: {str(OUTPUT_DIR)}\n"
                    f"Files must target: {str(OUTPUT_DIR)}/\n"
                    f"{assigned_line}"
                    "This builder run is in DIRECT SINGLE-FILE DELIVERY mode.\n"
                    "Return one complete fenced ```html index.html``` block directly in the model response.\n"
                    "Do not start with browser research, file_ops list, or file_ops read.\n"
                    "Prioritize a working gameplay shell and full visible <body> content over tool usage.\n"
                    + (
                        "This is a refinement pass on an existing artifact: preserve the strongest current structure and fill/replace empty containers instead of restarting from zero.\n"
                        if builder_refinement_context else
                        ""
                    )
                )
            else:
                output_info = (
                    f"[System Context]\n"
                    f"Output directory: {str(OUTPUT_DIR)}\n"
                    f"Files must be written to: {str(OUTPUT_DIR)}/\n"
                    f"{assigned_line}"
                    "If bootstrap draft files already exist, overwrite those exact filenames with the final pages rather than inventing new slugs.\n"
                    "For multi-page website work, your first meaningful action should be file_ops write calls that overwrite the assigned HTML files.\n"
                    "Do not spend the first turn browsing, listing, or planning if no write error has occurred.\n"
                    "A single-page draft is considered incomplete delivery.\n"
                    f"Use file_ops write for final HTML save.\n"
                )
            # F4-2: Ban referencing nonexistent local resources
            output_info += (
                "Do NOT reference local files (images, fonts, scripts) that do not exist in the output directory. "
                "Use CDN URLs or inline SVG/CSS instead of invented local file paths like 'hero-bg.jpg'.\n"
            )
            # F7-3: In continuation mode, inject existing file list so builder can improve rather than recreate
            if self._is_continuation_request(str(plan.goal or ""), getattr(self, "_current_conversation_history", [])):
                try:
                    existing_files = [
                        f.name for f in OUTPUT_DIR.iterdir()
                        if f.is_file() and f.suffix.lower() in (".html", ".css", ".js", ".json")
                    ]
                    if existing_files:
                        output_info += (
                            f"\n[EXISTING FILES from previous run — available for iterative improvement]\n"
                            f"{', '.join(sorted(existing_files))}\n"
                            "You may file_ops read these to understand the current state, then overwrite with improvements.\n"
                        )
                except Exception:
                    pass
        else:
            preview_hint = ""
            if subtask.agent_type in {"polisher", "reviewer", "tester", "deployer", "debugger"}:
                preview_hint = self._current_preview_hint(plan.goal)
            output_info = (
                f"[System Context]\n"
                f"Output directory: {str(OUTPUT_DIR)}\n"
                f"Preview server URL: http://127.0.0.1:{PREVIEW_PORT}/preview/\n"
                f"{preview_hint}"
                f"Files should be written to: {str(OUTPUT_DIR)}/\n"
            )
        desktop_qa_session: Dict[str, Any] = {}
        if subtask.agent_type in {"reviewer", "tester"}:
            desktop_qa_session = await self._maybe_collect_desktop_qa_session(
                subtask,
                plan.goal,
                str(getattr(task_profile, "task_type", "") or "website"),
            )
        desktop_qa_usable = self._desktop_qa_session_usable(desktop_qa_session)
        runtime_review_contract = ""
        if subtask.agent_type in {"reviewer", "tester"}:
            html_pages = [
                self._normalize_preview_path(str(path.relative_to(OUTPUT_DIR)))
                for path in self._current_run_html_artifacts()
            ]
            html_pages = [item for item in html_pages if item]
            if html_pages:
                runtime_review_contract = (
                    "[Runtime Review Contract]\n"
                    + (
                        "You MUST inspect every current page in this artifact set: "
                        + ", ".join(html_pages[:16]) + ".\n"
                        if task_classifier.wants_multi_page(plan.goal)
                        else "Inspect the current preview artifact before scoring.\n"
                    )
                    + "For multi-page reviews, first prove at least one real internal navigation path works. "
                    + "After that, you may directly open the remaining known preview paths to finish coverage faster; if navigation is broken, that is a rejection reason, not a reason to skip coverage.\n"
                    + "For at least one real interactive element, click or fill it and then IMMEDIATELY call browser observe, wait_for, or record_scroll to prove the visible state changed.\n"
                    + "Perform full-depth scrolling on the homepage and at least one representative secondary page; use lighter load checks on the remaining routes unless they show route-specific issues.\n"
                )
            if str(getattr(task_profile, "task_type", "") or "") == "game" and desktop_qa_usable:
                runtime_review_contract += (
                    "A desktop Evermind QA Preview Session has already recorded the gameplay path inside the internal preview window.\n"
                    "Treat that session as the primary gameplay evidence.\n"
                    "[Desktop QA Browser Policy]\n"
                    "DESKTOP_QA_BROWSER_SUPPRESSED=1\n"
                    "Do NOT launch browser/browser_use in this pass. Use the desktop QA evidence as the authoritative interaction record.\n"
                )
        polisher_visual_gap_report = ""
        if subtask.agent_type == "polisher":
            polisher_visual_gap_report = self._polisher_visual_gap_report()
        input_parts = [subtask.description]
        if skill_contract:
            input_parts.append(skill_contract)
        input_parts.append(output_info)
        if subtask.agent_type == "builder":
            if builder_refinement_context:
                input_parts.append(builder_refinement_context)
        if polisher_visual_gap_report:
            input_parts.append(polisher_visual_gap_report)
        if runtime_review_contract:
            input_parts.append(runtime_review_contract)
        if desktop_qa_session.get("summary"):
            input_parts.append(str(desktop_qa_session.get("summary") or ""))
        if context:
            input_parts.append(context)
        full_input = "\n\n".join(part.strip() for part in input_parts if str(part or "").strip())
        builder_allowed_html_targets = (
            self._builder_allowed_html_targets(plan, subtask)
            if subtask.agent_type == "builder"
            else []
        )

        # Create a virtual node for the agent
        agent_node = {
            "type": subtask.agent_type,
            "model": model,
            "model_is_default": True,
            "id": f"auto_{subtask.id}",
            "name": f"{subtask.agent_type.title()} #{subtask.id}",
            "output_dir": str(OUTPUT_DIR),
            "run_id": str((self._canonical_ctx or {}).get("run_id") or ""),
            "node_execution_id": str(self._ne_id_for_subtask(subtask.id) or ""),
        }
        if subtask.agent_type == "builder":
            # Always propagate bootstrap targets so file_ops error messages
            # and repair prompts can guide the model with concrete paths,
            # even for single-builder plans where write-restriction is off.
            agent_node["allowed_html_targets"] = builder_allowed_html_targets
            # Multi-page builders should stay on their assigned route set so
            # retries cannot drift into unplanned slugs.
            agent_node["enforce_html_targets"] = bool(builder_allowed_html_targets)
            agent_node["can_write_root_index"] = self._builder_can_write_root_index(plan, subtask, plan.goal)
            if builder_direct_multifile_mode:
                agent_node["builder_delivery_mode"] = "direct_multifile"
            elif builder_direct_text_mode:
                agent_node["builder_delivery_mode"] = "direct_text"
        try:
            preferred_model = self.ai_bridge.preferred_model_for_node(agent_node, model)
            preferred_provider = str(self.ai_bridge._resolve_model(preferred_model).get("provider", "") or "")
            await self._sync_ne_status(
                subtask.id,
                "running",
                assigned_model=preferred_model,
                assigned_provider=preferred_provider,
            )
        except Exception:
            preferred_model = model
            preferred_provider = ""

        enabled = resolve_enabled_plugins_for_node(
            subtask.agent_type,
            config=getattr(self.ai_bridge, "config", None),
        )
        if (
            subtask.agent_type in {"reviewer", "tester"}
            and str(getattr(task_profile, "task_type", "") or "") == "game"
            and desktop_qa_usable
        ):
            enabled = [name for name in enabled if name not in {"browser", "browser_use"}]
            self._append_ne_activity(
                subtask.id,
                "游戏审查已拿到桌面 QA 预览证据，本轮禁用外部浏览器链，优先使用 Evermind 内部会话结果。",
                entry_type="sys",
            )
        if enabled and any(PluginRegistry.get(name) is None for name in enabled):
            try:
                from plugins.implementations import register_all as register_builtin_plugins
                register_builtin_plugins()
            except Exception:
                pass
        if self._should_suppress_builder_browser(plan, subtask, repo_context):
            enabled = [name for name in enabled if name != "browser"]
            self._append_ne_activity(
                subtask.id,
                "已禁用 builder 浏览器研究，优先直接落盘页面文件，避免空转和回退覆盖。",
                entry_type="sys",
            )
        if builder_direct_multifile_mode:
            self._append_ne_activity(
                subtask.id,
                (
                    "Builder 已切换为多文件文本直出重试模式：本轮等待最终 HTML 代码块返回，不再要求先触发 file_ops 首写事件。"
                    if self._builder_direct_multifile_mode(subtask)
                    else "Builder 已为 Kimi 多页交付预切换为多文件文本直出模式，跳过首轮 file_ops 首写等待。"
                ),
                entry_type="sys",
            )
            await self.emit("subtask_progress", {
                "subtask_id": subtask.id,
                "stage": "builder_direct_multifile_mode",
                "message": (
                    "Builder retry is waiting for direct multi-file HTML output instead of file_ops first-write events."
                    if self._builder_direct_multifile_mode(subtask)
                    else "Builder auto-switched to direct multi-file HTML delivery for Kimi multi-page generation."
                ),
            })
        elif builder_direct_text_mode:
            self._append_ne_activity(
                subtask.id,
                "Builder 已切换为单文件直出模式：本轮直接返回完整 index.html 代码块，不再等待 file_ops 首写事件。",
                entry_type="sys",
            )
            await self.emit("subtask_progress", {
                "subtask_id": subtask.id,
                "stage": "builder_direct_text_mode",
                "message": "Builder switched to direct single-file HTML delivery for game generation.",
            })
        plugins = [PluginRegistry.get(p) for p in enabled if PluginRegistry.get(p)]

        subtask.builder_has_written_file = False
        subtask.builder_written_files = []
        subtask.builder_last_write_at = 0
        last_partial_output = getattr(subtask, "last_partial_output", "") or ""
        browser_actions: List[Dict[str, Any]] = []
        runtime_assigned_model = str(preferred_model or model or "").strip()
        runtime_assigned_provider = str(preferred_provider or "").strip()
        runtime_builder_direct_multifile_mode = bool(builder_direct_multifile_mode)
        runtime_direct_multifile_announced = bool(builder_direct_multifile_mode)
        runtime_builder_direct_text_mode = bool(builder_direct_text_mode)
        runtime_direct_text_announced = bool(builder_direct_text_mode)
        builder_nav_repair_retry = (
            self._builder_nav_repair_retry_active(subtask)
            if subtask.agent_type == "builder"
            else False
        )
        builder_retry_locked_snapshot = (
            self._capture_builder_retry_locked_root_artifacts(plan, subtask)
            if builder_nav_repair_retry
            else {}
        )
        timeout_sec = 0
        pre_polish_quality: Optional[Dict[str, Any]] = None
        pre_polish_signal_snapshot: Optional[Dict[str, Any]] = None
        if subtask.agent_type == "polisher":
            try:
                pre_polish_quality = self._validate_builder_quality(
                    [],
                    "",
                    goal=plan.goal,
                )
            except Exception:
                pre_polish_quality = None
            try:
                pre_polish_signal_snapshot = self._capture_route_signal_snapshot()
            except Exception:
                pre_polish_signal_snapshot = None

        async def on_progress(data):
            nonlocal last_partial_output
            nonlocal runtime_assigned_model
            nonlocal runtime_assigned_provider
            nonlocal runtime_builder_direct_multifile_mode
            nonlocal runtime_direct_multifile_announced
            nonlocal runtime_builder_direct_text_mode
            nonlocal runtime_direct_text_announced
            nonlocal timeout_sec
            data = dict(data or {})
            stage = str(data.get("stage") or "").strip().lower()
            source = str(data.get("source") or "").strip().lower()
            preview = str(data.get("preview") or data.get("partial_output") or "").strip()
            if stage == "partial_output" and source == "model" and preview:
                last_partial_output = preview[:4000]
                subtask.last_partial_output = last_partial_output
            assigned_model_update = data.get("assignedModel")
            assigned_provider_update = data.get("assignedProvider")
            assignment_changed = False
            if assigned_model_update is not None:
                next_assigned_model = str(assigned_model_update or "").strip()[:100]
                if next_assigned_model != runtime_assigned_model:
                    runtime_assigned_model = next_assigned_model
                    assignment_changed = True
            if assigned_provider_update is not None:
                next_assigned_provider = str(assigned_provider_update or "").strip()[:60]
                if next_assigned_provider != runtime_assigned_provider:
                    runtime_assigned_provider = next_assigned_provider
                    assignment_changed = True
            if (
                assignment_changed
                and runtime_assigned_model
                and not runtime_assigned_provider
            ):
                try:
                    runtime_assigned_provider = str(
                        self.ai_bridge._resolve_model(runtime_assigned_model).get("provider", "") or ""
                    )[:60]
                except Exception:
                    pass
            if subtask.agent_type == "builder":
                explicit_delivery_mode = str(
                    data.get("builder_delivery_mode")
                    or data.get("builderDeliveryMode")
                    or data.get("delivery_mode")
                    or ""
                ).strip().lower()
                explicit_direct_flag = data.get("builder_direct_multifile")
                if explicit_direct_flag is None:
                    explicit_direct_flag = data.get("builderDirectMultifile")
                inferred_runtime_direct_multifile = bool(explicit_direct_flag) or explicit_delivery_mode == "direct_multifile"
                inferred_runtime_direct_text = explicit_delivery_mode == "direct_text"
                if not inferred_runtime_direct_multifile and runtime_assigned_model:
                    inferred_runtime_direct_multifile = self._builder_execution_direct_multifile_mode(
                        plan,
                        subtask,
                        runtime_assigned_model,
                    )
                if not inferred_runtime_direct_text:
                    inferred_runtime_direct_text = self._builder_execution_direct_text_mode(
                        plan,
                        subtask,
                    )
                if inferred_runtime_direct_multifile and not runtime_builder_direct_multifile_mode:
                    runtime_builder_direct_multifile_mode = True
                    runtime_timeout = self._execution_timeout_for_subtask(
                        plan,
                        subtask,
                        runtime_assigned_model or model,
                    )
                    if runtime_timeout > timeout_sec:
                        timeout_sec = runtime_timeout
                        self._sync_ne_timeout_budget(subtask.id, timeout_sec)
                        self._append_ne_activity(
                            subtask.id,
                            f"Builder 运行时已切换为多文件直出，超时预算提升到 {timeout_sec}s。",
                            entry_type="sys",
                        )
                    if not runtime_direct_multifile_announced:
                        runtime_direct_multifile_announced = True
                        self._append_ne_activity(
                            subtask.id,
                            "Builder 已根据运行时模型切换为多文件文本直出模式，当前轮次不再等待 file_ops 首写事件。",
                            entry_type="sys",
                        )
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "builder_direct_multifile_mode",
                            "message": "Builder runtime switched to direct multi-file HTML delivery after model fallback.",
                            "assignedModel": runtime_assigned_model,
                            "assignedProvider": runtime_assigned_provider,
                        })
                if inferred_runtime_direct_text and not runtime_builder_direct_text_mode:
                    runtime_builder_direct_text_mode = True
                    if not runtime_direct_text_announced:
                        runtime_direct_text_announced = True
                        self._append_ne_activity(
                            subtask.id,
                            "Builder 已根据运行时配置切换为单文件文本直出模式，当前轮次不再等待 file_ops 首写事件。",
                            entry_type="sys",
                        )
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "builder_direct_text_mode",
                            "message": "Builder runtime switched to direct single-file HTML delivery.",
                            "assignedModel": runtime_assigned_model,
                            "assignedProvider": runtime_assigned_provider,
                        })
            if assignment_changed:
                await self._sync_ne_status(
                    subtask.id,
                    "running",
                    assigned_model=runtime_assigned_model,
                    assigned_provider=runtime_assigned_provider,
                )
            if stage == "executing_plugin":
                plugin_name = str(data.get("plugin") or "").strip()
                if plugin_name:
                    self._append_ne_activity(
                        subtask.id,
                        f"工具调用：{plugin_name}",
                        entry_type="sys",
                    )
            if stage == "builder_write" and subtask.agent_type == "builder":
                subtask.builder_has_written_file = True
                subtask.builder_last_write_at = time.time()
                written_path = str(data.get("path") or "").strip()
                if written_path and written_path not in subtask.builder_written_files:
                    subtask.builder_written_files.append(written_path)
                data.setdefault("output_dir", str(OUTPUT_DIR))
                self._append_ne_activity(
                    subtask.id,
                    f"已落盘真实文件：{written_path or 'HTML write detected'}",
                    entry_type="ok",
                )
            if stage == "builder_multifile_continue" and subtask.agent_type == "builder":
                continuation = int(data.get("continuation", 0) or 0)
                next_batch = [
                    str(item).strip()
                    for item in (data.get("next_batch") or [])
                    if str(item).strip()
                ]
                finish_reason = str(data.get("finish_reason") or "").strip()
                batch_line = ", ".join(next_batch[:6]) if next_batch else "unknown targets"
                self._append_ne_activity(
                    subtask.id,
                    (
                        f"Builder 多文件续批 {continuation} 已发起：{batch_line}"
                        + (f"（上一批结束原因：{finish_reason}）" if finish_reason else "")
                    ),
                    entry_type="info",
                )
            if stage == "builder_multifile_batch_ready" and subtask.agent_type == "builder":
                batch_content = str(data.get("content") or "")
                batch_files = self._extract_and_save_code(
                    batch_content,
                    subtask.id,
                    allow_root_index_copy=self._builder_can_write_root_index(plan, subtask, plan.goal),
                    multi_page_required=task_classifier.wants_multi_page(plan.goal),
                    allowed_html_targets=builder_allowed_html_targets or None,
                    allow_multi_page_raw_html_fallback=True,
                    allow_named_shared_asset_blocks=not builder_nav_repair_retry,
                    is_retry=bool(subtask.retries and subtask.retries > 0),
                )
                batch_files = list(dict.fromkeys(
                    self._normalize_generated_path(path)
                    for path in batch_files
                    if str(path).strip()
                ))
                if batch_files:
                    subtask.builder_has_written_file = True
                    subtask.builder_last_write_at = time.time()
                    subtask.builder_written_files = list(dict.fromkeys(
                        list(subtask.builder_written_files or []) + batch_files
                    ))
                    self._append_ne_activity(
                        subtask.id,
                        "已从 Builder 多文件批次回填文件："
                        + ", ".join(Path(path).name for path in batch_files[:8]),
                        entry_type="ok",
                    )
                data.pop("content", None)
                data["saved_files"] = batch_files[:20]
                data.setdefault("output_dir", str(OUTPUT_DIR))
            if stage == "browser_action":
                browser_action = {
                    "plugin": str(data.get("plugin") or "").strip().lower(),
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
                    "scroll_y": int(data.get("scroll_y", 0) or 0),
                    "viewport_height": int(data.get("viewport_height", 0) or 0),
                    "page_height": int(data.get("page_height", 0) or 0),
                    "is_scrollable": data.get("is_scrollable"),
                    "at_bottom": bool(data.get("at_bottom", False)),
                    "at_top": bool(data.get("at_top", False)),
                    "can_scroll_more": data.get("can_scroll_more"),
                    "console_error_count": int(data.get("console_error_count", 0) or 0),
                    "page_error_count": int(data.get("page_error_count", 0) or 0),
                    "failed_request_count": int(data.get("failed_request_count", 0) or 0),
                    "recent_failed_requests": [
                        {
                            "url": str(item.get("url") or "").strip(),
                            "error": str(item.get("error") or "").strip(),
                            "resource_type": str(item.get("resource_type") or "").strip().lower(),
                        }
                        for item in (data.get("recent_failed_requests") or [])[:3]
                        if isinstance(item, dict)
                    ],
                    "recording_path": str(data.get("recording_path") or "").strip(),
                    "capture_path": str(data.get("capture_path") or "").strip(),
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
            timeout_sec = self._execution_timeout_for_subtask(plan, subtask, model)
            self._sync_ne_timeout_budget(subtask.id, timeout_sec)
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
                    has_file_write=bool(getattr(subtask, "builder_has_written_file", False)),
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

                # §P0-FIRST-WRITE: Early abort if builder has not written any
                # real file within BUILDER_FIRST_WRITE_TIMEOUT_SEC.
                # Scaffold files written by _ensure_builder_bootstrap_scaffold
                # do NOT set builder_has_written_file; only real file_ops
                # writes fire the builder_write progress event.
                if (
                    subtask.agent_type == "builder"
                    and not runtime_builder_direct_multifile_mode
                    and not runtime_builder_direct_text_mode
                    and elapsed >= BUILDER_FIRST_WRITE_TIMEOUT_SEC
                    and not subtask.builder_has_written_file
                ):
                    exec_task.cancel()
                    try:
                        await exec_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                    self._append_ne_activity(
                        subtask.id,
                        f"⚠️ Builder 已运行 {elapsed}s 但未产出任何真实文件，触发提前超时重试",
                        entry_type="warn",
                    )
                    salvaged_files = self._salvage_builder_partial_output(plan, subtask, last_partial_output)
                    if salvaged_files:
                        subtask.builder_written_files = list(dict.fromkeys(
                            list(subtask.builder_written_files or []) + salvaged_files
                        ))
                        self._append_ne_activity(
                            subtask.id,
                            "已从超时前的部分输出中回收 HTML，避免下次重试从空白骨架重新开始。",
                            entry_type="info",
                        )
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "builder_partial_salvage",
                            "files": salvaged_files[:12],
                        })
                    timeout_msg = (
                        f"builder first-write timeout: {elapsed}s elapsed with no real file written. "
                        "Builder may be stalled after scaffold generation."
                    )
                    logger.warning(f"[Builder] First-write timeout for subtask {subtask.id}: {timeout_msg}")
                    result = {"success": False, "output": last_partial_output or "", "error": timeout_msg, "tool_results": []}
                    subtask.last_partial_output = last_partial_output
                    break

                if (
                    subtask.agent_type == "builder"
                    and not runtime_builder_direct_multifile_mode
                    and not runtime_builder_direct_text_mode
                    and subtask.builder_has_written_file
                    and getattr(subtask, "builder_last_write_at", 0)
                    and (time.time() - float(subtask.builder_last_write_at or 0)) >= BUILDER_POST_WRITE_IDLE_TIMEOUT_SEC
                ):
                    exec_task.cancel()
                    try:
                        await exec_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                    idle_elapsed = int(time.time() - float(subtask.builder_last_write_at or start_ts))
                    self._append_ne_activity(
                        subtask.id,
                        f"⚠️ Builder 最后一次真实写入后空闲 {idle_elapsed}s，触发提前收尾校验。",
                        entry_type="warn",
                    )
                    timeout_msg = (
                        f"builder post-write idle timeout: no new real file writes for {idle_elapsed}s after the last builder_write event."
                    )
                    logger.warning(f"[Builder] Post-write idle timeout for subtask {subtask.id}: {timeout_msg}")
                    result = {"success": False, "output": last_partial_output or "", "error": timeout_msg, "tool_results": []}
                    subtask.last_partial_output = last_partial_output
                    break

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
                    if subtask.agent_type == "builder" and last_partial_output:
                        salvaged_files = self._salvage_builder_partial_output(plan, subtask, last_partial_output)
                        if salvaged_files:
                            subtask.builder_written_files = list(dict.fromkeys(
                                list(subtask.builder_written_files or []) + salvaged_files
                            ))
                            self._append_ne_activity(
                                subtask.id,
                                f"总超时前已回收 {len(salvaged_files)} 个部分 HTML 文件，避免重试从空白开始。",
                                entry_type="info",
                            )
                            await self.emit("subtask_progress", {
                                "subtask_id": subtask.id,
                                "stage": "builder_partial_salvage",
                                "files": salvaged_files[:12],
                            })
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
            preview_ready_payload: Optional[Dict[str, Any]] = None
            tool_call_stats = result.get("tool_call_stats", {}) if isinstance(result, dict) else {}
            if not isinstance(tool_call_stats, dict):
                tool_call_stats = {}
            tool_results = result.get("tool_results", [])
            if not isinstance(tool_results, list):
                tool_results = []
            persisted_tool_artifacts = self._persist_tool_artifacts(subtask.id, tool_results)
            if persisted_tool_artifacts:
                artifact_types = sorted({
                    str(item.get("artifact_type") or "").strip()
                    for item in persisted_tool_artifacts
                    if isinstance(item, dict)
                })
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "tool_artifacts_persisted",
                    "count": len(persisted_tool_artifacts),
                    "artifact_types": artifact_types,
                })
                self._append_ne_activity(
                    subtask.id,
                    f"已持久化 {len(persisted_tool_artifacts)} 个工具证据产物"
                    + (f"（{', '.join(artifact_types)}）" if artifact_types else ""),
                    entry_type="info",
                )
            if subtask.agent_type in {"reviewer", "tester"}:
                desktop_qa_actions = desktop_qa_session.get("actions") if isinstance(desktop_qa_session.get("actions"), list) else []
                if desktop_qa_actions:
                    browser_actions.extend(
                        item for item in desktop_qa_actions
                        if isinstance(item, dict)
                    )
                    tool_call_stats["desktop_qa_session"] = max(1, int(tool_call_stats.get("desktop_qa_session", 0) or 0))
                    self._append_ne_activity(
                        subtask.id,
                        f"已接入桌面 QA 会话证据，共 {len(desktop_qa_actions)} 条交互动作。",
                        entry_type="info",
                    )

            # ── Collect files created by tools or extract from text ──
            files_created = []
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

            if subtask.agent_type == "builder" and not files_created and subtask.builder_written_files:
                files_created = [
                    self._normalize_generated_path(path)
                    for path in subtask.builder_written_files
                    if str(path or "").strip()
                ]

            # Fallback: extract code blocks from AI text and save as files
            if subtask.agent_type == "builder" and result.get("success") and full_output and not files_created:
                files_created = self._extract_and_save_code(
                    full_output,
                    subtask.id,
                    allow_root_index_copy=self._builder_can_write_root_index(plan, subtask, plan.goal),
                    multi_page_required=task_classifier.wants_multi_page(plan.goal),
                    allowed_html_targets=builder_allowed_html_targets or None,
                    allow_named_shared_asset_blocks=not builder_nav_repair_retry,
                    is_retry=bool(subtask.retries and subtask.retries > 0),
                )

            # Final fallback for builder: scan output directory for recent HTML.
            # Only enabled during an active run (run_started_at set) to avoid
            # accidentally reusing stale artifacts in isolated/test executions.
            if subtask.agent_type == "builder" and not files_created and self._run_started_at > 0:
                scan_cutoff = max((subtask.started_at or self._run_started_at) - 1.0, 0.0)
                files_created = self._collect_recent_builder_disk_scan_files(
                    plan,
                    subtask,
                    scan_cutoff=scan_cutoff,
                )
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
                if subtask.agent_type == "builder":
                    files_created = self._merge_builder_runtime_html_files(plan, subtask, files_created)
                    files_created, dropped_html = self._sanitize_builder_generated_files(
                        plan,
                        subtask,
                        files_created,
                        prev_results=prev_results,
                    )
                    if dropped_html:
                        self._append_ne_activity(
                            subtask.id,
                            (
                                "已过滤 builder 非法 HTML 产物："
                                + ", ".join(dropped_html[:8])
                                + "；仅保留当前 builder 负责的命名页面。"
                            ),
                            entry_type="warn",
                        )
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "builder_artifacts_sanitized",
                            "dropped_html": dropped_html[:12],
                            "kept_files": files_created[:20],
                        })
                    if builder_nav_repair_retry:
                        restored_locked = self._restore_builder_retry_locked_root_artifacts(
                            plan,
                            subtask,
                            builder_retry_locked_snapshot,
                        )
                        if restored_locked:
                            self._append_ne_activity(
                                subtask.id,
                                "已恢复导航修复重试中不允许改动的文件："
                                + ", ".join(restored_locked[:8])
                                + "；仅保留 index.html 的定点修复。",
                                entry_type="warn",
                            )
                            await self.emit("subtask_progress", {
                                "subtask_id": subtask.id,
                                "stage": "builder_nav_repair_locked_restore",
                                "restored_files": restored_locked[:12],
                            })
                        locked_targets = {
                            Path(name).name
                            for name in self._builder_allowed_html_targets(plan, subtask)
                            if str(name or "").strip()
                        }
                        if locked_targets:
                            files_created = [
                                item for item in files_created
                                if Path(item).name in locked_targets
                            ]

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

                if preview_html:
                    preview_url = build_preview_url_for_file(preview_html, output_dir=OUTPUT_DIR)
                    preview_ready_payload = {
                        "subtask_id": subtask.id,
                        "preview_url": preview_url,
                        "files": files_created,
                        "output_dir": str(OUTPUT_DIR),
                        "final": False,
                    }
                    # Strong artifact gate: ensure preview target file exists and passes baseline checks
                    # before this version is exposed as the active preview.
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

            if subtask.agent_type == "builder" and not result.get("success") and files_created:
                root_index_norm = self._normalize_generated_path(str(OUTPUT_DIR / "index.html"))
                normalized_created = {self._normalize_generated_path(path) for path in files_created}
                ownership_msg = ""
                if (
                    root_index_norm in normalized_created
                    and not self._builder_can_write_root_index(plan, subtask, plan.goal)
                ):
                    ownership_msg = (
                        "Only Builder 1 may write /tmp/evermind_output/index.html for a multi-page site. "
                        "Save your work as named secondary pages and keep the homepage ownership stable."
                    )
                    self._restore_root_index_after_secondary_builder(plan, subtask, prev_results=prev_results)
                    subtask.error = ownership_msg
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "builder_failed_artifact_root_ownership",
                        "message": ownership_msg,
                    })
                else:
                    salvage_quality = self._validate_builder_quality(
                        files_created,
                        full_output,
                        goal=plan.goal,
                        plan=plan,
                        subtask=subtask,
                    )
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "builder_failed_artifact_quality",
                        "score": salvage_quality.get("score"),
                        "errors": salvage_quality.get("errors", [])[:5],
                        "warnings": salvage_quality.get("warnings", [])[:5],
                    })
                    if salvage_quality.get("pass"):
                        result["success"] = True
                        result["error"] = ""
                        subtask.error = ""
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "builder_artifact_salvaged",
                            "message": "Builder timed out, but the saved artifacts passed quality gates and were promoted for downstream use.",
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

                root_index = OUTPUT_DIR / "index.html"
                root_index_norm = self._normalize_generated_path(str(root_index))
                normalized_created = {self._normalize_generated_path(path) for path in files_created}
                if (
                    root_index_norm in normalized_created
                    and not self._builder_can_write_root_index(plan, subtask, plan.goal)
                ):
                    ownership_msg = (
                        "Only Builder 1 may write /tmp/evermind_output/index.html for a multi-page site. "
                        "Save your work as named secondary pages and keep the homepage ownership stable."
                    )
                    self._restore_root_index_after_secondary_builder(plan, subtask, prev_results=prev_results)
                    result["success"] = False
                    result["error"] = ownership_msg
                    subtask.error = ownership_msg
                    self._append_ne_activity(
                        subtask.id,
                        "检测到非首页 builder 覆盖根 index.html，已回滚到稳定首页快照。",
                        entry_type="warn",
                    )
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "builder_root_ownership_failed",
                        "message": ownership_msg,
                    })
                else:
                    quality = self._validate_builder_quality(
                        files_created,
                        full_output,
                        goal=plan.goal,
                        plan=plan,
                        subtask=subtask,
                    )
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
                        # Preserve existing index.html for the retry rather than
                        # deleting it — deletion forces scaffold re-seeding which
                        # loses all builder progress and wastes another full attempt.
                        # The quality gate already marks result as failed.
                        result["success"] = False
                        result["error"] = quality_msg
                        subtask.error = quality_msg
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "quality_gate_failed",
                            "message": quality_msg,
                        })
                    else:
                        regression_reasons = self._builder_retry_regression_reasons(subtask, files_created)
                        if regression_reasons:
                            restored_files = self._restore_output_from_stable_preview()
                            regression_msg = (
                                "Builder regression guard failed: "
                                + "; ".join(regression_reasons[:3])
                            )
                            if restored_files:
                                regression_msg += ". Restored the latest stable preview."
                                self._append_ne_activity(
                                    subtask.id,
                                    f"Builder 重试触发降级保护，已回滚到稳定版本：{'; '.join(regression_reasons[:2])}",
                                    entry_type="warn",
                                )
                                await self.emit("subtask_progress", {
                                    "subtask_id": subtask.id,
                                    "stage": "builder_rollback",
                                    "message": "Builder regression guard restored the latest stable preview.",
                                    "restored_files": restored_files[:12],
                                })
                            result["success"] = False
                            result["error"] = regression_msg
                            subtask.error = regression_msg
                            await self.emit("subtask_progress", {
                                "subtask_id": subtask.id,
                                "stage": "builder_regression_guard_failed",
                                "message": regression_msg,
                            })
                        elif preview_ready_payload:
                            should_promote_stable = True
                            if task_classifier.wants_multi_page(plan.goal):
                                pending_builder_siblings = any(
                                    st.agent_type == "builder"
                                    and st.id != subtask.id
                                    and st.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED)
                                    for st in plan.subtasks
                                )
                                aggregate_gate = self._evaluate_multi_page_artifacts(plan.goal)
                                should_promote_stable = (not pending_builder_siblings) and bool(aggregate_gate.get("ok"))
                                if not should_promote_stable:
                                    self._append_ne_activity(
                                        subtask.id,
                                        "已生成预览，但暂不覆盖稳定版本；等待所有页面完成并通过多页总闸。",
                                        entry_type="info",
                                    )
                            if should_promote_stable:
                                promoted = self._promote_stable_preview(
                                    subtask_id=subtask.id,
                                    stage="builder_quality_pass",
                                    files_created=files_created,
                                    preview_artifact=preview_html,
                                )
                                if promoted:
                                    preview_ready_payload.update(promoted)
                            await self.emit("preview_ready", preview_ready_payload)

            if subtask.agent_type == "polisher" and result.get("success"):
                gap_errors = self._polisher_gap_gate_errors()
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "polisher_gap_gate",
                    "ok": not bool(gap_errors),
                    "issues": gap_errors[:4],
                })
                if gap_errors:
                    gap_msg = (
                        "Polisher deterministic gap gate failed: unfinished visual placeholders remain. "
                        f"Issues: {gap_errors[:3]}"
                    )
                    restored_files = self._restore_output_from_stable_preview()
                    if restored_files:
                        self._append_ne_activity(
                            subtask.id,
                            f"Polisher gap gate 失败，已回滚到稳定版本，恢复 {len(restored_files)} 个文件。",
                            entry_type="warn",
                        )
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "polisher_rollback",
                            "message": "Polisher gap gate failed; restored the latest stable builder output.",
                            "restored_files": restored_files[:12],
                        })
                    result["success"] = False
                    result["error"] = gap_msg
                    subtask.error = gap_msg
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "polisher_gap_gate_failed",
                        "message": gap_msg,
                    })
                else:
                    post_polish_quality = self._validate_builder_quality(
                        files_created,
                        result.get("output", ""),
                        goal=plan.goal,
                    )
                    pre_polish_score = int((pre_polish_quality or {}).get("score", 0) or 0)
                    post_polish_score = int(post_polish_quality.get("score", 0) or 0)
                    score_drop = max(0, pre_polish_score - post_polish_score)
                    regression_reasons: List[str] = []
                    changed_html_routes: List[str] = []
                    if task_classifier.wants_multi_page(plan.goal):
                        for item in files_created:
                            path = Path(str(item))
                            if path.suffix.lower() not in (".html", ".htm"):
                                continue
                            if self._is_internal_non_deliverable_html(path) or self._is_task_preview_fallback_html(path):
                                continue
                            try:
                                rel_path = self._normalize_preview_path(str(path.resolve().relative_to(OUTPUT_DIR.resolve())))
                            except Exception:
                                rel_path = path.name
                            if rel_path and rel_path not in changed_html_routes:
                                changed_html_routes.append(rel_path)
                        route_limit = 2
                        gap_routes = [
                            str(entry).lstrip("- ").split(":", 1)[0].strip()
                            for entry in self._current_polisher_visual_gap_entries()
                            if ":" in str(entry)
                        ]
                        if gap_routes:
                            route_limit = max(2, min(4, len(set(gap_routes))))
                        if len(self._current_run_html_artifacts()) > 3 and len(changed_html_routes) > route_limit:
                            regression_reasons.append(
                                f"polisher touched {len(changed_html_routes)} HTML routes (limit {route_limit})"
                            )
                    if not post_polish_quality.get("pass"):
                        regression_reasons.append(
                            "post-polish quality gate failed: "
                            + ", ".join(str(item) for item in (post_polish_quality.get("errors", []) or [])[:3])
                        )
                    if (
                        (pre_polish_quality or {}).get("pass")
                        and post_polish_quality.get("pass")
                        and score_drop >= 8
                    ):
                        regression_reasons.append(
                            f"quality score dropped from {pre_polish_score} to {post_polish_score}"
                        )
                    if pre_polish_signal_snapshot:
                        try:
                            post_polish_signal_snapshot = self._capture_route_signal_snapshot()
                            regression_reasons.extend(
                                self._polisher_signal_regression_reasons(
                                    pre_polish_signal_snapshot,
                                    post_polish_signal_snapshot,
                                    changed_html_routes,
                                )
                            )
                        except Exception:
                            pass
                    if regression_reasons:
                        restored_files = self._restore_output_from_stable_preview()
                        regression_msg = (
                            "Polisher regression guard failed: "
                            + "; ".join(regression_reasons[:3])
                        )
                        if restored_files:
                            regression_msg += ". Restored the latest stable builder output."
                            self._append_ne_activity(
                                subtask.id,
                                f"Polisher 触发回归保护，已回滚到稳定版本，原因：{'; '.join(regression_reasons[:2])}",
                                entry_type="warn",
                            )
                            await self.emit("subtask_progress", {
                                "subtask_id": subtask.id,
                                "stage": "polisher_rollback",
                                "message": "Polisher regression guard restored the latest stable builder output.",
                                "restored_files": restored_files[:12],
                            })
                        result["success"] = False
                        result["error"] = regression_msg
                        subtask.error = regression_msg
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "polisher_regression_guard_failed",
                            "message": regression_msg,
                        })

            if subtask.agent_type == "reviewer" and result.get("success"):
                task_type = str(getattr(task_profile, "task_type", "") or "website")
                browser_calls = int(tool_call_stats.get("browser", 0) or 0)
                browser_use_calls = int(tool_call_stats.get("browser_use", 0) or 0)
                qa_visual_calls = browser_calls + browser_use_calls
                desktop_qa_ready = desktop_qa_usable
                qa_browser_use_available = bool(result.get("qa_browser_use_available"))
                upstream_builder_tasks = self._transitive_upstream_builders(plan, subtask)
                upstream_builder_ids = {str(st.id) for st in upstream_builder_tasks}
                if qa_visual_calls < 1 and not desktop_qa_ready:
                    reviewer_msg = (
                        "Reviewer visual gate failed: neither browser nor browser_use was used. "
                        "Reviewer must navigate the preview and capture interaction evidence, or consume a successful desktop QA session."
                    )
                    result["success"] = False
                    result["error"] = reviewer_msg
                    subtask.error = reviewer_msg
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "reviewer_visual_gate_failed",
                        "message": reviewer_msg,
                    })
                elif task_type == "game" and qa_browser_use_available and browser_use_calls < 1 and not desktop_qa_ready:
                    reviewer_msg = (
                        "Reviewer gameplay gate failed: browser_use was available but not used. "
                        "Game reviews must record a real gameplay session before approval."
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
                        task_type,
                        browser_actions,
                        plan.goal,
                    )
                    reviewer_gate: Dict[str, Any] = {"ok": True, "preview_url": None, "errors": [], "warnings": [], "smoke": {"status": "skipped", "reason": "not_applicable"}}
                    multi_page_gate: Dict[str, Any] = {"ok": True, "errors": []}
                    reviewer_gate_ok = True
                    reviewer_gate_errors: List[str] = []
                    reviewer_gate_warnings: List[str] = []
                    reviewer_smoke: Dict[str, Any] = reviewer_gate.get("smoke", {}) or {}
                    if upstream_builder_ids:
                        reviewer_gate = await self._run_reviewer_visual_gate(plan.goal)
                        reviewer_gate_ok = bool(reviewer_gate.get("ok"))
                        reviewer_gate_errors = reviewer_gate.get("errors", []) or []
                        reviewer_gate_warnings = reviewer_gate.get("warnings", []) or []
                        reviewer_smoke = reviewer_gate.get("smoke", {}) or {}
                        reviewer_visual = reviewer_gate.get("visual_regression", {}) if isinstance(reviewer_gate.get("visual_regression"), dict) else {}
                        multi_page_gate = self._evaluate_multi_page_artifacts(plan.goal)
                        if not multi_page_gate.get("ok"):
                            reviewer_gate_ok = False
                            reviewer_gate_errors = reviewer_gate_errors + (multi_page_gate.get("errors", []) or [])
                        self._persist_visual_regression_artifact(subtask.id, "Reviewer", reviewer_gate)
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "reviewer_visual_gate",
                            "ok": reviewer_gate_ok,
                            "preview_url": reviewer_gate.get("preview_url"),
                            "errors": reviewer_gate_errors[:4],
                            "warnings": reviewer_gate_warnings[:4],
                            "smoke_status": reviewer_smoke.get("status", "skipped"),
                            "visual_status": reviewer_visual.get("status", "skipped"),
                            "visual_summary": reviewer_visual.get("summary", ""),
                        })
                    can_force_reject = bool(upstream_builder_ids)
                    if can_force_reject and (interaction_error or not reviewer_gate_ok):
                        forced_output = self._build_reviewer_forced_rejection(
                            interaction_error=interaction_error or "",
                            preview_gate=reviewer_gate,
                            multi_page_gate=multi_page_gate,
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
                full_output, synthesized_tags, remaining_tags = self._materialize_analyst_handoff(
                    plan,
                    full_output,
                    visited_urls=visited_urls,
                )
                result["output"] = full_output
                if synthesized_tags:
                    logger.info(
                        "Analyst handoff synthesized missing sections: %s",
                        synthesized_tags[:8],
                    )
                if remaining_tags:
                    logger.info(
                        f"Analyst handoff soft-warning: missing sections {remaining_tags[:6]}; "
                        f"downstream will use raw analyst output as fallback."
                    )
                if browser_calls < 1 or len(visited_urls) < 2:
                    analyst_msg = (
                        "Analyst research incomplete: must browse at least 2 live reference URLs and list them in the report."
                    )
                    result["success"] = False
                    result["error"] = analyst_msg
                    subtask.error = analyst_msg
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "analyst_reference_gate_failed",
                        "message": analyst_msg,
                        "browser_calls": browser_calls,
                        "visited_urls": visited_urls[:4],
                    })

            # ── Reviewer rejection → trigger builder re-run (ALL modes) ──
            if (
                subtask.agent_type == "reviewer"
                and result.get("success")
            ):
                reviewer_output = (result.get("output") or "").strip()
                reviewer_verdict = self._parse_reviewer_verdict(reviewer_output)
                if reviewer_verdict == "UNKNOWN":
                    missing_verdict_msg = (
                        "Reviewer output incomplete: missing explicit APPROVED/REJECTED verdict. "
                        "Retry the reviewer instead of soft-approving the artifact."
                    )
                    result["success"] = False
                    result["error"] = missing_verdict_msg
                    result["retryable"] = True
                    subtask.error = missing_verdict_msg
                    self._append_ne_activity(
                        subtask.id,
                        "Reviewer 未给出明确 verdict，已阻止软通过并要求重试。",
                        entry_type="warn",
                    )
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "reviewer_verdict_missing",
                        "message": missing_verdict_msg,
                        "output_len": len(reviewer_output),
                    })
                reviewer_rejected = reviewer_verdict == "REJECTED"
                rejection_details = ""

                if reviewer_rejected:
                    # Extract improvement suggestions from reviewer output
                    rejection_details = self._format_reviewer_rework_brief(reviewer_output)
                    self._append_ne_activity(
                        subtask.id,
                        f"Reviewer 退回说明：{rejection_details[:600]}",
                        entry_type="warn",
                    )
                    task_type = str(getattr(task_profile, "task_type", "") or "website")
                    upstream_builder_ids = {
                        str(st.id) for st in self._transitive_upstream_builders(plan, subtask)
                    }
                    builders_to_requeue = [
                        st for st in plan.subtasks
                        if st.agent_type == "builder"
                        and st.status == TaskStatus.COMPLETED
                        and str(st.id) in upstream_builder_ids
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
                        restored_files = self._restore_output_from_stable_preview()
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "reviewer_rejection",
                            "message": rejection_msg,
                            "rejection_round": self._reviewer_requeues,
                            "max_rejections": max_rejections,
                            "restored_files": restored_files[:12],
                        })
                        if restored_files:
                            self._append_ne_activity(
                                subtask.id,
                                f"Reviewer 退回前已恢复最近稳定版本，共 {len(restored_files)} 个文件，避免 builder 在退化产物上继续重写。",
                                entry_type="info",
                            )
                        for builder_task in eligible_builders:
                            builder_task.status = TaskStatus.PENDING
                            builder_task.retries += 1
                            builder_task.error = (
                                f"Reviewer rejected (round {self._reviewer_requeues}): "
                                f"{rejection_details[:600]}"
                            )
                            builder_task.description = self._merge_reviewer_rework_into_builder_description(
                                builder_task.description,
                                rejection_details,
                                round_num=self._reviewer_requeues,
                                max_rejections=max_rejections,
                            )
                            self._append_ne_activity(
                                builder_task.id,
                                f"收到 Reviewer 退回 brief（第 {self._reviewer_requeues} 轮）：{rejection_details[:600]}",
                                entry_type="warn",
                            )
                        # Reset reviewer so it re-checks after builder fixes
                        subtask.status = TaskStatus.PENDING
                        subtask.output = ""
                        subtask.completed_at = 0
                        result["success"] = False
                        result["error"] = rejection_msg
                        result["requeue_requested"] = True
                        result["requeue_subtasks"] = [st.id for st in eligible_builders] + [subtask.id]
                        subtask.error = ""
                        logger.info(
                            "Reviewer rejected — re-running builders %s",
                            [st.id for st in eligible_builders],
                        )
                    else:
                        reason = (
                            f"rejection budget reached ({self._reviewer_requeues}/{max_rejections})"
                            if self._reviewer_requeues >= max_rejections
                            else "builder retries exhausted"
                        )
                        if task_type == "website":
                            restored_files = self._restore_output_from_stable_preview()
                            fail_msg = (
                                f"Reviewer rejected the website and no further builder requeue is possible ({reason}). "
                                f"The run is blocked until the quality issues are fixed. Notes: {rejection_details[:400]}"
                            )
                            logger.warning(fail_msg)
                            result["success"] = False
                            result["retryable"] = False
                            result["error"] = fail_msg
                            result["output"] = reviewer_output or rejection_details
                            subtask.status = TaskStatus.FAILED
                            subtask.output = reviewer_output or rejection_details
                            subtask.error = fail_msg
                            subtask.completed_at = time.time()
                            await self._sync_ne_status(
                                subtask.id,
                                "failed",
                                output_summary=self._humanize_output_summary(
                                    subtask.agent_type,
                                    reviewer_output or rejection_details,
                                    False,
                                ),
                                error_message=fail_msg,
                            )
                            await self.emit("subtask_progress", {
                                "subtask_id": subtask.id,
                                "stage": "reviewer_rejection_no_retry",
                                "message": fail_msg,
                                "restored_files": restored_files[:12],
                            })
                            if restored_files:
                                self._append_ne_activity(
                                    subtask.id,
                                    f"Reviewer 终止交付前已恢复最近稳定版本，共 {len(restored_files)} 个文件。",
                                    entry_type="info",
                                )
                            self._append_ne_activity(
                                subtask.id,
                                f"Reviewer 最终阻断交付：{rejection_details[:500]}",
                                entry_type="error",
                            )
                        else:
                            # Non-website fallback retains the historical soft-pass behavior.
                            soft_pass_msg = (
                                f"Reviewer flagged issues but builder retries exhausted ({reason}). "
                                f"Delivering current artifact as-is. Notes: {rejection_details[:400]}"
                            )
                            logger.warning(soft_pass_msg)
                            result["success"] = True
                            result["retryable"] = False
                            result["output"] = soft_pass_msg
                            subtask.status = TaskStatus.COMPLETED
                            subtask.output = soft_pass_msg
                            subtask.error = ""
                            subtask.completed_at = time.time()
                            await self._sync_ne_status(subtask.id, "passed", output_summary=soft_pass_msg[:200])
                            await self.emit("subtask_progress", {
                                "subtask_id": subtask.id,
                                "stage": "reviewer_soft_pass",
                                "message": soft_pass_msg,
                            })
                            self._append_ne_activity(
                                subtask.id,
                                f"Reviewer 提出改进建议，但 builder 重试已用完。以当前产物继续交付。说明：{rejection_details[:400]}",
                                entry_type="warn",
                            )

            if subtask.agent_type == "tester" and result.get("success"):
                task_type = str(getattr(task_profile, "task_type", "") or "website")
                tester_browser_calls = int(tool_call_stats.get("browser", 0) or 0)
                tester_browser_use_calls = int(tool_call_stats.get("browser_use", 0) or 0)
                tester_visual_calls = tester_browser_calls + tester_browser_use_calls
                desktop_qa_ready = desktop_qa_usable
                qa_browser_use_available = bool(result.get("qa_browser_use_available"))
                if tester_visual_calls < 1 and not desktop_qa_ready:
                    tester_msg = (
                        "Tester visual gate failed: neither browser nor browser_use was used. "
                        "Tester must navigate the preview and capture interaction evidence, or consume a successful desktop QA session."
                    )
                    result["success"] = False
                    result["error"] = tester_msg
                    subtask.error = tester_msg
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "tester_visual_gate_failed",
                        "message": tester_msg,
                    })
                elif task_type == "game" and qa_browser_use_available and tester_browser_use_calls < 1 and not desktop_qa_ready:
                    tester_msg = (
                        "Tester gameplay gate failed: browser_use was available but not used. "
                        "Game tests must record a real gameplay session before passing."
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
                        task_type,
                        browser_actions,
                        plan.goal,
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
                tester_gate = await self._run_tester_visual_gate(plan.goal)
                gate_ok = bool(tester_gate.get("ok"))
                gate_errors = tester_gate.get("errors", []) or []
                gate_warnings = tester_gate.get("warnings", []) or []
                smoke = tester_gate.get("smoke", {}) or {}
                visual_regression = tester_gate.get("visual_regression", {}) if isinstance(tester_gate.get("visual_regression"), dict) else {}
                multi_page_gate = self._evaluate_multi_page_artifacts(plan.goal)
                if not multi_page_gate.get("ok"):
                    gate_ok = False
                    gate_errors = gate_errors + (multi_page_gate.get("errors", []) or [])
                self._persist_visual_regression_artifact(subtask.id, "Tester", tester_gate)
                smoke_status = smoke.get("status", "skipped")
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "tester_visual_gate",
                    "ok": gate_ok,
                    "preview_url": tester_gate.get("preview_url"),
                    "errors": gate_errors[:4],
                    "warnings": gate_warnings[:4],
                    "smoke_status": smoke_status,
                    "visual_status": visual_regression.get("status", "skipped"),
                    "visual_summary": visual_regression.get("summary", ""),
                })
                gate_note = (
                    f"Deterministic visual gate {'passed' if gate_ok else 'failed'}; "
                    f"smoke={smoke_status}; preview={tester_gate.get('preview_url') or 'n/a'}."
                )
                visual_status = str(visual_regression.get("status", "") or "").strip().lower()
                visual_summary = str(visual_regression.get("summary", "") or "").strip()
                visual_suggestions = visual_regression.get("suggestions") if isinstance(visual_regression.get("suggestions"), list) else []
                if visual_status in {"warn", "fail"} and visual_summary:
                    gate_note += f" Visual regression {visual_status}: {visual_summary}"
                if visual_suggestions:
                    gate_note += f" Suggestions: {visual_suggestions[:2]}"
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

            # F4-2: Token estimation fallback when streaming API doesn't return usage
            if total_tokens <= 0 and full_output:
                import re as _re_tok
                cjk_chars = len(_re_tok.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', full_output))
                ascii_chars = len(full_output) - cjk_chars
                # Heuristic: CJK ~1.5 tokens/char, ASCII ~0.75 tokens/char
                estimated_completion = int(cjk_chars * 1.5 + ascii_chars * 0.75)
                # Estimate prompt tokens from input size
                input_text = str(subtask.description or "")
                cjk_input = len(_re_tok.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', input_text))
                ascii_input = len(input_text) - cjk_input
                estimated_prompt = int(cjk_input * 1.5 + ascii_input * 0.75)
                prompt_tokens = estimated_prompt
                completion_tokens = estimated_completion
                total_tokens = prompt_tokens + completion_tokens
                logger.info(
                    "Token fallback estimation for %s: prompt≈%d completion≈%d total≈%d (output_chars=%d)",
                    subtask.agent_type, prompt_tokens, completion_tokens, total_tokens, len(full_output),
                )

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
                    assigned_model=result.get("assigned_model", result.get("model", "")),
                    assigned_provider=result.get("assigned_provider", ""),
                    tokens_used=total_tokens,
                    cost=estimated_cost,
                    loaded_skills=loaded_skills,
                    reference_urls=reference_urls,
                )
                await self._emit_ne_progress(subtask.id, progress=100, phase="complete")
            else:
                # Mark the attempt as failed immediately so sibling retries cannot leave this
                # node stuck in a stale "running" state long enough for the watchdog to
                # misclassify it as a timeout. Retry handling will reopen it to "running".
                human_summary = self._humanize_output_summary(
                    subtask.agent_type, result.get("error", ""), False
                )
                self._append_ne_activity(
                    subtask.id,
                    f"执行失败：{str(result.get('error', '') or human_summary)[:320]}",
                    entry_type="error",
                )
                await self._sync_ne_status(
                    subtask.id,
                    "failed",
                    output_summary=human_summary,
                    error_message=result.get("error", ""),
                    assigned_model=result.get("assigned_model", result.get("model", "")),
                    assigned_provider=result.get("assigned_provider", ""),
                    tokens_used=total_tokens,
                    cost=estimated_cost,
                    loaded_skills=loaded_skills,
                    reference_urls=reference_urls,
                    phase="attempt_failed",
                )
                self._update_ne_failure_details(
                    subtask.id,
                    output_summary=human_summary,
                    error_message=result.get("error", ""),
                    tokens_used=total_tokens,
                    cost=estimated_cost,
                    persist_error_message=True,
                )
            logger.info(
                f"Subtask done: id={subtask.id} agent={subtask.agent_type} success={bool(result.get('success'))} "
                f"output_len={len(full_output)} files={len(files_created)} retries={subtask.retries} "
                f"error={(str(result.get('error', ''))[:180] if not result.get('success') else '')}"
            )
            subtask.ended_at = time.time()

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
            human_summary = self._humanize_output_summary(subtask.agent_type, str(e), False)
            await self._sync_ne_status(
                subtask.id,
                "failed",
                output_summary=human_summary,
                error_message=str(e),
                phase="attempt_failed",
            )
            self._update_ne_failure_details(
                subtask.id,
                output_summary=human_summary,
                error_message=str(e),
                persist_error_message=True,
            )
            subtask.ended_at = time.time()
            return {"success": False, "output": "", "error": str(e), "tool_results": []}

    def _extract_and_save_code(
        self,
        output: str,
        subtask_id: str,
        *,
        allow_root_index_copy: bool = True,
        multi_page_required: bool = False,
        allowed_html_targets: Optional[List[str]] = None,
        allow_multi_page_raw_html_fallback: bool = False,
        allow_named_shared_asset_blocks: bool = True,
        is_retry: bool = False,
    ) -> list:
        """Extract code from AI output and save as files.
        Handles: markdown code blocks, raw HTML without fences.
        Returns list of file paths created."""
        files = []
        task_dir = OUTPUT_DIR / f"task_{subtask_id}"
        task_dir.mkdir(parents=True, exist_ok=True)
        allowed_html_targets_ordered = [
            Path(str(item).strip()).name
            for item in (allowed_html_targets or [])
            if str(item).strip()
        ]
        allowed_html_target_set = set(allowed_html_targets_ordered)
        strict_multi_page_named_output = multi_page_required and len(allowed_html_targets_ordered) > 1
        skipped_html_blocks: List[tuple[str, str]] = []

        seen_langs = {}  # track duplicates
        matched_spans: List[tuple[int, int]] = []

        def _validated_html_candidate(code: str, label: str) -> Optional[str]:
            normalized = self._normalize_html_artifact(code)
            integrity = inspect_html_integrity(normalized)
            if integrity.get("ok", True):
                return normalized
            issues = "; ".join(str(item) for item in (integrity.get("errors") or [])[:4])
            logger.warning(f"Skipping invalid extracted HTML block {label}: {issues}")
            return None

        def _handle_code_block(header_text: str, raw_code: str) -> None:
            lang, rel_path = self._parse_code_block_header(header_text)
            code = raw_code.strip()
            if not code or len(code) < 10:
                return  # skip tiny snippets
            if (
                not allow_named_shared_asset_blocks
                and rel_path is not None
                and rel_path.suffix.lower() not in (".html", ".htm")
            ):
                logger.info(f"Skipping extracted non-HTML block during locked builder repair: {rel_path.name}")
                return
            if strict_multi_page_named_output and rel_path is None and lang in ("css", "js", "javascript", "ts", "typescript"):
                logger.info(f"Skipping unnamed {lang} block for multi-page builder output")
                return
            if lang in ("html", "htm"):
                code = _validated_html_candidate(code, rel_path.as_posix() if rel_path is not None else header_text or "unnamed")
                if not code:
                    return
                if multi_page_required and rel_path is None:
                    logger.info("Skipping unnamed HTML block for multi-page builder output")
                    return
                if rel_path is not None and allowed_html_target_set and rel_path.name not in allowed_html_target_set:
                    logger.info(f"Skipping extracted HTML block outside assigned targets: {rel_path.name}")
                    skipped_entry = (rel_path.name, code)
                    if skipped_entry not in skipped_html_blocks:
                        skipped_html_blocks.append(skipped_entry)
                    return
                if rel_path is not None and rel_path.name == "index.html" and not allow_root_index_copy:
                    logger.info("Skipping extracted secondary builder index.html block")
                    return

            if rel_path is not None:
                try:
                    self._save_extracted_code_block(
                        task_dir=task_dir,
                        rel_path=rel_path,
                        code=code,
                        files=files,
                        allow_root_index_copy=allow_root_index_copy,
                        allowed_html_targets=allowed_html_target_set,
                        is_retry=is_retry,
                        merge_owner=f"builder-{subtask_id}",
                    )
                    return
                except Exception as e:
                    logger.error(f"Failed to save explicit code block {rel_path}: {e}")

            base_name = _LANG_FILENAME.get(lang, f"output.{lang}")
            count = seen_langs.get(base_name, 0)
            seen_langs[base_name] = count + 1
            if count > 0:
                stem, ext = os.path.splitext(base_name)
                base_name = f"{stem}_{count}{ext}"

            if not allow_named_shared_asset_blocks and lang not in ("html", "htm"):
                logger.info(f"Skipping extracted fallback non-HTML block during locked builder repair: {base_name}")
                return
            if lang in ("html", "htm") and allowed_html_target_set and base_name not in allowed_html_target_set:
                logger.info(f"Skipping extracted fallback HTML block outside assigned targets: {base_name}")
                return
            if lang in ("html", "htm") and base_name == "index.html" and not allow_root_index_copy and multi_page_required:
                logger.info("Skipping extracted fallback secondary builder index.html block")
                return

            filepath = task_dir / base_name
            try:
                task_type = getattr(self, "_current_task_type", "website")
                code = postprocess_generated_text(code, filename=base_name, task_type=task_type)
                filepath.write_text(code, encoding="utf-8")
                files.append(str(filepath))
                logger.info(f"Saved code to {filepath} ({len(code)} chars)")

                if allow_root_index_copy and lang in ("html", "htm") and base_name == "index.html":
                    root_copy = OUTPUT_DIR / "index.html"
                    root_copy.write_text(code, encoding="utf-8")
                    files.append(str(root_copy))
                    logger.info(f"Also saved to root: {root_copy}")
            except Exception as e:
                logger.error(f"Failed to save {filepath}: {e}")

        def _recover_lossy_fenced_blocks() -> None:
            if "```" not in output:
                return

            saved_names = {Path(item).name for item in files}
            saved_html_names = {
                Path(item).name
                for item in files
                if Path(item).suffix.lower() in (".html", ".htm")
            }
            outstanding_targets = {
                name for name in allowed_html_target_set
                if name not in saved_names
            }
            expected_html_count = len(allowed_html_target_set)
            if multi_page_required:
                expected_html_count = max(expected_html_count, 2)
            if expected_html_count <= 0:
                expected_html_count = 1
            if len(saved_html_names) >= expected_html_count and (not multi_page_required or not outstanding_targets):
                return

            current_header: Optional[str] = None
            current_lines: List[str] = []
            recovered_any = False

            def _flush_current() -> None:
                nonlocal current_header, current_lines, recovered_any
                if current_header is None:
                    return
                lang, rel_path = self._parse_code_block_header(current_header)
                if rel_path is not None and rel_path.name in saved_names:
                    current_header = None
                    current_lines = []
                    return
                before = len(files)
                _handle_code_block(current_header, "\n".join(current_lines))
                if len(files) > before:
                    recovered_any = True
                    saved_names.update(Path(item).name for item in files)
                current_header = None
                current_lines = []

            for raw_line in output.splitlines():
                stripped = raw_line.lstrip()
                if stripped.startswith("```"):
                    header = stripped[3:].strip()
                    if current_header is not None:
                        _flush_current()
                        if not header:
                            continue
                    if header:
                        current_header = header
                        current_lines = []
                    continue
                if current_header is not None:
                    current_lines.append(raw_line)

            if current_header is not None:
                _flush_current()

            if recovered_any:
                logger.info("Recovered lossy fenced code blocks during extraction")

        # Strategy 1: Extract markdown code blocks
        for match in _CODE_BLOCK_RE.finditer(output):
            matched_spans.append((match.start(), match.end()))
            _handle_code_block(match.group(1) or "", match.group(2))

        # Strategy 1b: Recover the final code block when the model forgot to close the fence.
        fence_markers = [m.start() for m in re.finditer(r"```", output)]
        if len(fence_markers) % 2 == 1:
            last_open = fence_markers[-1]
            already_handled = any(start <= last_open < end for start, end in matched_spans)
            if not already_handled:
                remainder = output[last_open + 3 :]
                if "\n" in remainder:
                    header_text, raw_code = remainder.split("\n", 1)
                    logger.info("Recovering unterminated code block during extraction")
                    _handle_code_block(header_text, raw_code)

        _recover_lossy_fenced_blocks()
        self._remap_skipped_html_blocks(
            task_dir=task_dir,
            files=files,
            skipped_html_blocks=skipped_html_blocks,
            allowed_html_targets_ordered=allowed_html_targets_ordered,
            allow_root_index_copy=allow_root_index_copy,
        )

        # Strategy 2: If no HTML code block found, look for raw HTML in output
        has_html = any(f.endswith('.html') for f in files)
        if not has_html and multi_page_required and not allow_multi_page_raw_html_fallback:
            logger.info("Skipping raw HTML fallback extraction for multi-page builder output without named files")
        elif not has_html and strict_multi_page_named_output:
            logger.info(
                "Skipping raw HTML fallback because multi-page builder requires explicit named HTML targets: %s",
                allowed_html_targets_ordered[:8],
            )
        elif not has_html and ('<!DOCTYPE' in output or '<html' in output):
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
                html_code = _validated_html_candidate(html_code, "raw_html_fallback")
                if html_code and len(html_code) > 50:
                    fallback_target_name = "index.html"
                    if allowed_html_targets_ordered:
                        if len(allowed_html_targets_ordered) == 1:
                            fallback_target_name = allowed_html_targets_ordered[0]
                        elif "index.html" in allowed_html_target_set:
                            fallback_target_name = "index.html"
                        else:
                            logger.info(
                                "Skipping raw HTML fallback because assigned HTML target is ambiguous: %s",
                                allowed_html_targets_ordered[:8],
                            )
                            return files
                    filepath = task_dir / fallback_target_name
                    try:
                        task_type = getattr(self, "_current_task_type", "website")
                        html_code = postprocess_generated_text(
                            html_code,
                            filename=fallback_target_name,
                            task_type=task_type,
                        )
                        filepath.write_text(html_code, encoding='utf-8')
                        files.append(str(filepath))
                        # Also save to root
                        if allow_root_index_copy and fallback_target_name == "index.html":
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

    async def _run_preview_visual_gate(self, *, run_smoke: bool, gate_name: str, goal: str = "") -> Dict[str, Any]:
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
        result = await validate_preview(
            preview_url,
            run_smoke=run_smoke,
            visual_scope=self._visual_baseline_scope(goal),
        )
        result["task_id"] = task_id
        return result

    async def _run_tester_visual_gate(self, goal: str = "") -> Dict[str, Any]:
        """
        Deterministic post-check for tester stage so visual validation is not purely model-text based.
        """
        return await self._run_preview_visual_gate(
            run_smoke=self._configured_tester_smoke(),
            gate_name="Tester",
            goal=goal,
        )

    async def _run_reviewer_visual_gate(self, goal: str = "") -> Dict[str, Any]:
        """
        Deterministic post-check for reviewer stage so blank/white previews become structured rejections.
        """
        return await self._run_preview_visual_gate(
            run_smoke=self._configured_reviewer_smoke(),
            gate_name="Reviewer",
            goal=goal,
        )

    def _desktop_qa_session_enabled(self) -> bool:
        raw = os.getenv("EVERMIND_DESKTOP_QA_SESSION_ENABLED", "1")
        try:
            cfg = getattr(self.ai_bridge, "config", {}) or {}
            raw = cfg.get("desktop_qa_session_enabled", raw)
        except Exception:
            pass
        return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}

    def _desktop_qa_timeout_seconds(self) -> float:
        raw = os.getenv("EVERMIND_DESKTOP_QA_SESSION_TIMEOUT_SEC", "18")
        try:
            cfg = getattr(self.ai_bridge, "config", {}) or {}
            raw = cfg.get("desktop_qa_session_timeout_sec", raw)
        except Exception:
            pass
        try:
            return max(3.0, min(float(raw), 60.0))
        except Exception:
            return 18.0

    def _resolve_current_preview_url(self, goal: str = "", *, allow_stable_fallback: bool = True) -> str:
        task_id, html_file = latest_preview_artifact(OUTPUT_DIR)
        if (not html_file or not html_file.exists()) and allow_stable_fallback:
            stable = self._stable_preview_path if self._stable_preview_path and self._stable_preview_path.exists() else None
            if stable is not None:
                task_id, html_file = "current_run_stable", stable
        if not html_file or not html_file.exists():
            return ""
        try:
            return build_preview_url_for_file(html_file, output_dir=OUTPUT_DIR)
        except Exception:
            logger.warning("Failed to build preview URL for desktop QA session (task_id=%s)", task_id)
            return ""

    def _desktop_qa_session_artifacts(self, node_execution_id: str, session_id: str) -> List[Dict[str, Any]]:
        if not node_execution_id or not session_id:
            return []
        try:
            artifacts = get_artifact_store().list_artifacts(node_execution_id=node_execution_id) or []
        except Exception:
            return []
        return [
            artifact for artifact in artifacts
            if isinstance(artifact, dict)
            and str(artifact.get("artifact_type") or "").strip() in {"qa_session_capture", "qa_session_video", "qa_session_log"}
            and str((artifact.get("metadata") or {}).get("source") or "").strip() == "desktop_qa_session"
            and str((artifact.get("metadata") or {}).get("session_id") or "").strip() == session_id
        ]

    def _desktop_qa_action_events(self, session_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(session_payload, dict):
            return []
        actions = session_payload.get("actions") if isinstance(session_payload.get("actions"), list) else []
        normalized_actions: List[Dict[str, Any]] = []
        for item in actions:
            if not isinstance(item, dict):
                continue
            normalized_actions.append({
                "plugin": "desktop_qa_session",
                "action": str(item.get("action") or "").strip().lower(),
                "subaction": str(item.get("subaction") or item.get("intent") or "").strip().lower(),
                "ok": bool(item.get("ok")),
                "url": str(item.get("url") or session_payload.get("previewUrl") or session_payload.get("preview_url") or "").strip(),
                "target": str(item.get("target") or "").strip(),
                "observation": str(item.get("observation") or "").strip(),
                "state_hash": str(item.get("state_hash") or item.get("stateHash") or "").strip(),
                "previous_state_hash": str(item.get("previous_state_hash") or item.get("previousStateHash") or "").strip(),
                "state_changed": bool(item.get("state_changed", False)),
                "keys_count": int(item.get("keys_count", 0) or 0),
                "scroll_y": int(item.get("scroll_y", 0) or 0),
                "viewport_height": int(item.get("viewport_height", 0) or 0),
                "page_height": int(item.get("page_height", 0) or 0),
                "is_scrollable": item.get("is_scrollable"),
                "at_bottom": bool(item.get("at_bottom", False)),
                "at_top": bool(item.get("at_top", False)),
                "can_scroll_more": item.get("can_scroll_more"),
                "console_error_count": int(item.get("console_error_count", 0) or 0),
                "page_error_count": int(item.get("page_error_count", 0) or 0),
                "failed_request_count": int(item.get("failed_request_count", 0) or 0),
                "recent_failed_requests": [
                    {
                        "url": str(req.get("url") or "").strip(),
                        "error": str(req.get("error") or "").strip(),
                        "resource_type": str(req.get("resource_type") or "").strip().lower(),
                    }
                    for req in (item.get("recent_failed_requests") or [])[:3]
                    if isinstance(req, dict)
                ],
                "capture_path": str(item.get("capture_path") or "").strip(),
                "recording_path": str(item.get("recording_path") or session_payload.get("videoPath") or session_payload.get("video_path") or "").strip(),
            })
        return normalized_actions

    def _desktop_qa_prefetch_summary(self, session_payload: Dict[str, Any], task_type: str) -> str:
        if not isinstance(session_payload, dict):
            return ""
        actions = self._desktop_qa_action_events(session_payload)
        if not actions:
            return ""
        frame_count = len(session_payload.get("frames") or []) if isinstance(session_payload.get("frames"), list) else 0
        console_errors = len(session_payload.get("consoleErrors") or session_payload.get("console_errors") or [])
        page_errors = len(session_payload.get("pageErrors") or session_payload.get("page_errors") or [])
        failed_requests = len(session_payload.get("failedRequests") or session_payload.get("failed_requests") or [])
        rrweb_events = int(session_payload.get("rrwebEventCount") or session_payload.get("rrweb_event_count") or 0)
        timelapse_frames = int(session_payload.get("timelapseFrameCount") or session_payload.get("timelapse_frame_count") or 0)
        action_lines = []
        for item in actions[:8]:
            fragment = str(item.get("action") or "action")
            if item.get("target"):
                fragment += f" target={item.get('target')}"
            if item.get("keys_count"):
                fragment += f" keys={item.get('keys_count')}"
            if item.get("state_hash"):
                fragment += f" state={str(item.get('state_hash'))[:8]}"
            if item.get("state_changed"):
                fragment += " changed=1"
            action_lines.append(f"- {fragment}")
        return (
            "[Desktop QA Session Evidence]\n"
            "A desktop Electron QA session already ran against the internal Evermind preview window before your verdict.\n"
            f"- task_type: {task_type or 'unknown'}\n"
            f"- preview_url: {str(session_payload.get('previewUrl') or session_payload.get('preview_url') or '')}\n"
            f"- session_status: {str(session_payload.get('status') or '')}\n"
            f"- summary: {str(session_payload.get('summary') or '')}\n"
            f"- frame_count: {frame_count}\n"
            f"- console_errors: {console_errors}\n"
            f"- page_errors: {page_errors}\n"
            f"- failed_requests: {failed_requests}\n"
            + (f"- rrweb_dom_events: {rrweb_events} (full DOM-level session recording available)\n" if rrweb_events > 0 else "")
            + (f"- timelapse_frames: {timelapse_frames} (high-frequency screenshot capture at 500ms intervals)\n" if timelapse_frames > 0 else "")
            + ("- recording: available\n" if str(session_payload.get("videoPath") or session_payload.get("video_path") or "").strip() else "")
            + "Use this evidence as the primary gameplay record. Only call browser/browser_use if you need extra confirmation or if the desktop session is clearly insufficient.\n"
            + ("Observed action sequence:\n" + "\n".join(action_lines) + "\n" if action_lines else "")
        ).strip()

    def _desktop_qa_session_usable(self, session_payload: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(session_payload, dict) or not session_payload:
            return False
        actions = session_payload.get("actions") if isinstance(session_payload.get("actions"), list) else []
        artifacts = session_payload.get("artifacts") if isinstance(session_payload.get("artifacts"), list) else []
        frames = session_payload.get("frames") if isinstance(session_payload.get("frames"), list) else []
        console_errors = session_payload.get("consoleErrors") or session_payload.get("console_errors") or []
        page_errors = session_payload.get("pageErrors") or session_payload.get("page_errors") or []
        failed_requests = session_payload.get("failedRequests") or session_payload.get("failed_requests") or []
        summary = str(session_payload.get("summary") or "").strip()
        return bool(
            summary
            and (
                actions
                or frames
                or artifacts
                or console_errors
                or page_errors
                or failed_requests
            )
        )

    async def _maybe_collect_desktop_qa_session(
        self,
        subtask: SubTask,
        goal: str,
        task_type: str,
    ) -> Dict[str, Any]:
        if subtask.agent_type not in {"reviewer", "tester"}:
            return {}
        if task_type != "game" or not self._desktop_qa_session_enabled():
            return {}

        run_id = str((self._canonical_ctx or {}).get("run_id") or "").strip()
        node_execution_id = str(self._ne_id_for_subtask(subtask.id) or "").strip()
        preview_url = self._resolve_current_preview_url(goal, allow_stable_fallback=True)
        if not run_id or not node_execution_id or not preview_url:
            return {}

        session_id = f"desktop-qa-{node_execution_id}-{int(time.time() * 1000)}"
        timeout_sec = self._desktop_qa_timeout_seconds()
        await self.emit("subtask_progress", {
            "subtask_id": subtask.id,
            "stage": "qa_session_requested",
            "agent": subtask.agent_type,
            "task_type": task_type,
            "run_id": run_id,
            "node_execution_id": node_execution_id,
            "preview_url": preview_url,
            "scenario": f"{subtask.agent_type}_gameplay",
            "session_id": session_id,
            "duration_ms": int(timeout_sec * 1000),
        })
        self._append_ne_activity(
            subtask.id,
            "已请求桌面端 QA Preview Session，在 Evermind 内部预览窗口录制游戏交互证据。",
            entry_type="sys",
        )

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            artifacts = self._desktop_qa_session_artifacts(node_execution_id, session_id)
            log_artifact = next(
                (
                    artifact for artifact in artifacts
                    if str(artifact.get("artifact_type") or "").strip() == "qa_session_log"
                ),
                None,
            )
            if log_artifact:
                payload: Dict[str, Any] = {}
                try:
                    raw_content = str(log_artifact.get("content") or "").strip()
                    parsed = json.loads(raw_content) if raw_content else {}
                    if isinstance(parsed, dict):
                        payload = parsed
                except Exception as exc:
                    logger.warning("Failed to parse desktop QA session log for %s: %s", subtask.id, exc)
                payload["session_id"] = session_id
                payload["preview_url"] = str(payload.get("previewUrl") or payload.get("preview_url") or preview_url).strip()
                payload["artifacts"] = artifacts
                payload["actions"] = self._desktop_qa_action_events(payload)
                payload["summary"] = self._desktop_qa_prefetch_summary(payload, task_type)
                payload["usable"] = self._desktop_qa_session_usable(payload)
                payload["ok"] = bool(payload.get("actions")) and str(payload.get("status") or "").strip().lower() in {"completed", "incomplete"}
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "qa_session_completed",
                    "session_id": session_id,
                    "ok": bool(payload.get("ok")),
                    "capture_count": sum(1 for item in artifacts if str(item.get("artifact_type") or "") == "qa_session_capture"),
                    "video_count": sum(1 for item in artifacts if str(item.get("artifact_type") or "") == "qa_session_video"),
                })
                return payload
            await asyncio.sleep(0.5)

        await self.emit("subtask_progress", {
            "subtask_id": subtask.id,
            "stage": "qa_session_fallback",
            "session_id": session_id,
            "message": f"Desktop QA session did not return in {timeout_sec:.1f}s; falling back to the browser chain.",
        })
        return {}

    async def _refresh_visual_baseline_for_success(self, goal: str, report: Optional[Dict[str, Any]] = None) -> None:
        task_id, html_file = latest_preview_artifact(OUTPUT_DIR)
        if not html_file:
            return
        preview_url = build_preview_url_for_file(html_file, output_dir=OUTPUT_DIR)
        baseline = await update_visual_baseline(
            preview_url,
            self._visual_baseline_scope(goal),
            metadata={
                "goal": str(goal or "")[:500],
                "task_id": str((self._canonical_ctx or {}).get("task_id") or ""),
                "run_id": str((self._canonical_ctx or {}).get("run_id") or ""),
                "preview_task_id": str(task_id or ""),
            },
        )
        if baseline.get("updated"):
            logger.info(
                "Visual baseline refreshed: scope=%s page=%s captures=%s",
                baseline.get("scope_key"),
                baseline.get("page_key"),
                len(baseline.get("captures") or []),
            )
            if isinstance(report, dict):
                report["visual_baseline"] = {
                    "updated": True,
                    "scope_key": baseline.get("scope_key"),
                    "page_key": baseline.get("page_key"),
                    "captures": baseline.get("captures", []),
                }

    async def _emit_final_preview(self, report_success: Optional[bool] = None):
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
            self._hydrate_stable_preview_from_disk()

            # Prefer run-local HTML artifacts to avoid opening stale previews from previous runs.
            cutoff = max(self._run_started_at - 2.0, 0.0)
            run_local: List[str] = []
            for html in OUTPUT_DIR.rglob("*"):
                if not html.is_file() or html.suffix.lower() not in (".html", ".htm"):
                    continue
                if is_partial_html_artifact(html):
                    continue
                try:
                    rel = html.resolve().relative_to(OUTPUT_DIR.resolve())
                    if rel.parts and rel.parts[0] == "_stable_previews":
                        continue
                except Exception:
                    pass
                try:
                    mtime = html.stat().st_mtime
                except Exception:
                    continue
                if mtime >= cutoff:
                    run_local.append(self._normalize_generated_path(str(html)))
            run_deliverables = [
                self._normalize_generated_path(str(path))
                for path in self._current_run_deliverable_artifacts()
            ]

            if report_success is False:
                self._restore_root_index_from_stable_preview()
                if self._stable_preview_path and self._stable_preview_path.exists():
                    preview_url = build_preview_url_for_file(self._stable_preview_path, output_dir=OUTPUT_DIR)
                    logger.info(f"Final preview_ready (stable after failed run): {preview_url}")
                    await self.emit("preview_ready", {
                        "preview_url": preview_url,
                        "files": list(self._stable_preview_files),
                        "output_dir": str(OUTPUT_DIR),
                        "final": True,
                        "stable_preview": True,
                        "stage": self._stable_preview_stage,
                    })
                else:
                    root_index = OUTPUT_DIR / "index.html"
                    if root_index.exists():
                        root_validation = validate_html_file(root_index)
                        preview_url = build_preview_url_for_file(root_index, output_dir=OUTPUT_DIR)
                        if preview_url:
                            live_files = run_local or [self._normalize_generated_path(str(root_index))]
                            logger.info(f"Final preview_ready (live fallback after failed run): {preview_url}")
                            await self.emit("preview_ready", {
                                "preview_url": preview_url,
                                "files": live_files,
                                "output_dir": str(OUTPUT_DIR),
                                "final": True,
                                "stable_preview": False,
                                "stage": "failed_run_live_fallback",
                                "validation_ok": bool(root_validation.get("ok")),
                                "validation_errors": (root_validation.get("errors") or [])[:4],
                            })
                            return
                    logger.info("Final preview skipped: run failed and no stable preview snapshot exists")
                return

            if report_success and run_local:
                latest_html = self._select_preview_artifact_for_files(run_local)
                if latest_html is not None:
                    promoted = self._promote_stable_preview(
                        subtask_id="final",
                        stage="final_success",
                        files_created=run_deliverables or run_local,
                        preview_artifact=latest_html,
                    )
                    if promoted:
                        logger.info("Captured final stable preview from run-local artifacts")

            if self._stable_preview_path and self._stable_preview_path.exists():
                preview_url = build_preview_url_for_file(self._stable_preview_path, output_dir=OUTPUT_DIR)
                logger.info(f"Final preview_ready (stable): {preview_url}")
                await self.emit("preview_ready", {
                    "preview_url": preview_url,
                    "files": list(self._stable_preview_files),
                    "output_dir": str(OUTPUT_DIR),
                    "final": True,
                    "stable_preview": True,
                    "stage": self._stable_preview_stage,
                })
                return

            if not run_local:
                logger.info("Final preview scan: no run-local HTML artifacts found")
                return

            latest_html = self._select_preview_artifact_for_files(run_local)
            if latest_html is None:
                logger.info("Final preview scan: no eligible preview artifact selected")
                return
            preview_url = build_preview_url_for_file(latest_html, output_dir=OUTPUT_DIR)
            logger.info(f"Final preview_ready: {preview_url}")
            await self.emit("preview_ready", {
                "preview_url": preview_url,
                "files": (run_deliverables or run_local)[:20],
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
        retry_error = (subtask.error or "Unknown error").strip() or "Unknown error"
        retry_error_lower = retry_error.lower()
        if (
            subtask.agent_type == "polisher"
            and any(
                marker in retry_error_lower
                for marker in (
                    "polisher pre-write timeout",
                    "polisher loop guard",
                    "polisher deterministic gap gate failed",
                    "polisher regression guard failed",
                    "gap gate failed",
                    "execution timeout",  # catch generic execution timeouts too
                )
            )
        ):
            restored_files = self._restore_output_from_stable_preview()
            # Treat structural polisher failures as safe fallbacks, not "skips":
            # the node really failed, but the run can continue with builder output.
            fallback_msg = "Polisher 未能安全落盘，已回退到最近稳定版本并继续后续节点。"
            self._append_ne_activity(
                subtask.id,
                f"{fallback_msg} 已恢复 {len(restored_files)} 个文件。" if restored_files
                else f"{fallback_msg} 直接使用 builder 产物继续。",
                entry_type="warn",
            )
            subtask.status = TaskStatus.COMPLETED
            subtask.output = fallback_msg
            subtask.error = ""
            subtask.completed_at = time.time()
            await self._sync_ne_status(
                subtask.id,
                "failed",
                output_summary=fallback_msg[:200],
                error_message=fallback_msg[:300],
                phase="soft_failed",
            )
            await self.emit("subtask_progress", {
                "subtask_id": subtask.id,
                "stage": "polisher_soft_failed",
                "message": "Polisher failed safely; reverted to builder output and continued to reviewer.",
                "restored_files": restored_files[:12] if restored_files else [],
            })
            return True
        if subtask.retries >= subtask.max_retries:
            logger.warning(f"Subtask {subtask.id} exceeded max retries ({subtask.max_retries})")
            subtask.status = TaskStatus.FAILED
            if subtask.agent_type == "builder":
                restored_files = self._restore_output_from_stable_preview()
                cleaned_files = [] if restored_files else self._cleanup_internal_builder_artifacts()
                if restored_files:
                    self._append_ne_activity(
                        subtask.id,
                        f"Builder 最终失败后已恢复最近稳定版本，共 {len(restored_files)} 个文件，避免 live 输出停留在退化稿。",
                        entry_type="warn",
                    )
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "stable_preview_restored",
                        "message": "Builder exhausted retries; restored the latest stable preview into the live output.",
                        "restored_files": restored_files[:12],
                    })
                elif cleaned_files:
                    self._append_ne_activity(
                        subtask.id,
                        f"Builder 最终失败后已清理 {len(cleaned_files)} 个内部骨架/半成品文件，避免继续暴露空白页面。",
                        entry_type="warn",
                    )
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "failed_builder_artifacts_cleaned",
                        "message": "Removed bootstrap / partial builder artifacts after the final failure.",
                        "cleaned_files": cleaned_files[:12],
                    })
            # §FIX: Only sync NE to failed after ALL retries exhausted
            err_msg = f"All {subtask.max_retries} retries exhausted. Last error: {(subtask.error or 'Unknown')[:200]}"
            await self._sync_ne_status(subtask.id, "failed", error_message=err_msg)
            await self.emit("subtask_progress", {
                "subtask_id": subtask.id,
                "stage": "error",
                "message": err_msg,
            })
            return False

        # ── Guard: token truncation loop is non-retryable for non-builder/non-polisher nodes ──
        # When spritesheet or imagegen hits repeated finish=length, retrying with the same
        # model will just trigger the same truncation. Fail fast instead of wasting API calls.
        NON_RETRYABLE_TRUNCATION_AGENTS = {"spritesheet", "imagegen", "assetimport", "scribe", "bgremove"}
        if (
            subtask.agent_type in NON_RETRYABLE_TRUNCATION_AGENTS
            and any(
                marker in retry_error_lower
                for marker in (
                    "model hit token limit during tool calls",
                    "breaking truncated tool loop",
                    "length_truncation_break",
                )
            )
        ):
            logger.warning(
                "Non-retryable token truncation for %s subtask %s: %s",
                subtask.agent_type,
                subtask.id,
                retry_error[:200],
            )
            subtask.status = TaskStatus.FAILED
            subtask.error = f"Non-retryable: model token limit prevents completing {subtask.agent_type} tool calls. {retry_error[:200]}"
            await self._sync_ne_status(
                subtask.id,
                "failed",
                error_message=subtask.error[:300],
            )
            return False

        subtask.retries += 1
        plan.total_retries += 1
        subtask.status = TaskStatus.RETRYING
        # Reset monotonic progress high-water mark for this subtask
        if hasattr(self, "_progress_high_water"):
            self._progress_high_water.pop(subtask.id, None)
        logger.info(
            f"Retrying subtask: id={subtask.id} agent={subtask.agent_type} "
            f"attempt={subtask.retries}/{subtask.max_retries} model={model} error={retry_error[:200]}"
        )

        # §FIX: Reset NE to running so frontend shows "retrying" not "failed"
        await self._sync_ne_status(
            subtask.id, "running",
            input_summary=f"Retry {subtask.retries}/{subtask.max_retries}: {subtask.description[:200]}",
            progress=12,
            phase="retrying",
            retry_count=subtask.retries,
            reset_started_at=True,
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
            builder_error_text = str(subtask.error or "")
            builder_error_lower = builder_error_text.lower()
            is_timeout = "timeout" in builder_error_lower
            nav_repair_only = self._builder_nav_repair_only(builder_error_text)
            partial_output = getattr(subtask, 'last_partial_output', '') or ''
            salvaged_timeout_files: List[str] = []
            if is_timeout and len(partial_output) > 100:
                salvaged_timeout_files = self._salvage_builder_partial_output(plan, subtask, partial_output)
                if salvaged_timeout_files:
                    self._append_ne_activity(
                        subtask.id,
                        f"重试前已从超时内容回收 {len(salvaged_timeout_files)} 个 HTML 文件，避免从空白骨架重来。",
                        entry_type="info",
                    )
            should_restore_stable = (
                (is_timeout and not salvaged_timeout_files)
                or (
                    not nav_repair_only
                    and any(
                        marker in builder_error_lower
                        for marker in (
                            "reviewer rejected",
                            "quality gate failed",
                            "multi-page delivery incomplete",
                            "index.html does not expose enough working local navigation links",
                            "index.html references missing local pages",
                            "broken local navigation links detected",
                        )
                    )
                )
            )
            if should_restore_stable:
                restored_files = self._restore_output_from_stable_preview()
                cleaned_files = [] if restored_files else self._cleanup_internal_builder_artifacts()
                if restored_files:
                    self._append_ne_activity(
                        subtask.id,
                        f"重试前已恢复最近稳定版本，共 {len(restored_files)} 个文件，避免在退化稿上继续重写。",
                        entry_type="info",
                    )
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "stable_preview_restored",
                        "message": "Restored the latest stable preview into the live output before builder retry.",
                        "restored_files": restored_files[:12],
                    })
                elif cleaned_files:
                    self._append_ne_activity(
                        subtask.id,
                        f"重试前已清理 {len(cleaned_files)} 个内部骨架/半成品文件，避免继续在混合输出上重写。",
                        entry_type="info",
                    )
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "failed_builder_artifacts_cleaned",
                        "message": "Cleaned bootstrap / partial builder artifacts before retry because no stable snapshot was available.",
                        "cleaned_files": cleaned_files[:12],
                    })
            assigned_targets = self._builder_bootstrap_targets(plan, subtask) if self._is_multi_page_website_goal(plan.goal) else []
            aggregate_gate = self._evaluate_multi_page_artifacts(plan.goal) if assigned_targets else {}
            missing_targets = self._missing_builder_targets(
                plan,
                subtask,
                aggregate_gate.get("observed_html_files", []) or aggregate_gate.get("html_files", []) or [],
            ) if assigned_targets else []
            repair_targets = self._builder_repair_targets(plan, subtask, aggregate_gate) if assigned_targets else []
            error_targets = self._builder_error_repair_targets(builder_error_text) if assigned_targets else []
            direct_multifile_retry = self._builder_execution_direct_multifile_mode(plan, subtask, model)
            direct_retry_targets = repair_targets[:] if repair_targets else (missing_targets[:] if missing_targets else assigned_targets[:])
            for name in error_targets:
                if name in assigned_targets and name not in direct_retry_targets:
                    direct_retry_targets.append(name)
            if self._builder_retry_should_keep_full_scope(
                builder_error_text,
                aggregate_gate,
                assigned_targets,
                direct_retry_targets,
            ):
                direct_retry_targets = assigned_targets[:]
            if self._is_multi_page_website_goal(plan.goal) and direct_retry_targets:
                direct_multifile_retry = True
            direct_override_line = (
                f"{BUILDER_TARGET_OVERRIDE_MARKER} {', '.join(direct_retry_targets)}\n"
                if direct_retry_targets else
                ""
            )
            retry_target_scope_note = (
                (
                    BUILDER_DIRECT_MULTIFILE_MARKER + "\n"
                    if self._is_multi_page_website_goal(plan.goal) and direct_retry_targets
                    else ""
                )
                + direct_override_line
                + (
                    "- Prioritize ONLY these currently missing / invalid assigned files first: "
                    + ", ".join(direct_retry_targets[:12])
                    + "\n"
                )
                + "- Do NOT invent any new HTML filename outside the override list during this retry.\n"
                if direct_retry_targets else
                ""
            )
            assignment_note = ""
            if assigned_targets:
                assignment_note = (
                    f"- Your assigned HTML filenames: {', '.join(assigned_targets)}\n"
                    + (
                        f"- Missing or invalid pages still owned by you: {', '.join(missing_targets)}\n"
                        if missing_targets else
                        ""
                    )
                    + "- Do NOT write any HTML filename outside this list.\n"
                )
            multi_page_retry_note = ""
            if self._is_multi_page_website_goal(plan.goal):
                multi_page_retry_note = (
                    "- Do NOT write index_part*.html or page fragments\n"
                    "- Repair or create real linked pages such as index.html plus named secondary routes\n"
                    "- task_*/index.html preview fallbacks do NOT count; save owned pages directly as named route files under /tmp/evermind_output/\n"
                    f"{assignment_note}"
                )
            if nav_repair_only:
                gate = aggregate_gate
                preserve_pages = [item for item in (gate.get("html_files") or []) if item != "index.html"]
                missing_nav = gate.get("missing_nav_targets") or []
                unlinked_pages = gate.get("unlinked_secondary_pages") or []
                enhanced_input = "".join([
                    f"{subtask.description}\n\n",
                    "⚠️ NAVIGATION REPAIR ONLY.\n",
                    f"{BUILDER_NAV_REPAIR_ONLY_MARKER}\n",
                    f"{BUILDER_TARGET_OVERRIDE_MARKER} index.html\n",
                    "The required pages already exist, but index.html / shared navigation does not point to the real files.\n",
                    (f"- The ONLY valid local HTML routes for this site are: {', '.join(assigned_targets)}\n" if assigned_targets else ""),
                    (f"- Preserve these existing pages exactly as they are: {', '.join(str(item) for item in preserve_pages[:12])}\n" if preserve_pages else ""),
                    (f"- Broken navigation targets to remove or replace: {', '.join(str(item) for item in missing_nav[:12])}\n" if missing_nav else ""),
                    (f"- Real pages that must become reachable from navigation: {', '.join(str(item) for item in unlinked_pages[:12])}\n" if unlinked_pages else ""),
                    "- First inspect /tmp/evermind_output/ with file_ops list/read.\n",
                    "- Patch only index.html and shared navigation so every link points to a real existing named page.\n",
                    "- Remove or rewrite any href that points to a non-assigned local HTML filename; use an existing assigned page or a same-page #anchor instead.\n",
                    "- Output ONLY a single fenced ```html index.html ...``` block for the repaired homepage.\n",
                    "- Do NOT write styles.css, app.js, or any secondary HTML page during this retry.\n",
                    "- Do NOT return prose, explanations, summaries, or planning text before/after the code block.\n",
                    "- Do NOT rewrite or delete the secondary pages.\n",
                    "- Do NOT reduce the page count or invent new slugs when suitable pages already exist.\n",
                    "- Keep the current visual design, structure, and strongest sections intact.\n",
                    multi_page_retry_note,
                    "- Save the repaired homepage/navigation via file_ops write.\n",
                ])
            elif (
                direct_multifile_retry
                and self._is_multi_page_website_goal(plan.goal)
                and any(
                    marker in builder_error_lower
                    for marker in (
                        "quality gate failed",
                        "timeout",
                        "too thin / stub-like",
                        "index.html does not expose enough working local navigation links",
                        "index.html references missing local pages",
                        "broken local navigation links detected",
                        "multi-page delivery incomplete",
                        "did not finish its assigned html pages",
                        "invalid or corrupted html pages detected",
                        "missing root index.html",
                        "html target not assigned",
                        "only builder 1 may write",
                        "did not save any real named html page",
                    )
                )
            ):
                preserved_pages = [
                    str(item)
                    for item in (aggregate_gate.get("html_files") or [])
                    if str(item or "").strip() and str(item) not in direct_retry_targets
                ]
                recovered_line = (
                    "- Pages already recovered from the timed-out response: "
                    + ", ".join(Path(path).name for path in salvaged_timeout_files[:12])
                    + "\n"
                    if salvaged_timeout_files else
                    ""
                )
                preserve_line = (
                    "- Preserve these existing good pages as-is unless one of them is explicitly listed below: "
                    + ", ".join(preserved_pages[:12])
                    + "\n"
                    if preserved_pages else
                    ""
                )
                direct_targets_line = (
                    f"- Repair or create ONLY these files now: {', '.join(direct_retry_targets)}\n"
                    if direct_retry_targets else
                    "- Repair or create the remaining assigned files now.\n"
                )
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    + (
                        "⚠️ PREVIOUS ATTEMPT TIMED OUT AFTER PARTIAL MULTI-PAGE DELIVERY.\n"
                        if is_timeout else
                        "⚠️ MULTI-PAGE DELIVERY NEEDS TARGETED REPAIR.\n"
                    )
                    + "DIRECT MULTI-FILE DELIVERY ONLY.\n"
                    + direct_override_line
                    + "The previous attempt already created real pages. Do NOT restart the full site from zero.\n"
                    + (
                        "- The ONLY valid local HTML routes for this site are: "
                        + ", ".join(assigned_targets)
                        + "\n"
                        if assigned_targets else
                        ""
                    )
                    + recovered_line
                    + preserve_line
                    + direct_targets_line
                    + "- Do NOT use browser research, file_ops list, or file_ops read on this retry.\n"
                    + "- If a listed file already exists, patch and return the full corrected file instead of summarizing it.\n"
                    + "- Output ONLY fenced code blocks like ```html pricing.html ...``` for the exact override targets.\n"
                    + "- Do NOT return prose, planning notes, or status narration.\n"
                    + "- Remove or rewrite any href that points to a non-assigned local HTML filename; use an existing assigned page or a same-page #anchor instead.\n"
                    + "- If any existing page is clearly the wrong language, wrong destination, or wrong topic for the current goal, overwrite it instead of preserving contamination.\n"
                    + "- Keep the strongest existing design system, motion language, and copy density intact across the repaired files.\n"
                    + (
                        "- If index.html is listed, ensure shared navigation links every real page in the current output set.\n"
                        if "index.html" in direct_retry_targets else
                        ""
                    )
                    + f"{multi_page_retry_note}"
                )
            elif any(
                marker in builder_error_lower
                for marker in (
                    "content completeness failure",
                    "page has blank sections",
                    "low content fill",
                )
            ):
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ CONTENT COMPLETENESS FAILURE.\n"
                    "The previous artifact already contains useful structure. Preserve the strongest parts and repair the blank / low-fill areas only.\n"
                    f"{retry_target_scope_note}"
                    + (
                        "- First inspect the current output and continue from that artifact rather than restarting from zero.\n"
                        if not self._builder_execution_direct_text_mode(plan, subtask)
                        else "- Continue from the current saved index.html and rewrite a stronger full file in place.\n"
                    )
                    + "- Remove decorative empty wrappers, empty sections, and dead placeholder bands.\n"
                    + "- If a container exists only for FX/HUD chrome, keep it lean and purposeful; do not multiply empty shells.\n"
                    + "- Fill every visible section with real headings, copy, controls, media, or gameplay UI so the page never renders blank bands.\n"
                    + "- Do NOT leave image slots empty; add topic-matched imagery or a finished CSS/SVG composition for every premium visual module.\n"
                    + "- Replace flat black/white background slabs with layered surfaces and coordinated accent tones instead of a one-color reset.\n"
                    + "- Do NOT replace a strong gameplay/site shell with a weaker simplified rewrite.\n"
                    + f"{multi_page_retry_note}"
                    + "- Save the corrected final files via file_ops write.\n"
                )
            elif (
                "html target not assigned" in builder_error_lower
                or "only builder 1 may write" in builder_error_lower
                or "did not save any real named html page" in builder_error_lower
            ):
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ HTML OWNERSHIP / OUTPUT CONTRACT VIOLATION.\n"
                    f"{retry_target_scope_note}"
                    f"{assignment_note}"
                    "First inspect the current output directory only if you need to confirm which assigned pages are still missing.\n"
                    "Do NOT touch another builder's files. Do NOT rewrite index.html unless it is explicitly assigned to you.\n"
                    "Immediately save the missing assigned pages via file_ops write using the exact assigned filenames.\n"
                    "Preserve any strong existing pages instead of restarting the whole site.\n"
                )
            elif is_timeout and subtask.retries == 1 and len(partial_output) > 100:
                # First retry after timeout WITH partial output: store it internally
                # but do NOT dump raw code into the task description (UI shows this).
                # Save partial to a temp file for the builder to read.
                partial_file = OUTPUT_DIR / "_partial_builder.html"
                try:
                    partial_file.parent.mkdir(parents=True, exist_ok=True)
                    partial_file.write_text(partial_output[:8000], encoding="utf-8")
                    saved_partial = True
                except Exception:
                    saved_partial = False
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ 上次执行因超时中断，但已有部分代码产出。\n"
                    + (f"部分产出已保存在 {partial_file}，请用 file_ops read 读取它。\n" if saved_partial else "")
                    + f"{retry_target_scope_note}"
                    + f"先用 file_ops list 检查 {OUTPUT_DIR}/ 现有文件，再用 file_ops read 读取已生成页面。\n"
                    "请在已有代码基础上继续完成，保留已经好的结构、视觉和页面，不要从零开始重写。\n"
                    "只修复超时前未完成或明显有问题的部分；除非文件结构已损坏，否则不要推翻重做。\n"
                    "如果现有页面明显属于错误的语言或错误的题材，就把它视为污染并覆盖掉，不要继续沿用。\n"
                    f"{multi_page_retry_note}"
                    f"完成后用 file_ops write 保存完整文件到 {OUTPUT_DIR}/index.html，并同步修复相关子页面。\n"
                )
            elif is_timeout and subtask.retries == 1:
                # First retry after timeout: preserve whatever already works instead of degrading quality.
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ PREVIOUS ATTEMPT TIMED OUT.\n"
                    f"{retry_target_scope_note}"
                    "You MUST preserve and improve the best existing artifact instead of restarting from scratch.\n"
                    "- First call file_ops list on /tmp/evermind_output/\n"
                    "- If index.html or linked pages exist, read them and keep the good sections/pages\n"
                    "- Patch only the missing, blank, broken, or low-quality areas\n"
                    "- If an existing page clearly belongs to the wrong language/topic for the current goal, treat it as contamination and overwrite it\n"
                    "- Do NOT compress scope, remove pages, or downgrade the design to a stub\n"
                    "- Keep topic-accurate imagery when it already works; fill missing media slots and avoid flat black/white-only backgrounds on the repaired pages\n"
                    "- Do NOT rewrite the product from zero unless the existing artifact is irreparably malformed\n"
                    f"{multi_page_retry_note}"
                    "- Save the corrected result back via file_ops write, including all required linked pages\n"
                )
            elif is_timeout and subtask.retries >= 2:
                # Later retries still preserve the good version; they should reduce wasted exploration, not product quality.
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ MULTIPLE TIMEOUTS DETECTED.\n"
                    f"{retry_target_scope_note}"
                    "Stay focused, but do NOT downgrade the product into a bare-bones page.\n"
                    "- Inspect /tmp/evermind_output/ first and preserve the strongest existing work\n"
                    "- If a page is obviously from the wrong language/topic, overwrite it instead of preserving contamination\n"
                    "- Keep the full requested page count / information architecture intact\n"
                    "- Fix the most critical missing sections, blank content, broken navigation, and runtime issues first\n"
                    "- Restore layered palette treatment and meaningful visual anchors; no flat black/white slabs or empty image slots on premium pages\n"
                    f"{multi_page_retry_note}"
                    "- Avoid exploratory tool loops; edit decisively and save final files via file_ops write\n"
                )
            else:
                # Non-timeout error retry
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    f"⚠️ PREVIOUS ERROR: {subtask.error}\n"
                    f"{retry_target_scope_note}"
                    "Inspect the current output first, preserve the good parts, and fix only the failing issues.\n"
                    "If any existing page is obviously the wrong language or wrong topic for the current goal, treat it as contamination and overwrite it.\n"
                    "Fill missing visual anchors, repair weak palette treatment, and keep topic-matched imagery instead of downgrading it.\n"
                    "Do not replace a strong result with a weaker simplified rewrite.\n"
                    f"{multi_page_retry_note}"
                    "Save the corrected files to /tmp/evermind_output/ via file_ops write.\n"
                )
        elif subtask.agent_type == "polisher":
            visual_gap_report = self._polisher_visual_gap_report()
            polisher_error = str(subtask.error or "")
            polisher_error_lower = polisher_error.lower()
            if "gap gate failed" in polisher_error_lower:
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ POLISHER GAP GATE FAILED.\n"
                    "The previous polish pass still left unfinished visual placeholders or broken local routes in the shipped artifact.\n"
                    + (visual_gap_report + "\n" if visual_gap_report else "")
                    + "Repair ONLY the unfinished visual/media modules now.\n"
                    + "- Replace blank image shells, fake placeholder copy, empty map/location panels, giant icon/pattern placeholders, dead local links, and gradient-only filler blocks with finished visuals.\n"
                    + "- Strengthen the same pages with richer motion, hover depth, and section rhythm while preserving the best existing structure.\n"
                    + "- Do NOT browse first. Start with file_ops write by your next meaningful action.\n"
                    + "- Do NOT downgrade any already-strong page into a simpler rewrite.\n"
                    + "- Save the corrected HTML/CSS/JS back to /tmp/evermind_output/.\n"
                )
            elif "timeout" in polisher_error_lower:
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ PREVIOUS POLISH PASS TIMED OUT.\n"
                    + (visual_gap_report + "\n" if visual_gap_report else "")
                    + "Preserve the strongest existing artifact and patch only the unfinished visuals, motion, and spacing issues.\n"
                    + "- Start from shared styles.css/app.js plus the affected HTML routes.\n"
                    + "- Do NOT restart the entire site from zero.\n"
                    + "- Replace empty media blocks, giant icon/pattern placeholder visuals, and broken local page routes first, then add premium motion refinement.\n"
                    + "- Save the corrected files via file_ops write.\n"
                )
            else:
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    f"⚠️ PREVIOUS ERROR: {subtask.error}\n"
                    + (visual_gap_report + "\n" if visual_gap_report else "")
                    + "Inspect the current output, preserve the strongest pages, and fix the failing polish issues only.\n"
                    + "- Prioritize unfinished media slots, placeholder copy, giant icon/pattern shells, dead local links, weak motion, and thin premium finish.\n"
                    + "- Use decisive file_ops write calls instead of read/browse loops.\n"
                    + "- Save the corrected HTML/CSS/JS to /tmp/evermind_output/.\n"
                )
        elif subtask.agent_type == "analyst":
            analyst_error = str(subtask.error or "").lower()
            if "timeout" in analyst_error:
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    "⚠️ PREVIOUS ATTEMPT TIMED OUT.\n"
                    "This retry should prioritize speed over breadth.\n"
                    "- You MAY use the browser tool on 1-2 fast-loading technical URLs (GitHub repos, official docs), "
                    "but DO NOT force browser research if your existing knowledge is sufficient.\n"
                    "- If you choose to browse, use ONLY fast-loading pages (GitHub raw files, API docs). "
                    "Skip any site that loads slowly or shows captcha.\n"
                    "- Prefer producing a concise implementation-ready report from your existing knowledge "
                    "over risking another timeout with extensive browsing.\n"
                    "- Be concise — output a compact report with all required XML tags.\n"
                )
            else:
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    f"⚠️ PREVIOUS ERROR: {subtask.error}\n"
                    "This retry MUST use the browser tool on at least 2 different source URLs.\n"
                    "Prioritize GitHub repos, source files, docs, tutorials, implementation guides, devlogs, and postmortems.\n"
                    "Use live product websites only as supporting visual evidence.\n"
                    "If one site blocks you, skip it and visit another.\n"
                    "Your final report MUST include ALL required XML tags for downstream handoff:\n"
                    "<reference_sites>, <design_direction>, <non_negotiables>, <deliverables_contract>, <risk_register>, "
                    "<builder_1_handoff>, <builder_2_handoff>, <reviewer_handoff>, "
                    "<tester_handoff>, <debugger_handoff>.\n"
                    "Do NOT finish after a single source. Do NOT return freeform prose only.\n"
                )
        else:
            if subtask.agent_type == "builder":
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    f"⚠️ PREVIOUS ATTEMPT FAILED (retry {subtask.retries}/{subtask.max_retries}):\n"
                    f"Error: {subtask.error}\n\n"
                    "MANDATORY FOR THIS RETRY:\n"
                    "- Your first successful tool action must be file_ops write that saves real HTML files.\n"
                    "- Save a compact but complete valid HTML skeleton first: close <head>, <style>, <body>, and </html> properly.\n"
                    "- Do NOT spend turns on browsing or directory probing before the HTML files are written.\n"
                    "- Avoid giant inline asset blobs, massive arrays, or oversized first-pass map data before the initial save succeeds.\n"
                    "- Preserve any strong existing pages; do not restart from zero or replace a stronger version with a weaker fallback.\n"
                    "- For multi-page work, save named HTML pages and keep navigation working across them.\n"
                )
            else:
                enhanced_input = (
                    f"{subtask.description}\n\n"
                    f"⚠️ PREVIOUS ATTEMPT FAILED (retry {subtask.retries}/{subtask.max_retries}):\n"
                    f"Error: {subtask.error}\n\n"
                    f"Please fix the issue and try again. Be more careful and faster this time."
                )

        agent_node = {"type": subtask.agent_type, "model": model, "id": f"auto_{subtask.id}_r{subtask.retries}",
                      "name": f"{subtask.agent_type.title()} #{subtask.id} (retry {subtask.retries})",
                      "retry_attempt": subtask.retries}
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
            # If retry budget is now exhausted, mark as FAILED so the caller
            # does not attempt another retry cycle.
            if subtask.retries >= subtask.max_retries:
                subtask.status = TaskStatus.FAILED
                err_msg = f"All {subtask.max_retries} retries exhausted. Last error: {(subtask.error or 'Unknown')[:200]}"
                await self._sync_ne_status(subtask.id, "failed", error_message=err_msg)
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "error",
                    "message": err_msg,
                })
            else:
                subtask.status = TaskStatus.PENDING
                subtask.completed_at = 0
            # Return False so the orchestrator main loop handles the next attempt
            # (if budget remains) instead of burning all retries recursively.
            return False

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
                downstream_ids = self._collect_transitive_downstream_ids(plan, [builder_task.id])

                if downstream_ids:
                    await self.emit("subtask_progress", {
                        "subtask_id": builder_task.id,
                        "stage": "requeue_downstream",
                        "message": f"Reset downstream tasks after test failure: {', '.join(downstream_ids)}",
                        "requeue_subtasks": downstream_ids,
                    })

                for st in plan.subtasks:
                    if st.id not in downstream_ids:
                        continue
                    st.status = TaskStatus.PENDING
                    st.output = ""
                    st.error = ""
                    st.completed_at = 0
                    self._reset_progress_tracking(st.id)
                    try:
                        await self._sync_ne_status(
                            st.id,
                            "queued",
                            progress=0,
                            phase="requeued",
                            reset_started_at=True,
                            error_message="",
                            output_summary="",
                        )
                    except Exception:
                        pass
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
        Parse reviewer output into APPROVED / REJECTED / UNKNOWN.
        UNKNOWN means the reviewer did not finish a usable verdict and must retry.
        """
        text = (output or "").strip()
        if not text:
            return "UNKNOWN"

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
        logger.warning(
            "Reviewer output has no clear verdict (no JSON, no APPROVED/REJECTED keyword). "
            "Marking as UNKNOWN so the reviewer retries instead of soft-approving. output_len=%d",
            len(text),
        )
        return "UNKNOWN"

    # ═══════════════════════════════════════════
    # Report
    # ═══════════════════════════════════════════
    def _build_report(self, plan: Plan, results: Dict) -> Dict:
        success_count = sum(1 for st in plan.subtasks if st.status == TaskStatus.COMPLETED)
        fail_count = sum(1 for st in plan.subtasks if st.status == TaskStatus.FAILED)
        blocked_count = sum(1 for st in plan.subtasks if st.status == TaskStatus.BLOCKED)
        terminal_count = sum(
            1
            for st in plan.subtasks
            if st.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED)
        )
        pending_count = len(plan.subtasks) - terminal_count
        root_failures = [
            st for st in plan.subtasks
            if st.status == TaskStatus.FAILED
            and "blocked by failed dependencies" not in str(st.error or "").lower()
        ]
        blocked_failures = [
            st for st in plan.subtasks
            if st.status == TaskStatus.BLOCKED
            or (
                st.status == TaskStatus.FAILED
                and "blocked by failed dependencies" in str(st.error or "").lower()
            )
        ]
        total_tokens = 0
        total_cost = 0.0
        for item in (results or {}).values():
            if not isinstance(item, dict):
                continue
            total_tokens += max(0, int(item.get("tokens_used", 0) or 0))
            total_cost += max(0.0, float(item.get("cost", 0.0) or 0.0))

        def _node_label(st: SubTask) -> str:
            return f"{st.agent_type} #{st.id}"

        remaining_risks: List[str] = []
        seen_risks = set()
        for st in root_failures + blocked_failures:
            detail = str(st.error or "").strip() or "No detailed error was captured."
            risk = f"{_node_label(st)}: {detail[:220]}"
            if risk not in seen_risks:
                remaining_risks.append(risk)
                seen_risks.add(risk)
            if len(remaining_risks) >= 6:
                break

        if fail_count == 0 and blocked_count == 0 and success_count == len(plan.subtasks):
            summary = f"Run completed successfully: {success_count}/{len(plan.subtasks)} subtasks passed."
            if plan.total_retries:
                summary += f" Retries used: {plan.total_retries}."
        else:
            summary_parts: List[str] = []
            if root_failures:
                root_labels = ", ".join(_node_label(st) for st in root_failures[:3])
                summary_parts.append(
                    f"Root failure in {len(root_failures)} subtask(s): {root_labels}."
                )
            if blocked_failures:
                blocked_labels = ", ".join(_node_label(st) for st in blocked_failures[:4])
                summary_parts.append(
                    f"Downstream blocked by dependencies: {blocked_labels}."
                )
            if pending_count:
                summary_parts.append(f"{pending_count} subtask(s) remained pending.")
            if not summary_parts:
                summary_parts.append(
                    f"Run ended without a clean pass: {success_count}/{len(plan.subtasks)} subtasks completed."
                )
            summary = " ".join(summary_parts)

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
            "summary": summary,
            "remaining_risks": remaining_risks,
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
