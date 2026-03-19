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
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

from plugins.base import PluginRegistry, get_default_plugins_for_node
import task_classifier
from preview_validation import (
    build_preview_url_for_file,
    latest_preview_artifact,
    validate_html_file,
    validate_preview,
)

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
        # Pro reviewer rejection guard: allow at most one builder requeue per run.
        self._pro_reviewer_requeues: int = 0

    async def emit(self, event_type: str, data: Dict):
        if self.on_event:
            await self.on_event({"type": event_type, **data})

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

    def _configured_subtask_timeout(self, agent_type: str) -> int:
        """
        Hard upper bound for one subtask execution to prevent silent 20+ minute hangs.
        """
        cfg = getattr(self.ai_bridge, "config", None)
        # Timeouts tuned per role: builder/analyst/reviewer need more time for browser research.
        defaults = {"builder": 900, "analyst": 480, "reviewer": 420}
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
        return max(60, min(value, 1800))

    def _configured_progress_heartbeat(self) -> int:
        raw = os.getenv("EVERMIND_PROGRESS_HEARTBEAT_SEC", "20")
        try:
            value = int(raw)
        except Exception:
            value = 20
        return max(5, min(value, 120))

    def _apply_retry_policy(self, plan: Plan):
        retries = self._configured_max_retries()
        for st in plan.subtasks:
            st.max_retries = retries
        plan.max_total_retries = max(plan.max_total_retries, retries * max(len(plan.subtasks), 1))

    def _builder_design_requirements(self, goal: str = "") -> str:
        """Return task-adaptive design requirements based on goal classification."""
        return task_classifier.design_requirements(goal)

    def _builder_task_description(self, goal: str) -> str:
        """Return task-adaptive builder task description."""
        return task_classifier.builder_task_description(goal)

    def _pro_builder_focus(self, goal: str) -> tuple[str, str]:
        """Return parallel builder focus for pro mode. Each builder creates an independent part.
        Both save to separate files; debugger merges at the end."""
        task_type = task_classifier.classify(goal).task_type
        mapping = {
            "website": (
                "YOUR JOB: Build the TOP HALF — header/nav, hero, trust badges, features grid. "
                "Save to /tmp/evermind_output/index_part1.html via file_ops write.",
                "YOUR JOB: Build the BOTTOM HALF — testimonials, pricing/CTA, footer. "
                "Save to /tmp/evermind_output/index_part2.html via file_ops write.",
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
        for f in files_created:
            if f.endswith(".html") or f.endswith(".htm"):
                html_path = f
                # Prefer root index for final preview quality gate
                if Path(f) == OUTPUT_DIR / "index.html":
                    break

        html = ""
        if html_path and Path(html_path).exists():
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
    async def run(self, goal: str, model: str = "gpt-5.4", conversation_history: Optional[List[Dict]] = None, difficulty: str = "standard") -> Dict:
        """
        Execute a user goal autonomously.
        Returns full execution report.
        conversation_history: optional list of recent {role, content} dicts for context continuity.
        difficulty: 'simple', 'standard', or 'pro' — controls number of workflow nodes.
        """
        self._cancel = False
        self._run_started_at = time.time()
        history = conversation_history or []
        difficulty = difficulty if difficulty in ("simple", "standard", "pro") else "standard"
        self.difficulty = difficulty
        self._pro_reviewer_requeues = 0

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
            plan = await self._plan(goal, model, conversation_history=history, difficulty=difficulty)
            self.active_plan = plan

            if not plan.subtasks:
                return {"success": False, "error": "Failed to create plan", "plan": None}

            await self.emit("plan_created", {
                "subtasks": [{"id": st.id, "agent": st.agent_type, "task": st.description,
                              "depends_on": st.depends_on} for st in plan.subtasks],
                "total": len(plan.subtasks)
            })

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

    def _planner_prompt_for_difficulty(self, difficulty: str) -> str:
        """Generate planner prompt tailored to difficulty level."""
        base_rules = (
            "You are a task planner. Output ONLY a valid JSON object, no other text.\n\n"
            "ABSOLUTE RULES (MUST follow):\n"
            "- Builder task MUST match the user's requested product type (website/game/dashboard/tool/presentation/creative) while producing a single self-contained index.html.\n"
            "- Builder must save final output to /tmp/evermind_output/index.html via file_ops write (or provide full HTML fallback).\n"
            "- You MUST NOT mention GitHub Pages, Netlify, Vercel, or any cloud deployment.\n"
            "- You MUST NOT ask the tester to check public URLs.\n\n"
        )

        if difficulty == "simple":
            return base_rules + (
                "Keep the plan to 2-3 subtasks (fast mode).\n"
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
                "Keep the plan to 7 subtasks (advanced mode with parallel builders).\n"
                "REQUIRED structure for pro mode:\n"
                "- #1 analyst → research and design brief\n"
                "- #2 builder → build core structure (depends on #1)\n"
                "- #3 builder → enhance and polish (depends on #1 for parallel, or #2 for sequential)\n"
                "- #4 reviewer → strict quality gate with browser screenshots (depends on #2, #3)\n"
                "- #5 deployer → confirm files and preview URL (depends on #2, #3)\n"
                "- #6 tester → full visual test (depends on #4, #5)\n"
                "- #7 debugger → fix issues from reviewer/tester (depends on #6)\n"
                "Pro mode MUST have 2 builders — they split the work for higher quality.\n\n"
                "Output format:\n"
                '{"subtasks": [\n'
                '  {"id": "1", "agent": "analyst", "task": "Research design references", "depends_on": []},\n'
                '  {"id": "2", "agent": "builder", "task": "Build core structure", "depends_on": ["1"]},\n'
                '  {"id": "3", "agent": "builder", "task": "Enhance and polish", "depends_on": ["2"]},\n'
                '  {"id": "4", "agent": "reviewer", "task": "Strict quality gate with browser screenshots", "depends_on": ["2","3"]},\n'
                '  {"id": "5", "agent": "deployer", "task": "Confirm files and preview URL", "depends_on": ["2","3"]},\n'
                '  {"id": "6", "agent": "tester", "task": "Full browser visual test", "depends_on": ["4","5"]},\n'
                '  {"id": "7", "agent": "debugger", "task": "Fix issues found by reviewer/tester", "depends_on": ["6"]}\n'
                "]}\n"
            )
        else:  # standard
            return base_rules + (
                "Keep the plan to 3-4 subtasks (balanced mode).\n"
                "Use 4 subtasks by default.\n\n"
                "Output format:\n"
                '{"subtasks": [\n'
                '  {"id": "1", "agent": "builder", "task": "...", "depends_on": []},\n'
                '  {"id": "2", "agent": "reviewer", "task": "Review the code quality", "depends_on": ["1"]},\n'
                '  {"id": "3", "agent": "deployer", "task": "Confirm files are saved and provide preview URL http://127.0.0.1:8765/preview/", "depends_on": ["1"]},\n'
                '  {"id": "4", "agent": "tester", "task": "Verify HTML validity and preview visual quality", "depends_on": ["3"]}\n'
                "]}\n"
            )

    def _fallback_plan_for_difficulty(self, goal: str, difficulty: str) -> List:
        """Generate fallback subtasks when AI planning fails."""
        profile = task_classifier.classify(goal)
        builder = SubTask(id="1", agent_type="builder", description=self._builder_task_description(goal))
        deployer_desc = "List generated files and provide local preview URL http://127.0.0.1:8765/preview/"
        tester_desc = profile.tester_hint

        if difficulty == "simple":
            return [
                builder,
                SubTask(id="2", agent_type="deployer", description=deployer_desc, depends_on=["1"]),
                SubTask(id="3", agent_type="tester", description=tester_desc, depends_on=["2"]),
            ]
        elif difficulty == "pro":
            focus_1, focus_2 = self._pro_builder_focus(goal)
            builder3_deps = ["1"]  # ALWAYS parallel
            return [
                SubTask(id="1", agent_type="analyst", description=(
                    f"ADVANCED MODE — {task_classifier.analyst_description(goal)}"
                ), depends_on=[]),
                SubTask(id="2", agent_type="builder", description=(
                    f"ADVANCED MODE — Use analyst notes and build.\n"
                    f"{self._builder_task_description(goal)}\n{focus_1}"
                ), depends_on=["1"]),
                SubTask(id="3", agent_type="builder", description=(
                    f"ADVANCED MODE — Use analyst notes and build.\n"
                    f"{self._builder_task_description(goal)}\n{focus_2}"
                ), depends_on=builder3_deps),
                SubTask(id="4", agent_type="reviewer", description=(
                    "STRICT QUALITY GATE (pro mode): "
                    "1) Use browser to navigate to http://127.0.0.1:8765/preview/ and capture screenshots; "
                    "2) Score: layout/color/typography/animation/responsive (each 1-10); "
                    "3) Output JSON with verdict: APPROVED or REJECTED. Be STRICT."
                ), depends_on=["2", "3"]),
                SubTask(id="5", agent_type="deployer", description=deployer_desc, depends_on=["2", "3"]),
                SubTask(id="6", agent_type="tester", description=tester_desc, depends_on=["4", "5"]),
                SubTask(id="7", agent_type="debugger", description=(
                    "Fix any issues from reviewer/tester. Read and fix /tmp/evermind_output/index.html."
                ), depends_on=["6"]),
            ]
        else:  # standard
            return [
                builder,
                SubTask(id="2", agent_type="reviewer", description="Review code quality", depends_on=["1"]),
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

        if difficulty == "pro":
            analyst_desc = ""
            for st in plan.subtasks:
                if st.agent_type == "analyst" and st.description.strip() and not analyst_desc:
                    analyst_desc = st.description.strip()

            if not analyst_desc:
                analyst_desc = task_classifier.analyst_description(goal)
            builder_desc = (
                f"ADVANCED MODE — Use analyst notes and build.\n"
                f"{self._builder_task_description(goal)}\n"
                f"Extra pro requirements for {profile.task_type}: advanced polish, "
                "smooth transitions, and attention to detail."
            )

            # ALL task types: 2 builders in pro mode — ALWAYS parallel
            # Both builders depend only on analyst (#1), creating vertical stacked layout
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
                    description=(
                        "STRICT QUALITY GATE (pro mode): "
                        "1) Use browser to navigate to http://127.0.0.1:8765/preview/ and capture screenshots; "
                        "2) Score: layout/color/typography/animation/responsive (each 1-10); "
                        "3) Output JSON with verdict: APPROVED (avg≥7) or REJECTED (avg<7 or any dim≤3). "
                        "If REJECTED, list specific improvements builder must fix. Be STRICT."
                    ),
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
                        + ("Merge index_part1.html and index_part2.html into index.html if needed. " if is_website else "")
                        + "Use file_ops read to check /tmp/evermind_output/index.html, "
                        "then file_ops write to save the corrected version. "
                        "If no issues were found, confirm everything is good."
                    ),
                    depends_on=["6"],
                ),
            ]
            return

        if difficulty == "standard":
            builder_desc = self._builder_task_description(goal)
            plan.subtasks = [
                SubTask(id="1", agent_type="builder", description=builder_desc, depends_on=[]),
                SubTask(id="2", agent_type="reviewer", description="Review code quality. Use browser to navigate to http://127.0.0.1:8765/preview/ and visually verify.", depends_on=["1"]),
                SubTask(id="3", agent_type="deployer", description="List generated files and provide local preview URL http://127.0.0.1:8765/preview/", depends_on=["1"]),
                SubTask(id="4", agent_type="tester", description=profile.tester_hint, depends_on=["3"]),
            ]
            return

        if difficulty == "simple":
            builder_desc = self._builder_task_description(goal)
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
            "prompt": self._planner_prompt_for_difficulty(difficulty),
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

            if not ready:
                # Mark downstream tasks as failed when any dependency has failed.
                blocked = []
                for st in plan.subtasks:
                    if st.id in completed or st.status == TaskStatus.CANCELLED:
                        continue
                    failed_deps = [dep for dep in st.depends_on if dep in failed]
                    if not failed_deps:
                        continue
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
                if blocked:
                    logger.warning(f"Blocked subtasks due to failed dependencies: {blocked}")
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
                    st.status = TaskStatus.FAILED
                    st.error = str(result) or "Unknown subtask exception"
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
        subtask.status = TaskStatus.IN_PROGRESS
        logger.info(
            f"Subtask start: id={subtask.id} agent={subtask.agent_type} retries={subtask.retries} "
            f"task={subtask.description[:140]}"
        )

        await self.emit("subtask_start", {
            "subtask_id": subtask.id,
            "agent": subtask.agent_type,
            "task": subtask.description[:200]
        })

        # Build context from dependency outputs
        context_parts = []
        for dep_id in subtask.depends_on:
            dep_result = prev_results.get(dep_id, {})
            dep_task = next((s for s in plan.subtasks if s.id == dep_id), None)
            if dep_task and dep_result.get("output"):
                context_parts.append(
                    f"[Result from {dep_task.agent_type} #{dep_id}]:\n{dep_result['output'][:MAX_DEP_CONTEXT_CHARS]}"
                )

        context = "\n\n".join(context_parts)

        # ── Inject execution context ──
        if subtask.agent_type == "builder":
            # Builder should focus on deterministic local file writes, not preview navigation.
            output_info = (
                f"\n\n[System Context]\n"
                f"Output directory: {str(OUTPUT_DIR)}\n"
                f"Files must be written to: {str(OUTPUT_DIR)}/\n"
                f"Use file_ops write for final HTML save.\n"
            )
        else:
            output_info = (
                f"\n\n[System Context]\n"
                f"Output directory: {str(OUTPUT_DIR)}\n"
                f"Preview server URL: http://127.0.0.1:{PREVIEW_PORT}/preview/\n"
                f"Files should be written to: {str(OUTPUT_DIR)}/\n"
            )
        full_input = f"{subtask.description}{output_info}\n\n{context}" if context else f"{subtask.description}{output_info}"

        # Create a virtual node for the agent
        agent_node = {
            "type": subtask.agent_type,
            "model": model,
            "id": f"auto_{subtask.id}",
            "name": f"{subtask.agent_type.title()} #{subtask.id}",
        }

        enabled = get_default_plugins_for_node(subtask.agent_type, config=getattr(self.ai_bridge, "config", None))
        plugins = [PluginRegistry.get(p) for p in enabled if PluginRegistry.get(p)]

        async def on_progress(data):
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
                await self.emit("subtask_progress", {
                    "subtask_id": subtask.id,
                    "stage": "waiting_ai",
                    "agent": subtask.agent_type,
                    "elapsed_sec": elapsed,
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
                    result = {"success": False, "output": "", "error": timeout_msg, "tool_results": []}
                    break

            if not isinstance(result, dict):
                raise TypeError(f"ai_bridge returned non-dict result: {type(result).__name__}")

            full_output = str(result.get("output", ""))
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

                logger.info(f"Files from subtask {subtask.id}: {files_created}")
                await self.emit("files_created", {
                    "subtask_id": subtask.id,
                    "files": files_created,
                    "output_dir": str(OUTPUT_DIR),
                })

                # ── Emit preview_ready if HTML files were created ──
                html_files = [f for f in files_created if f.endswith(('.html', '.htm'))]
                if html_files:
                    # Determine task folder name for preview URL
                    first_html = Path(html_files[0])
                    task_folder = first_html.parent.name if first_html.parent != OUTPUT_DIR else ""
                    html_name = first_html.name
                    if task_folder:
                        preview_url = f"http://127.0.0.1:{PREVIEW_PORT}/preview/{task_folder}/{html_name}"
                    else:
                        preview_url = f"http://127.0.0.1:{PREVIEW_PORT}/preview/{html_name}"
                    await self.emit("preview_ready", {
                        "subtask_id": subtask.id,
                        "preview_url": preview_url,
                        "files": files_created,
                        "output_dir": str(OUTPUT_DIR),
                    })
                    # Strong artifact gate: ensure preview target file exists and passes baseline checks.
                    preview_gate_result = validate_html_file(first_html)
                    await self.emit("subtask_progress", {
                        "subtask_id": subtask.id,
                        "stage": "preview_validation",
                        "ok": bool(preview_gate_result.get("ok")),
                        "preview_url": preview_url,
                        "errors": preview_gate_result.get("errors", [])[:4],
                        "warnings": preview_gate_result.get("warnings", [])[:4],
                        "score": (preview_gate_result.get("checks") or {}).get("score"),
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

            # ── Pro-mode reviewer rejection → trigger builder re-run ──
            if (
                subtask.agent_type == "reviewer"
                and result.get("success")
                and (getattr(plan, "difficulty", self.difficulty) == "pro")
            ):
                reviewer_output = (result.get("output") or "").strip()
                reviewer_verdict = self._parse_reviewer_verdict(reviewer_output)
                reviewer_rejected = reviewer_verdict == "REJECTED"
                rejection_details = ""

                if reviewer_rejected:
                    # Extract improvement suggestions from reviewer output
                    rejection_details = reviewer_output[:1500]
                    # Find the last builder subtask that completed
                    last_builder = None
                    for st in plan.subtasks:
                        if st.agent_type == "builder" and st.status == TaskStatus.COMPLETED:
                            last_builder = st
                    can_requeue = (
                        last_builder is not None
                        and last_builder.retries < last_builder.max_retries
                        and self._pro_reviewer_requeues < 1
                    )
                    if can_requeue:
                        self._pro_reviewer_requeues += 1
                        rejection_msg = (
                            f"Reviewer REJECTED the product (pro mode quality gate). "
                            f"Builder will re-run with reviewer feedback."
                        )
                        await self.emit("subtask_progress", {
                            "subtask_id": subtask.id,
                            "stage": "reviewer_rejection",
                            "message": rejection_msg,
                        })
                        # Reset the builder to re-run with reviewer feedback
                        last_builder.status = TaskStatus.PENDING
                        last_builder.retries += 1
                        last_builder.error = f"Reviewer rejected: {rejection_details[:600]}"
                        last_builder.description = (
                            f"{last_builder.description}\n\n"
                            f"⚠️ REVIEWER REJECTED YOUR OUTPUT. You MUST fix these issues:\n"
                            f"{rejection_details[:800]}\n\n"
                            f"Read the existing /tmp/evermind_output/index.html, fix the problems, "
                            f"and write the improved version back. Focus on quality.\n"
                        )
                        # Reset reviewer so it re-checks after builder fixes
                        subtask.status = TaskStatus.PENDING
                        subtask.output = ""
                        subtask.completed_at = 0
                        result["success"] = True
                        result["error"] = rejection_msg
                        result["requeue_requested"] = True
                        result["requeue_subtasks"] = [last_builder.id, subtask.id]
                        subtask.error = ""
                        logger.info(
                            f"Reviewer rejected — re-running builder {last_builder.id} "
                            f"(retry {last_builder.retries}/{last_builder.max_retries})"
                        )
                    else:
                        # Builder exhausted retries — let it pass with warning
                        reason = "retry budget reached" if self._pro_reviewer_requeues >= 1 else "builder retries exhausted"
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

            await self.emit("subtask_complete", {
                "subtask_id": subtask.id,
                "agent": subtask.agent_type,
                "success": result.get("success", False),
                "output_preview": full_output[:2000],
                "full_output": full_output,
                "files_created": files_created,
                "error": result.get("error", "") if not result.get("success") else "",
            })
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
            if html_start >= 0 and html_end > html_start:
                html_code = output[html_start:html_end + 7]
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

    async def _run_tester_visual_gate(self) -> Dict[str, Any]:
        """
        Deterministic post-check for tester stage so visual validation is not purely model-text based.
        """
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
                            f"Tester visual gate artifact fallback hit: using {candidate}"
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
        result = await validate_preview(preview_url, run_smoke=self._configured_tester_smoke())
        result["task_id"] = task_id
        return result

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

            # Prefer run-local HTML artifacts to avoid opening stale previews from previous runs.
            cutoff = max(self._run_started_at - 2.0, 0.0)
            run_local: List[tuple[float, Path]] = []
            for html in OUTPUT_DIR.rglob("*"):
                if not html.is_file() or html.suffix.lower() not in (".html", ".htm"):
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
            await self.emit("subtask_progress", {
                "subtask_id": subtask.id,
                "stage": "error",
                "message": f"Subtask failed after max retries: {subtask.error or 'Unknown error'}",
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
            if is_timeout and subtask.retries == 1:
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
            # On retry: skip browser research, use built-in knowledge instead
            enhanced_input = (
                f"{subtask.description}\n\n"
                f"⚠️ PREVIOUS ATTEMPT TIMED OUT — DO NOT USE BROWSER THIS TIME.\n"
                f"Use your built-in knowledge to provide a design brief.\n"
                f"Do NOT call browser tool. Do NOT visit any websites.\n"
                f"Just write your analysis and recommendations based on what you already know.\n"
                f"Be concise — output a SHORT design brief (under 500 words).\n"
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
        Defaults to APPROVED when verdict is absent to avoid false negatives on free-form text.
        """
        text = (output or "").strip()
        if not text:
            return "APPROVED"

        # Prefer structured JSON verdict when available.
        try:
            json_start = text.find("{")
            json_end = text.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(text[json_start:json_end])
                verdict = str(parsed.get("verdict", "")).strip().upper()
                if verdict in ("APPROVED", "REJECTED"):
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
        if '"approved"' in lower or "'approved'" in lower:
            return "APPROVED"
        return "APPROVED"

    # ═══════════════════════════════════════════
    # Report
    # ═══════════════════════════════════════════
    def _build_report(self, plan: Plan, results: Dict) -> Dict:
        success_count = sum(1 for st in plan.subtasks if st.status == TaskStatus.COMPLETED)
        fail_count = sum(1 for st in plan.subtasks if st.status == TaskStatus.FAILED)
        terminal_count = sum(1 for st in plan.subtasks if st.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED))
        pending_count = len(plan.subtasks) - terminal_count
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
