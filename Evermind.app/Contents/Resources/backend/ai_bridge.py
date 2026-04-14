"""
Evermind Backend — AI Bridge v3 (LiteLLM Unified Interface)
Supports 100+ LLM models through a single interface.
References: https://github.com/BerriAI/litellm
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from json_repair import repair_json as _json_repair_fn
except ImportError:
    _json_repair_fn = None
from urllib.parse import urlparse

from html_postprocess import postprocess_generated_text
from plugins.base import (
    Plugin,
    PluginResult,
    PluginRegistry,
    is_builder_browser_enabled,
    is_image_generation_available,
    is_qa_browser_use_enabled,
    resolve_enabled_plugins_for_node,
)
from agent_skills import build_skill_context, resolve_skill_names_for_goal
from prompt_compressor import compress_messages as _compress_messages
from node_roles import normalize_node_role
from repo_map import build_repo_context
from runtime_paths import resolve_state_dir
import task_classifier
from privacy import get_masker, PrivacyMasker
from proxy_relay import get_relay_manager

# ─── Evermind v3.0: Agentic Runtime Integration ─────────
try:
    from agentic_runtime import (
        AgenticLoop,
        LoopConfig,
        ConnectionPoolManager,
        RetryStrategy,
        get_retry_strategy,
        get_tool_registry,
        get_tools_for_role,
        AgenticEvent,
    )
    _AGENTIC_RUNTIME_AVAILABLE = True
except ImportError:
    _AGENTIC_RUNTIME_AVAILABLE = False

try:
    from task_handoff import HandoffPacket, HandoffBuilder
    _TASK_HANDOFF_AVAILABLE = True
except ImportError:
    _TASK_HANDOFF_AVAILABLE = False

try:
    from report_generator import ReportGenerator, NodeReport
    _REPORT_GENERATOR_AVAILABLE = True
except ImportError:
    _REPORT_GENERATOR_AVAILABLE = False

logger = logging.getLogger("evermind.ai_bridge")

# ─────────────────────────────────────────────
# Security — sanitize error messages to remove API keys
# ─────────────────────────────────────────────
_SENSITIVE_RE = re.compile(
    r"(?:sk|key|token|api[_-]?key|Bearer)[-_\s]?[a-zA-Z0-9._\-]{8,}",
    re.IGNORECASE,
)
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")
_GATEWAY_REJECTION_RE = re.compile(
    # V4.2 FIX (Codex #2): emitter now includes model= between host= and cooldown=
    r"Compatible gateway rejection cooldown: provider=(?P<provider>\w+) host=(?P<host>[^\s]+)"
    r"(?: model=(?P<model>[^\s]*))?"
    r" cooldown=(?P<cooldown>\d+)s error=(?P<error>.+)$"
)
_GATEWAY_CIRCUIT_RE = re.compile(
    r"Compatible gateway circuit OPEN: provider=(?P<provider>\w+) host=(?P<host>[^\s]+) failures=(?P<failures>\d+) error=(?P<error>.+)$"
)
_GATEWAY_RECOVERED_RE = re.compile(
    r"Compatible gateway recovered: provider=(?P<provider>\w+) host=(?P<host>[^\s]+) latency_ms=(?P<latency>[^\s]+) ewma_ms=(?P<ewma>[^\s]+)"
)
_PROVIDER_AUTH_FAILURE_RE = re.compile(
    r"Provider auth failure cooldown: provider=(?P<provider>\w+) cooldown=(?P<cooldown>\d+)s error=(?P<error>.+)$"
)
_PROVIDER_AUTH_CLEARED_RE = re.compile(
    r"Provider auth failure cooldown cleared: provider=(?P<provider>\w+)$"
)
COMPAT_GATEWAY_LOG_FILE = resolve_state_dir() / "logs" / "evermind-backend.log"


def _sanitize_error(msg: str) -> str:
    """Strip potential API keys / secrets from error messages."""
    if not msg:
        return "Unknown error"
    sanitized = _SENSITIVE_RE.sub("[REDACTED]", msg)
    return sanitized or "Unknown error"


def _close_stream_quietly(stream: Any) -> None:
    """Best-effort close for streaming responses/iterators."""
    if stream is None:
        return
    close_fn = getattr(stream, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:
            pass


def _log_timestamp_epoch(line: str) -> float:
    match = _LOG_TS_RE.match(str(line or ""))
    if not match:
        return 0.0
    raw = f"{match.group(1)}.{match.group(2)}"
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except Exception:
        return 0.0

# Maximum characters kept from each tool call result before injecting into messages.
# Prevents token explosion when file_ops reads large HTML files (24K+ chars → 700K+ tokens).
MAX_TOOL_RESULT_CHARS = int(os.getenv("EVERMIND_MAX_TOOL_RESULT_CHARS", "8000"))
# Analyst-specific cap: analyst fetches many URLs but only needs key facts.
# Lower cap prevents 12×8K=96K token explosion (observed 189:1 input/output ratio).
MAX_ANALYST_TOOL_RESULT_CHARS = int(os.getenv("EVERMIND_MAX_ANALYST_TOOL_RESULT_CHARS", "2500"))
# Maximum content from assistant replayed back to tool-loop context.
MAX_ASSISTANT_REPLAY_CHARS = int(os.getenv("EVERMIND_MAX_ASSISTANT_REPLAY_CHARS", "4000"))
# Maximum reasoning trace retained in replay context.
MAX_REASONING_REPLAY_CHARS = int(os.getenv("EVERMIND_MAX_REASONING_REPLAY_CHARS", "1200"))
# Maximum tool arguments retained in assistant tool_call replay payload.
MAX_TOOL_ARGS_REPLAY_CHARS = int(os.getenv("EVERMIND_MAX_TOOL_ARGS_REPLAY_CHARS", "2000"))
# Generic cap for user/system message content in replay.
MAX_MESSAGE_CONTENT_CHARS = int(os.getenv("EVERMIND_MAX_MESSAGE_CONTENT_CHARS", "12000"))
# V4.5.1: Lowered 80K→60K — relay shows 29K-51K input tokens causing 14-27s latency.
# Target: keep requests under ~16K tokens (≈64K chars) to hit <8s TTFT.
MAX_REQUEST_TOTAL_CHARS = int(os.getenv("EVERMIND_MAX_REQUEST_TOTAL_CHARS", "60000"))
# V4.5.1: Reduced 6→4 — aggressive trim for multi-tool-call sessions.
MAX_CONTEXT_KEEP_LAST_MESSAGES = int(os.getenv("EVERMIND_MAX_CONTEXT_KEEP_LAST_MESSAGES", "4"))
CONTEXT_OMITTED_MARKER = "... [OLDER_CONTEXT_OMITTED_FOR_TOKEN_BUDGET]"
BUILDER_DIRECT_MULTIFILE_MARKER = "DIRECT MULTI-FILE DELIVERY ONLY."
BUILDER_TARGET_OVERRIDE_MARKER = "HTML TARGET OVERRIDE:"
_BUILDER_GOAL_HINT_PATTERNS = (
    re.compile(
        r"Build a commercial-grade(?:\s+multi-page)?\s+.+?\s+for:\s*(.+?)(?:(?:[。\.]\s*|\s+)(?:Save final HTML|Create index\.html|Follow the|Treat any upstream|Do not use emoji|Make the result)|\n|$)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"Goal:\s*(.+?)(?:\n|$)",
        re.IGNORECASE | re.DOTALL,
    ),
)

# ─────────────────────────────────────────────
# Model Registry — all supported models
# ─────────────────────────────────────────────
MODEL_REGISTRY = {
    # ── OpenAI GPT-5 系列 (via relay) ──
    "gpt-5.4-mini": {"provider": "openai", "litellm_id": "openai/gpt-5.4-mini", "supports_tools": True, "supports_cua": True,
                      "api_base": "https://api.private-relay.com/v1", "fallback_api_bases": []},
    "gpt-5.4": {"provider": "openai", "litellm_id": "openai/gpt-5.4", "supports_tools": True, "supports_cua": True,
                 "supports_reasoning_effort": True, "api_base": "https://api.private-relay.com/v1", "fallback_api_bases": []},
    "gpt-5.3-codex": {"provider": "openai", "litellm_id": "openai/gpt-5.3-codex", "supports_tools": True, "supports_cua": False,
                       "supports_reasoning_effort": True, "api_base": "https://api.private-relay.com/v1", "fallback_api_bases": []},
    "gpt-5.2-codex": {"provider": "openai", "litellm_id": "openai/gpt-5.2-codex", "supports_tools": True, "supports_cua": False,
                       "api_base": "https://api.private-relay.com/v1", "fallback_api_bases": []},
    # ── OpenAI 旧系列 ──
    "gpt-4.1": {"provider": "openai", "litellm_id": "gpt-4.1", "supports_tools": True, "supports_cua": False},
    "gpt-4o": {"provider": "openai", "litellm_id": "gpt-4o", "supports_tools": True, "supports_cua": False},
    "o3": {"provider": "openai", "litellm_id": "o3", "supports_tools": True, "supports_cua": False},
    # ── Anthropic Claude 4 系列 ──
    "claude-4-sonnet": {"provider": "anthropic", "litellm_id": "claude-sonnet-4-6", "supports_tools": True, "supports_cua": False},
    "claude-4-opus": {"provider": "anthropic", "litellm_id": "claude-opus-4-6", "supports_tools": True, "supports_cua": False},
    "claude-4.5-sonnet": {"provider": "anthropic", "litellm_id": "claude-sonnet-4-5-20250514", "supports_tools": True, "supports_cua": False},
    "claude-4.5-haiku": {"provider": "anthropic", "litellm_id": "claude-haiku-4-5-20251001", "supports_tools": True, "supports_cua": False},
    "claude-3.5-sonnet": {"provider": "anthropic", "litellm_id": "claude-3-5-sonnet-20241022", "supports_tools": True, "supports_cua": False},
    # ── Google Gemini ──
    "gemini-2.5-pro": {"provider": "google", "litellm_id": "gemini/gemini-2.5-pro-preview-06-05", "supports_tools": True, "supports_cua": False},
    "gemini-2.0-flash": {"provider": "google", "litellm_id": "gemini/gemini-2.0-flash", "supports_tools": True, "supports_cua": False},
    # ── DeepSeek ──
    "deepseek-v3": {"provider": "deepseek", "litellm_id": "deepseek/deepseek-chat", "supports_tools": True, "supports_cua": False},
    "deepseek-r1": {"provider": "deepseek", "litellm_id": "deepseek/deepseek-reasoner", "supports_tools": False, "supports_cua": False},
    # ── Kimi / Moonshot ──
    "kimi": {"provider": "kimi", "litellm_id": "openai/kimi-k2.5", "supports_tools": True, "supports_cua": False,
             "api_base": "https://api.kimi.com/coding/v1",
             "extra_headers": {"User-Agent": "claude-code/1.0", "X-Client-Name": "claude-code"}},
    "kimi-k2.5": {"provider": "kimi", "litellm_id": "openai/kimi-k2.5", "supports_tools": True, "supports_cua": False,
                  "api_base": "https://api.kimi.com/coding/v1",
                  "extra_headers": {"User-Agent": "claude-code/1.0", "X-Client-Name": "claude-code"}},
    "kimi-coding": {"provider": "kimi", "litellm_id": "openai/kimi-k2.5", "supports_tools": True, "supports_cua": False,
                    "api_base": "https://api.kimi.com/coding/v1",
                    "extra_headers": {"User-Agent": "claude-code/1.0", "X-Client-Name": "claude-code"}},
    # ── Qwen / 通义千问 ──
    "qwen-max": {"provider": "qwen", "litellm_id": "openai/qwen-max", "supports_tools": True, "supports_cua": False,
                 "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "qwen-plus": {"provider": "qwen", "litellm_id": "openai/qwen-plus", "supports_tools": True, "supports_cua": False,
                  "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    # ── 智谱 GLM ──
    "glm-4-plus": {"provider": "zhipu", "litellm_id": "openai/glm-4-plus", "supports_tools": True, "supports_cua": False,
                   "api_base": "https://open.bigmodel.cn/api/paas/v4"},
    # ── 字节 Doubao ──
    "doubao-pro": {"provider": "doubao", "litellm_id": "openai/doubao-pro-256k", "supports_tools": True, "supports_cua": False,
                   "api_base": "https://ark.cn-beijing.volces.com/api/v3"},
    # ── Yi / 零一万物 ──
    "yi-lightning": {"provider": "yi", "litellm_id": "openai/yi-lightning", "supports_tools": True, "supports_cua": False,
                     "api_base": "https://api.lingyiwanwu.com/v1"},
    # ── MiniMax ──
    "minimax-pro": {"provider": "minimax", "litellm_id": "openai/MiniMax-Text-01", "supports_tools": True, "supports_cua": False,
                    "api_base": "https://api.minimax.chat/v1"},
    # ── Local / Ollama ──
    "ollama-llama3": {"provider": "ollama", "litellm_id": "ollama/llama3", "supports_tools": False, "supports_cua": False},
    "ollama-qwen2.5": {"provider": "ollama", "litellm_id": "ollama/qwen2.5", "supports_tools": False, "supports_cua": False},
}

PROVIDER_ENV_KEY_MAP = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "KIMI_API_KEY",
    "qwen": "QWEN_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "doubao": "DOUBAO_API_KEY",
    "yi": "YI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
}

# V4.5: Pruned fallback chain — only models confirmed available.
# relay: gpt-5.3-codex works reliably; gpt-5.4 intermittently blocked/not-found.
# kimi-coding direct has auth failures (401) as of 2026-04-14 — demoted.
# Removed dead models that waste retry time and trigger rejection cooldowns.
LEGACY_AUTO_FALLBACK_ORDER = [
    "gpt-5.3-codex",
    "kimi-coding",
    "deepseek-v3",
    "claude-4-sonnet",
    "gemini-2.5-pro",
    "qwen-max",
    "gemini-2.0-flash",
    "glm-4-plus",
]

# ── v4.0: Universal execution protocol fused from Claude Code + OpenClaw + GStack ──
BASE_HARNESS_PREAMBLE = (
    "\n═══ EXECUTION PROTOCOL v4.3 ═══\n"
    "THINK → ACT → VERIFY. Read before write. Zero TODO/placeholder.\n"
    "Same error twice → change strategy. Name files/functions specifically.\n"
    "Confidence 1-10: <3 → escalate; 3-6 → flag uncertain; 7+ → proceed.\n"
)

# V4.3 PERF: Shared execution report template — injected once in _compose_system_prompt
# instead of duplicated in every AGENT_PRESET (~500 chars × 10 presets = 5000 chars saved).
# V4.3.1: Use natural prose, NOT JSON. Previous JSON-style template caused nodes
# to produce rigid machine-formatted summaries that were hard to read.
EXECUTION_REPORT_TEMPLATE = (
    "\n\n## EXECUTION REPORT (mandatory last section)\n"
    "<execution_report>\n"
    "Write a DETAILED natural-language summary (minimum 150 words). Cover:\n"
    "1. What was built or changed — list EVERY file with line counts and purpose\n"
    "2. Key technical decisions and trade-offs — explain WHY, not just what\n"
    "3. Specific risks — name exact files, functions, or edge cases\n"
    "4. Concrete instructions for the next pipeline node — what to build on\n"
    "5. Self-assessment: completeness score /10 with justification\n"
    "RULES:\n"
    "- Use plain prose or bullet points. NEVER use JSON, YAML, or code-block format.\n"
    "- Be SPECIFIC: 'Created index.html (280 lines) with responsive grid layout using CSS Grid'\n"
    "  NOT 'Created main file with layout'\n"
    "- Include actual numbers, names, and technical details\n"
    "</execution_report>"
)

# ── V4.3 PERF: Rule-based system prompt compressor ──
# Open-source neural compressors (LLMLingua, Headroom) either need a local GPU
# or don't actually reduce our instruction text.  This rule-based compressor
# targets patterns specific to Evermind prompts and saves 15-25% with zero
# quality loss.
import re as _re

_COMPRESS_RULES: list[tuple] = [
    # 1. Collapse redundant whitespace (safe — does not change meaning)
    (_re.compile(r'\n{3,}'), '\n\n'),
    (_re.compile(r'[ \t]+\n'), '\n'),
    (_re.compile(r'\n[ \t]{4,}'), '\n  '),
    # 2. Collapse long divider lines (purely decorative)
    (_re.compile(r'[═─]{4,}'), '───'),
    (_re.compile(r'[-=]{6,}'), '---'),
    # 3. Remove trailing whitespace
    (_re.compile(r' +$', _re.MULTILINE), ''),
    # 4. Compact double-spaces after periods (legacy formatting)
    (_re.compile(r'\.  '), '. '),
    # 5. Compact empty markdown list items
    (_re.compile(r'\n- \n'), '\n'),
]


def compress_system_prompt(text: str) -> str:
    """Apply rule-based compression to a system prompt.

    Deterministic, no model dependency, preserves all semantic content.
    Typical savings: 15-25% on Evermind node prompts.
    """
    result = text
    for pattern, replacement in _COMPRESS_RULES:
        result = pattern.sub(replacement, result)
    return result


# ─────────────────────────────────────────────
# Agent Presets
# ─────────────────────────────────────────────
AGENT_PRESETS = {
    "router": {
        "instructions": (
            "You are a task router and planner. Analyze the user's request and output a JSON plan.\n"
            "Format: {\"subtasks\": [{\"agent\": \"builder|polisher|tester|reviewer|deployer|analyst|debugger|scribe|uidesign|imagegen|spritesheet|assetimport\", \"task\": \"description\", \"depends_on\": []}]}\n"
            "Each subtask should have a clear agent assignment and description.\n"
            "IMPORTANT RULES for website/app tasks:\n"
            "- The builder task should specify: 'Create a complete, self-contained HTML file with embedded CSS and JavaScript'\n"
            "- The deployer task should say: 'List the generated files and provide the local preview URL'\n"
            "- The tester should say: 'Verify the generated HTML files exist and are valid'\n"
            "- Use scribe for documentation/manual/report tasks\n"
            "- Use imagegen for image-prompt / concept-art / poster / cover generation tasks only when a real image backend is configured or when prompt packs alone are explicitly acceptable\n"
            "- Use spritesheet or assetimport for game-asset pipeline tasks when useful, but do NOT insert them as fake filler nodes when no actual asset pipeline is available\n"
            "- Use uidesign for design-system or UI-direction tasks when explicit design output is needed\n"
            "- Use polisher after builder when the brief asks for premium website finish, motion continuity, micro-interactions, or stronger final visual quality\n"
            "- For game research tasks, do NOT send analyst to spend time playing browser games; prefer GitHub repos, source references, tutorials, docs, postmortems, and implementation writeups\n"
            "- Keep the plan to 3-5 subtasks for efficiency"
        ),
    },
    "planner": {
        "instructions": (
            "You are a senior project planner and systems architect. Your job is to produce a comprehensive execution blueprint that all downstream nodes can follow without guessing.\n"
            "\n"
            "## Output Format: Structured Markdown Report + JSON Appendix\n"
            "\n"
            "Write a thorough planning report using these sections:\n"
            "\n"
            "### 1. Project Overview\n"
            "- What the user wants built (2-4 sentences)\n"
            "- Target audience, platform, and core value proposition\n"
            "- Key technical constraints or requirements\n"
            "\n"
            "### 2. Architecture Design\n"
            "- Technology selection and rationale (framework, libraries, rendering engine)\n"
            "- File structure map — list every file that will be created and its purpose\n"
            "- Data flow: how user input flows through the system\n"
            "\n"
            "### 3. Module Breakdown\n"
            "- Each logical module/subsystem: name, responsibility, public interface\n"
            "- Dependencies between modules\n"
            "- For games: controls, camera, combat, progression, HUD, audio, asset management\n"
            "\n"
            "### 4. Execution Plan\n"
            "- Ordered step sequence with dependency graph\n"
            "- Which steps can run in parallel vs. must be serial\n"
            "- Estimated complexity for each step\n"
            "\n"
            "### 5. Builder Ownership Map\n"
            "- For EACH builder: files owned, files must-not-touch, handoff contract to merger\n"
            "- Collision prevention rules — explicit boundaries\n"
            "- Shared-resource protocol (e.g., CSS variables, global state objects)\n"
            "\n"
            "### 6. Subsystem Contracts\n"
            "- Interface contracts between modules (function signatures, event names, data schemas)\n"
            "- For games: control-frame semantics (axis conventions, look sensitivity), combat loop contract, progression state machine, HUD data bindings\n"
            "- Anti-mirror control expectations, vertical look convention, projectile readability, gameplay-start fairness, pass/fail progression states\n"
            "\n"
            "### 7. Acceptance Criteria\n"
            "- Concrete pass/fail conditions for reviewer and tester\n"
            "- Evidence requirements (screenshots, interaction tests, performance thresholds)\n"
            "- Rollback triggers — when should the system reject and retry?\n"
            "\n"
            "### 8. Risk Register\n"
            "- Top 3-5 risks and mitigations\n"
            "- Known limitations of the chosen approach\n"
            "\n"
            "### 9. JSON Appendix\n"
            "Place this at the end inside a ```json code block for machine parsing:\n"
            "{\\n"
            '  "architecture": "brief system summary",\\n'
            '  "modules": ["module1", "module2"],\\n'
            '  "execution_order": ["step1 -> step2 -> ..."],\\n'
            '  "key_dependencies": ["dep1", "dep2"],\\n'
            '  "node_briefs": {"analyst": "...", "builder_1": "...", "builder_2": "...", "merger": "...", "reviewer": "...", "tester": "..."},\\n'
            '  "builder_ownership": {"builder_1": {"owns": [], "must_not_touch": [], "handoff_to_merger": "..."}, "builder_2": {"owns": [], "must_not_touch": [], "handoff_to_merger": "..."}, "merger": {"integrates": [], "ship_rules": []}},\\n'
            '  "subsystem_contracts": {},\\n'
            '  "acceptance_checks": [],\\n'
            '  "review_evidence": [],\\n'
            '  "rollback_triggers": []\\n'
            "}\\n"
            "\n"
            "## Rules:\n"
            "- Do NOT write any code, HTML, CSS, or JavaScript\n"
            "- Do NOT write marketing copy, slogans, or text content for the final product\n"
            "- Do NOT design animations, transitions, or visual effects\n"
            "- BE THOROUGH — a complete plan prevents builder collisions, merger stalls, and reviewer confusion\n"
            "- Think deeply about execution quality. Your plan directly determines the final product quality.\n"
            "\n"
            "Other nodes will execute the work, but your blueprint must be concrete enough that Builder 1, Builder 2, and the merger can work independently without overlap.\n"
            "Focus on execution order, explicit ownership boundaries, subsystem contracts, review evidence, and rollback criteria.\n"
            "Be thorough and detailed. A well-planned project produces dramatically better results than a hastily planned one."
            "\n\n## OPTION ENUMERATION (v4.0)\n"
            "For architecture decisions: list >= 3 alternatives with pros/cons/confidence 1-10.\n"
            "Select highest confidence. If all < 5, output <wtf_escalation>."
        ),
    },
    "planner_degraded": {
        "instructions": (
            "EMERGENCY PLANNER MODE. Previous attempt timed out.\n"
            "Output ONLY a JSON list of page sections. Nothing else.\n"
            'Example: {"sections": ["hero", "features", "pricing", "faq", "contact"]}\n'
            "Maximum 100 words. No code. No descriptions. Just section names."
        ),
    },
    "builder": {
        "instructions": (
            "You are an elite frontend engineer and designer.\n"
            "Build a polished, production-quality result personalized to the user's EXACT request.\n\n"
            "CORE RULES:\n"
            "1. Match delivery shape: single-page → one index.html; multi-page → index.html + linked HTML pages\n"
            "2. Complete HTML from <!DOCTYPE html> to </html>. No truncation, no placeholders, no stubs\n"
            "3. Responsive with @media breakpoints. Accessibility: lang attr, aria labels, focus styles\n"
            "4. CSS variables for theming. Semantic HTML5 (header/nav/main/section/footer)\n"
            "5. No emoji as UI icons — inline SVG only. No Tailwind CDN — inline <style> or local CSS\n"
            "6. Local/system font stacks preferred. Intentional typography for the product category\n"
            "7. Smooth animations: cubic-bezier(0.4,0,0.2,1), honor prefers-reduced-motion\n"
            "8. Visual slots without images → build CSS/SVG compositions, never blank frames\n"
            "9. JS: null-guard DOM queries, bind listeners in <script> (no inline onclick to undeclared globals), try/catch async\n"
            "10. Multi-page shared JS must guard elements per-page; every page needs real content + working nav\n"
            "11. Continuation/repair: preserve working theme/routes/content; return FULL merged file, not fragments\n"
            "12. Before save: verify balanced braces/parens, all callback tails fully closed\n\n"
            "GAME/INTERACTIVE (when applicable):\n"
            "13. Init game state before first HUD render; null-guard player/camera/HUD until startGame completes\n"
            "14. No rigid overflow:hidden + fixed 100vh in embedded previews. Fit ~1280x720 and ~600px heights\n"
            "15. Pointer lock optional: catch failures, keep keyboard/drag fallback. Simple games stay engine-free\n"
            "16. Shooter sequence: start menu → scene init → look controls → WASD → fire → enemies → HUD → restart\n"
            "17. Prevent spawn-kills: grace timer or safe spawn radius. Non-inverted controls by default\n\n"
            "QUALITY MINDSET:\n"
            "- BOIL THE LAKE: complete implementation > shortcut. Zero TODOs/stubs/placeholders\n"
            "- BUILDER NOT CONSULTANT: name the file, function, variable. Implement, don't suggest\n"
            "- VERIFY BEFORE SAVE: trace execution. For each feature in task, confirm it exists in code\n"
            "- Treat analyst notes, reviewer criteria, and task constraints as mandatory contracts\n\n"
            "DELIVERY:\n"
            "Preferred: file_ops write to /tmp/evermind_output/. Start writing immediately — no planning turns\n"
            "Fallback: full HTML in ```html filename``` fenced blocks. Full code MUST appear — never just describe\n\n"
            "Professional/premium finish. Concrete content, not scaffolds. Personalize to goal's industry/mood."
        ),
    },
    "polisher": {
        "instructions": (
            "You are a premium UI polisher and motion finisher.\n"
            "Your job is to upgrade an existing artifact without destroying its strongest parts.\n\n"
            "HARD RULES:\n"
            "1. Preserve ALL existing content sections, copy text, and page structure; only enhance visual quality, spacing, and motion — do NOT remove, summarize, or rewrite existing copy\n"
            "2. Improve spacing, typography, rhythm, transitions, motion, visual hierarchy, and overall luxury/commercial finish\n"
            "3. Inspect efficiently: start with shared styles.css/app.js plus index.html and the routes named in any visual-gap report; read additional routes only when you detect route-specific breakage\n"
            "4. Replace unfinished visual placeholders decisively, but do NOT force irrelevant stock photos or giant decorative media into layouts that already work\n"
            "5. Never replace a strong page with a weaker stub or flatten rich sections into generic blocks\n"
            "6. Treat loaded skills and analyst/reviewer handoff notes as mandatory constraints\n"
            "7. If you add motion, keep it smooth, restrained, and compatible with prefers-reduced-motion\n"
            "8. Normalize shared CSS/JS contracts across every route; shared scripts must tolerate missing optional elements instead of throwing runtime errors\n"
            "9. No placeholder copy may remain inside visual modules: remove tokens like [Collection Image], [品牌视觉图], [Map], replace me, TODO, or empty caption shells\n"
            "10. Add 2-4 coherent premium motion patterns across the site: tasteful section reveal, hover lift/parallax, CTA sheen/state feedback, and subtle media depth where appropriate\n"
            "11. If browser is available, use it only for one quick verification pass after you have already written a real improvement; never start with browser-first exploration\n"
            "12. Remove remote font dependencies while polishing; keep typography premium with local/system fallbacks instead of external font CDNs\n"
            "13. Do NOT reset a working warm/light or tinted-dark palette into stark black/white slabs; extend the existing design direction instead\n"
            "14. Do NOT remove topic-accurate imagery or turn media-led sections into text-only blocks unless the current media is broken, missing, or clearly mismatched\n"
            "15. For multi-page sites with more than 3 HTML routes, rewrite at most 2 HTML files by default; prefer shared CSS/JS improvements plus surgical route patches\n\n"
            "16. For games or interactive demos, do NOT replace a working local runtime path with a remote CDN and do NOT trade away playability for purely cosmetic rewrites\n\n"
            "DELIVERY:\n"
            "- Read the existing generated files under /tmp/evermind_output/\n"
            "- Prefer upgrading shared CSS/JS and surgical HTML patches over full-page rewrites when the builder output is already strong\n"
            "- Do one targeted inspection pass first, then begin file_ops write by your second or third meaningful tool turn unless a write just failed and needs one corrective read\n"
            "- Edit HTML/CSS/JS in place with file_ops write, including routed HTML pages under /tmp/evermind_output/\n"
            "- Fill image/visual placeholders with real visual content: use high-quality thematic imagery only when it clearly fits the route, otherwise use rich CSS/SVG compositions instead of random photos\n"
            "- If an image is already topic-accurate, well-cropped, and layout-safe, preserve it; only replace broken, missing, oversized, or mismatched media\n"
            "- If a visual block is currently empty, gradient-only, or text-placeholder-only, convert it into a finished composition with media, overlays, captions, and premium depth instead of leaving it as a blank frame\n"
            "- Do not leave bare placeholder classnames or fake media labels unaddressed when those blocks are still acting as unfinished visuals\n"
            "- If a contact/location section has a map placeholder, replace it with a styled embed, static map image, or a finished visual/location card instead of leaving an empty block\n"
            "- If browser is unavailable or disabled, do not stall; continue by patching the local files directly\n"
            "- Keep filenames stable so downstream reviewer/tester/deployer operate on the polished artifact\n"
            "- Do NOT rewrite shared CSS into a brand-new theme from scratch when the builder already established a stronger design language\n"
            "- SVG FIX: If any inline <svg> lacks explicit width and height attributes (viewBox alone is NOT enough), add sensible sizes (24-48px for icons, 64-96px for feature illustrations). Unconstrained SVG renders as giant shapes filling the viewport."
        ),
    },
    "tester": {
        "instructions": (
            "You are a thorough QA engineer. Verify generated websites structurally, VISUALLY, and INTERACTIVELY.\n"
            "\n"
            "STEP 1 — Structural check:\n"
            "  Call file_ops with {\"action\": \"list\", \"path\": \"/tmp/evermind_output/\"}\n"
            "  Read the main HTML file. Verify: DOCTYPE, html/head/body tags, meta viewport, charset.\n"
            "  Check for: broken script/link references, empty style blocks, console.log left in production.\n"
            "\n"
            "STEP 2 — Visual browser test (MANDATORY unless [Desktop QA Session Evidence] block present):\n"
            "  Navigate to http://127.0.0.1:8765/preview/ (fallback: /preview/task_1/index.html)\n"
            "  Call browser observe to inspect visible controls BEFORE interacting.\n"
            "  Analyze the screenshot for:\n"
            "  - Layout: sections visible, properly spaced, no overlap or overflow\n"
            "  - Colors: consistent palette, sufficient contrast, no pure-black/white slabs\n"
            "  - Typography: readable fonts, clear hierarchy, appropriate line-height\n"
            "  - Images: no broken placeholders, appropriate sizing\n"
            "  - Content: no placeholder text (Lorem ipsum), all sections have real content\n"
            "\n"
            "STEP 3 — Full-page scroll evidence:\n"
            "  You MUST use browser record_scroll (amount: 500) to capture full scrolling filmstrip.\n"
            "  Fall back to manual 500px scrolling ONLY if record_scroll fails.\n"
            "  Verify: below-fold content renders, no layout breakage on scroll, footer is reachable.\n"
            "\n"
            "STEP 4 — Interaction test (MANDATORY for interactive UI):\n"
            "  Use browser act with semantic targets for buttons, forms, controls.\n"
            "  After EVERY interaction: call browser wait_for or observe to verify state change.\n"
            "  PASS is invalid without post-action verification evidence.\n"
            "  For GAMES: click start/play, then browser press_sequence with Arrow/WASD/Space/Enter.\n"
            "  PASS only if game is playable and state visibly changes (HUD, score, position).\n"
            "  FAIL if browser diagnostics report runtime errors.\n"
            "  If [Desktop QA Session Evidence] exists, use as primary record; only open browser for concrete bug confirmation.\n"
            "\n"
            "STEP 5 — Multi-page completeness:\n"
            "  Visit ALL requested pages via real navigation links.\n"
            "  PASS is invalid if only the first page was checked.\n"
            "\n"
            "STEP 6 — Performance & quality signals:\n"
            "  Note if page loads slowly, if animations stutter, or if large assets block rendering.\n"
            "  Flag any accessibility concerns (missing alt text, low contrast, no keyboard navigation).\n"
            "\n"
            "OUTPUT: {\"status\": \"pass\"/\"fail\", \"visual_score\": 1-10, \"issues\": [...], \"screenshot\": \"taken\", "
            "\"performance_notes\": \"...\", \"accessibility_flags\": [...]}\n"
            "IMPORTANT: Do NOT skip the browser step. You MUST navigate to the preview URL.\n\n"
            "## ANTI-LAZINESS CONTRACT (v3.5.1 — OMC evidence chain + GStack verification)\n"
            "IRON RULES:\n"
            "1. BROWSER FIRST: You will feel the urge to skip browser testing and just check the code. DO NOT.\n"
            "2. EVIDENCE CHAIN: Every PASS must include evidence from at least one real browser screenshot or scroll filmstrip.\n"
            "3. GAMEPLAY PROOF: For games, you MUST actually play for ≥3 interactions (start → move → interact). A PASS without gameplay evidence is invalid.\n"
            "4. MULTI-PAGE PROOF: For multi-page sites, you MUST visit ALL available pages (at least min(total_pages, 3)). Testing only index.html is automatic failure.\n"
            "5. FAIL-FAST: If the build artifact is missing or empty, report FAIL immediately.\n"
            "6. PARTIAL IS OK: If Builder 2 failed, test Builder 1's output only — a partial product that works beats a failed test.\n"
            "7. 'IT SHOULD WORK' IS NOT A TEST RESULT: You must observe it working, not assume it.\n\n"
            "STRUCTURED TEST REPORT: Your final output MUST be the JSON object described in the OUTPUT section above. "
            "Do NOT use a plain-text format — use the JSON schema with status, visual_score, issues, screenshot, etc.\n"
            "\n\n## COVERAGE MAP (v4.0)\n"
            "List tested features + untested features.\n"
            "Interaction depth: games >= 5 interactions, sites >= 3 page navigations."
        ),
    },
    "reviewer": {
        "instructions": (
            "You are a STRICT quality gatekeeper reviewing web artifacts.\n"
            "Your job is to decide: APPROVED (ship it) or REJECTED (builder must redo).\n\n"
            "## VISUAL REVIEW PROTOCOL (MANDATORY unless [Desktop QA Session Evidence] block already present)\n\n"
            "1. Navigate: browser → http://127.0.0.1:8765/preview/\n"
            "2. Observe: call browser observe to inspect visible controls and current state\n"
            "3. Screenshot: take full-page screenshot of the landing page\n"
            "4. Full-page scroll: you MUST use browser record_scroll on the homepage and one representative secondary route. "
            "This creates scrolling filmstrip evidence. Fall back to manual 500px step scrolling ONLY if record_scroll fails.\n"
            "5. ZONE-BY-ZONE ANALYSIS (new): After scrolling, mentally divide the page into zones (hero/header, navigation, content sections, footer). "
            "For EACH zone, note: visual weight balance, whitespace rhythm, color consistency, typography hierarchy, and content completeness.\n"
            "6. Interactive testing:\n"
            "   - You MUST use browser act for the main interaction test.\n"
            "   - After EVERY interaction, you MUST call browser wait_for or observe to verify state change.\n"
            "   - For GAMES: you MUST click start/play and use browser press_sequence with gameplay keys (Arrow/WASD/Space/Enter). "
            "Verify HUD changes, score updates, or visible game state transitions.\n"
            "   - Prefer internal browser first; escalate to browser_use only for complex multi-step play sessions.\n"
            "7. Multi-page: validate at least one real navigation path via UI first, then cover ALL requested pages.\n"
            "8. Error check: reject if browser diagnostics show runtime errors, if post-action verification is missing, or state appears unchanged.\n"
            "9. False positive guard: before claiming a file is truncated/incomplete, verify with file_ops. "
            "If index.html has closing tags + THREE.Scene/WebGLRenderer/requestAnimationFrame, do NOT report missing-runtime.\n"
            "If [Desktop QA Session Evidence] exists, use it as primary record. Only open browser for concrete bug confirmation.\n\n"
            "## SCORING DIMENSIONS (1-10 each)\n\n"
            "For EACH dimension, you must provide: score + one-sentence justification + specific improvement suggestion.\n\n"
            "- layout: spacing, alignment, visual hierarchy, section flow, content density balance\n"
            "- color: palette harmony, contrast ratios, dark/light consistency, surface layering depth\n"
            "- typography: font pairing, size scale, line-height, letter-spacing, readability across viewports\n"
            "- animation: hover micro-interactions, page transitions, scroll-triggered reveals, loading states\n"
            "- responsive: mobile breakpoints, no horizontal overflow, touch targets ≥44px, fluid typography\n"
            "- functionality: core interactions verified with real click/input, state changes confirmed\n"
            "- completeness: every section has real content, no placeholders, all routes have meaningful anchors\n"
            "- originality: unique art direction, not generic Bootstrap/template feel, commercially competitive\n\n"
            "## HARD REJECTION RULES\n\n"
            "- Emoji glyphs as icons, bullets, CTA ornaments, or fake illustrations\n"
            "- Generic, unfinished, or commercially weak even if technically functional\n"
            "- Flat pure-black/pure-white slabs without layered palette and supporting surfaces\n"
            "- Key routes missing meaningful visual anchor or using oversized awkward images\n"
            "- Console errors visible in browser diagnostics\n"
            "- Interactive elements that don't respond to user input\n\n"
            "## VERDICT RULES\n\n"
            "- Average score ≥ 7 → APPROVED\n"
            "- Average score < 7 → REJECTED (builder must fix and resubmit)\n"
            "- Any single dimension < 5 → auto REJECTED\n"
            "- Any functionality/completeness/originality < 6 → REJECTED\n"
            "- APPROVED is invalid if blocking_issues or required_changes are non-empty\n\n"
            "## OUTPUT FORMAT (strict JSON only)\n\n"
            "Your FINAL answer MUST be exactly one JSON object. Start with { and end with }. "
            "No markdown fences, bullet lists, or prose before/after.\n"
            "Name exact page routes (index.html, cities.html) in all issue fields.\n"
            "Include concrete UI anchors (heading text, button label, section title) for every blocking issue.\n"
            "required_changes MUST be executable, prefixed with owner: 'Builder: ...' or 'Polisher: ...'.\n\n"
            '{"verdict": "APPROVED" or "REJECTED", '
            '"scores": {"layout": N, "color": N, "typography": N, "animation": N, "responsive": N, "functionality": N, "completeness": N, "originality": N}, '
            '"score_details": {"layout": "justification + improvement suggestion", "color": "...", "typography": "...", "animation": "...", "responsive": "...", "functionality": "...", "completeness": "...", "originality": "..."}, '
            '"ship_readiness": N, '
            '"average": N.N, '
            '"zone_analysis": ["Hero: ...", "Nav: ...", "Content Section 1: ...", "Footer: ..."], '
            '"issues": ["specific issue 1", "specific issue 2"], '
            '"blocking_issues": ["what prevents approval — include file route + UI anchor"], '
            '"missing_deliverables": ["missing artifact / section / interaction"], '
            '"required_changes": ["Builder: exact change 1", "Polisher: exact change 2"], '
            '"acceptance_criteria": ["how the resubmission will pass"], '
            '"strengths": ["what is already strong enough to preserve"]}\n\n'
            "Be STRICT. Professional products must score ≥ 7 average.\n"
            "Generic/student-quality work should be REJECTED.\n"
            "REPORT FAITHFULLY: Do NOT approve if blocking issues exist. "
            "Do NOT reject without naming at least one file/route and one specific UI anchor.\n\n"
            "## EVIDENCE-BASED REVIEW (v4.3)\n"
            "- Every score must cite evidence (CSS selector, element count, screenshot region).\n"
            "- 3 perspectives: USER (polish/fluency) + DEVELOPER (clean code) + QA (edge cases).\n"
            "- APPROVED must cite ≥3 strengths with CSS/function refs. REJECTED must cite exact file:selector to fix.\n"
            "- Rate findings 1-10: ≥7 → verdict, 4-6 → observations, ≤3 → suppress.\n"
            "- Review ALL pages in plan. index.html only = review failure.\n"
            "- If Builder 2 failed, review Builder 1 alone.\n"
            "- Include 'confidence' (1-10) and 'observations' array in JSON output."
        ),
    },
    "merger": {
        "instructions": (
            "You are an expert code merger and integration engineer.\n"
            "Your mission is to combine outputs from multiple parallel builders into a single cohesive, working product WITHOUT losing any builder's contributions.\n\n"
            "## MERGE PROTOCOL\n"
            "1. **Inventory**: Use file_list to scan /tmp/evermind_output/ and identify all files from all builders. Read the planner's builder_ownership map if available.\n"
            "2. **Conflict detection**: Read each builder's files. Identify overlapping files (especially index.html, shared CSS/JS). Map which builder owns which pages/modules.\n"
            "3. **Integration strategy**: Decide the merge approach:\n"
            "   - If builders own separate pages: unify navigation and shared styles\n"
            "   - If builders share index.html: merge sections in order, deduplicate styles/scripts\n"
            "   - For games: one builder typically owns the runtime, the other owns assets/levels — integrate via the game's module system\n"
            "4. **Merge execution**: Write the final merged files using file_write. Ensure:\n"
            "   - All navigation links work across all pages\n"
            "   - Shared CSS is consolidated (no duplicate/conflicting class names)\n"
            "   - Shared JS is reconciled (no duplicate globals, event listeners)\n"
            "   - All builder content sections are preserved\n"
            "5. **Integrity verification**: After merging, verify with bash or file_read that:\n"
            "   - All HTML files have proper structure (DOCTYPE to /html)\n"
            "   - JS has balanced braces\n"
            "   - CSS selectors don't clash\n"
            "   - Cross-page links resolve correctly\n\n"
            "## HARD RULES\n"
            "1. NEVER discard a builder's work — every builder's content must appear in the final merge\n"
            "2. NEVER create a fresh rewrite that ignores builder outputs\n"
            "3. When builders produce competing index.html files, merge both sets of content into one coherent page or establish proper routing\n"
            "4. Preserve each builder's strongest design decisions (color palette, layout, animations)\n"
            "5. Shared CSS variables must be unified, not duplicated with different values\n"
            "6. Shared JavaScript must tolerate missing optional DOM elements (null-guard queries)\n"
            "7. For multi-page sites, create a unified navigation bar/menu that covers all pages from all builders\n"
            "8. For games, maintain the primary builder's runtime loop and integrate the secondary builder's assets/modules into it\n"
            "9. After merge, every page must load without console errors\n"
            "10. If builders used different font stacks or color themes, choose the stronger one and adapt the other\n\n"
            "## OUTPUT FORMAT\n"
            "Your output must include:\n"
            "- **Merge Strategy**: How you combined the builder outputs\n"
            "- **Files Merged**: List of source files and how they were combined\n"
            "- **Conflicts Resolved**: Any naming/style/logic conflicts and how you resolved them\n"
            "- **Final File Inventory**: Complete list of files in the merged output\n"
            "- **Navigation Map**: How pages link together\n\n"
            "## EFFICIENCY PROTOCOL (v3.5.1 — GStack pipeline discipline)\n"
            "IRON RULES:\n"
            "1. READ-THEN-WRITE: Maximum 2 read calls, then write. No analysis paralysis.\n"
            "2. PRIMARY IS BASE: Builder-1's output is the foundation. Inject Builder-2's modules into it. Never rebuild.\n"
            "3. SKIP EMPTY SHELLS: If Builder-2's files are < 50 lines or only contain stubs, discard them.\n"
            "4. FAIL-GRACEFUL: If Builder-2 failed or produced nothing, ship Builder-1's output with minor cleanup.\n"
            "5. THREE MINUTE RULE: Total execution < 3 minutes. If reading takes > 60s, you're over-analyzing.\n\n"
            "## MERGE STRATEGY (structured handoff from OMC patterns)\n"
            "Before writing, produce a mental merge plan:\n"
            "- List modules from Builder-1: [module name → function count → status: keep/modify]\n"
            "- List modules from Builder-2: [module name → function count → status: inject/discard]\n"
            "- Identify conflicts: [variable name collisions, CSS selector clashes, duplicate DOM IDs]\n"
            "- Resolution for each conflict: [rename/namespace/merge/discard]\n"
            "Then execute the merge in a single write pass.\n\n"
            "## QUALITY BAR\n"
            "- The merged output must be visually consistent — it should look like one unified product, not a stitched patchwork\n"
            "- All interactive elements from all builders must work\n"
            "- Merged CSS must not produce layout breaks, z-index wars, or font mismatches\n"
            "- After merge, trace the execution path: page loads → init functions fire → game loop starts → all systems connected\n"
            "- The final product must pass the same quality bar as a single-builder output\n"
            "\n\n## MERGE INVENTORY PROTOCOL (v4.0)\n"
            "Before any merge:\n"
            "1. List EVERY file from Builder 1 (name, size, key functions).\n"
            "2. List EVERY file from Builder 2 (name, size, key functions).\n"
            "3. List ALL conflicts (file overlaps, CSS class collisions, JS global collisions).\n"
            "4. For each conflict: resolution strategy + confidence 1-10.\n"
            "After merge: verify each builder's key features still work by tracing execution paths."
        ),
    },
    "deployer": {
        "instructions": (
            "You are a deployment and delivery verification specialist.\n"
            "Your mission is to confirm that all generated artifacts are correctly structured, complete, and ready for preview.\n\n"
            "## VERIFICATION PROTOCOL\n"
            "1. **File inventory**: Use file_list on /tmp/evermind_output/ to enumerate all generated files. Verify every expected file from the plan exists.\n"
            "2. **Structure validation**: For each HTML file, verify DOCTYPE, <html>, <head>, <body>, and </html> closing tags exist. Check that referenced CSS/JS files are present.\n"
            "3. **Asset integrity**: Verify that referenced local images, fonts, and scripts exist. Flag any broken local references.\n"
            "4. **Cross-reference check**: For multi-page sites, verify all inter-page links point to existing files. Check that shared CSS/JS paths are correct.\n"
            "5. **Preview URL mapping**: Generate the correct local preview URL for each file.\n\n"
            "## HARD RULES\n"
            "1. Do NOT attempt to deploy to GitHub Pages, Netlify, Vercel, or any external service\n"
            "2. Do NOT modify any files — you are a read-only verifier\n"
            "3. Report missing files, broken references, and structural issues precisely\n"
            "4. Flag any files that appear to be stubs, placeholders, or incomplete\n"
            "5. Verify file sizes are reasonable (HTML > 500 bytes, not empty stubs)\n\n"
            "## OUTPUT FORMAT (strict JSON)\n"
            '{"status": "deployed" or "issues_found", '
            '"preview_url": "http://127.0.0.1:8765/preview/index.html", '
            '"files": [{"path": "...", "type": "html|css|js|asset", "size_bytes": N, "valid": true}], '
            '"page_map": [{"page": "index.html", "links_to": ["about.html", "..."]}], '
            '"issues": ["specific issue if any"], '
            '"missing_files": ["expected but not found"], '
            '"broken_references": ["file.html references missing.css"]}\n'
        ),
    },
    "debugger": {
        "instructions": (
            "You are an elite debugging and root-cause-analysis engineer.\n"
            "Your mission is to diagnose failures, map the fault path through actual code, and produce the smallest coherent fix that restores correctness WITHOUT destroying working functionality.\n\n"
            "## DIAGNOSTIC PROTOCOL\n"
            "1. **Triage**: Read the error message, stack trace, and reviewer/tester rejection notes carefully. Classify the fault: runtime crash, logic error, layout regression, missing asset, build failure, or interaction break.\n"
            "2. **Evidence collection**: Use file_read to inspect the exact files and line ranges mentioned in the error. Use grep_search to locate related patterns (function defs, variable names, CSS classes). Limit reads to the minimum needed — do NOT read the entire codebase.\n"
            "3. **Root-cause isolation**: Identify the exact causal chain. Map the data flow from the failing symptom back to the root fault. Document which module/function/line is the source and which are downstream effects.\n"
            "4. **Fix implementation**: Write the minimum viable patch using file_write. Preserve ALL working functionality: gameplay loops, navigation, CSS themes, animation timings, and content structure.\n"
            "5. **Verification**: After writing, use bash to run any available verification commands (e.g. node --check, python -c import, grep for syntax markers). Confirm balanced braces/tags in HTML/JS.\n\n"
            "## HARD RULES\n"
            "1. NEVER replace a working artifact with a fresh rewrite unless it is irreparably broken — patch in place\n"
            "2. NEVER introduce new dependencies or change the tech stack to fix a bug\n"
            "3. When fixing HTML/JS, return the FULL file from <!DOCTYPE html> to </html>; do NOT output fragments\n"
            "4. Before every save, self-check inline JS for balanced braces/parens, closed callback tails, and no undefined references\n"
            "5. For CSS issues, inspect computed specificity and cascade order before overriding\n"
            "6. For game bugs, test-play the fix path mentally: init → render → update → input → collision → state\n"
            "7. When an existing repository context is injected, use the repo map to choose files deliberately instead of wandering\n"
            "8. If multiple bugs exist, fix them in dependency order (data model → logic → rendering → UI)\n"
            "9. Treat reviewer rejection notes as mandatory requirements — every listed issue must be addressed\n"
            "10. Document what you fixed and why in your output so the reviewer can verify\n\n"
            "## OUTPUT FORMAT\n"
            "Your output must include:\n"
            "- **Root Cause**: 1-3 sentences explaining the actual fault\n"
            "- **Fix Applied**: What you changed and why\n"
            "- **Files Modified**: List of files with brief change descriptions\n"
            "- **Verification**: Evidence that the fix is correct\n"
            "- **Risk Assessment**: Any side-effect risks from the fix\n"
            "\n\n## ROOT CAUSE ENUMERATION (v4.0)\n"
            "List >= 3 possible causes, ranked by likelihood.\n"
            "Format: trigger → root cause → fix.\n"
            "Fix confidence < 6: document uncertainty, suggest verification."
        ),
    },
    "analyst": {
        "instructions": (
            "You are a research analyst for product, UX, and design tasks.\n"
            "CRITICAL: Do NOT write HTML, CSS, or JavaScript code. Do NOT use file_ops write to create code files.\n"
            "Your ONLY output is a structured TEXT research report with XML-tagged sections.\n"
            "Do NOT attempt to build the website or generate page content — that is the builder's job.\n"
            "When the task asks for references, inspiration, competitors, trends, or design analysis, "
            "you MUST use source_fetch or the browser tool to gather evidence, but prioritize implementation-friendly sources first.\n"
            "Use source_fetch first for GitHub/blob/raw source files, docs pages, README files, and fast technical writeups; "
            "use browser for visual/UI inspection or when a page needs JS rendering/interaction.\n"
            "Prefer GitHub repos, source trees, README files, tutorials, docs, implementation guides, devlogs, and postmortems.\n"
            "Use live product websites only as supporting visual evidence, not as the primary research set.\n"
            "Prefer browser observe/extract for initial inspection, and use browser act only when interaction is required.\n"
            "Visit up to 5 distinct URLs for thorough research. Prioritize quality over quantity — each URL should provide actionable implementation-grade information.\n"
            "Those URLs should favor GitHub/source/docs/tutorial pages before live product websites.\n"
            "If a site is blocked by captcha, login wall, or bot-detection, skip it immediately and try another URL.\n"
            "Always include the visited URLs in your final report before the analysis summary.\n"
            "Research deeply — do not stop after a single source. A thorough analyst produces dramatically better downstream results.\n"
            "For game tasks, do NOT browse playable web games as your main research flow. Prefer GitHub repos, source code, tutorials, docs, devlogs, and postmortems.\n"
            "For browser game tasks, prioritize implementation-grade open-source references such as three.js examples, pmndrs/postprocessing, donmccurdy/three-pathfinding, Mugen87/yuka, and Kenney asset packs when they fit the requested genre.\n"
            "When you cite source code, prefer exact repo/file anchors or raw source URLs over vague project-level mentions.\n"
            "For game tasks, include at least one controller/combat source repo and one permissive asset/material source, and allocate 2-4 exact URLs or repo/file anchors per builder handoff when multiple builders exist.\n"
            "If a site's terms or anti-bot policy make automated crawling inappropriate, do NOT use it as a crawl target; prefer GitHub/docs pages or cite the official asset page sparingly without bulk scraping.\n"
            "You are optimizing the next nodes' execution quality, not writing a vague inspiration memo.\n"
            "Prefer a mixed evidence set: source repo(s), technical docs/tutorials, and visual/product references.\n"
            "Translate research into concrete downstream constraints, implementation advice, and review criteria.\n"
            "\nFEASIBILITY REVIEW (v3.5):\n"
            "Before writing your handoff, critically evaluate the plan:\n"
            "- Flag any feature that is IMPOSSIBLE to implement in a single HTML file (e.g., server-side database, real multiplayer, native mobile APIs)\n"
            "- For each impossible feature, provide a FEASIBLE ALTERNATIVE that achieves similar user value\n"
            "- If the plan asks for 3D with complex physics, specify exactly which Three.js features to use and which to skip\n"
            "- Rate each planned feature as: EASY / MODERATE / HARD / INFEASIBLE, and redistribute builder workload accordingly\n"
            "- Your builder handoffs must contain SPECIFIC code patterns, API calls, and architecture decisions — not just 'use Three.js'\n"
            "\nBUILDER HANDOFF QUALITY (v3.5):\n"
            "Each <builder_N_handoff> section MUST include:\n"
            "- Exact modules/systems this builder owns (file names if multi-file)\n"
            "- 2-3 reference code snippets from your research (copy actual patterns, not pseudocode)\n"
            "- Specific API calls to use (e.g., 'new THREE.PerspectiveCamera(75, aspect, 0.1, 1000)')\n"
            "- Anti-patterns to avoid (common mistakes you found in research)\n"
            "- Estimated complexity and time budget guidance\n"
            "Your report is not freeform. It must contain downstream execution handoffs using exact XML tags:\n"
            "<reference_sites>, <design_direction>, <non_negotiables>, <deliverables_contract>, <risk_register>, "
            "<builder_1_handoff>, <builder_2_handoff>, <reviewer_handoff>, "
            "<tester_handoff>, <debugger_handoff>.\n"
            "For game tasks, you MUST also include <reference_code_snippets>, <game_mechanics_spec>, <control_frame_contract>, and <asset_sourcing_plan>.\n"
            "Use those tags to optimize the next nodes' prompts. "
            "Enforce a premium quality bar and explicitly ban emoji glyphs inside generated pages.\n"
            "After browser research, produce your report as plain text content (not via file_ops). "
            "Write a comprehensive, detailed report — your research quality directly determines the final product quality."
            "\n\n## EXHAUSTIVE SURVEY (v4.0)\n"
            "List ALL options per dimension before ranking. Add <proactive_discoveries> for unsolicited findings.\n"
            "Each builder handoff must include feasibility confidence 1-10."
        ),
    },
    "scribe": {
        "instructions": (
            "You are a senior technical writer and content architect.\n"
            "Your mission is to produce structured, implementation-ready documentation that downstream nodes can execute directly.\n\n"
            "## SCRIBE PROTOCOL\n"
            "1. **Scope analysis**: Read the goal and understand what type of documentation is needed: user manual, API docs, content architecture, copy deck, or technical specification.\n"
            "2. **Structure first**: Always start with a clear information architecture: sections, headings, hierarchy, and reading flow.\n"
            "3. **Content delivery**: Fill every section with production-quality content — no TODOs, no placeholders, no lorem ipsum.\n\n"
            "## CONTENT TYPES & RULES\n"
            "### Content Architecture Handoff (for build workflows)\n"
            "- Output page-by-page content structure with exact section names\n"
            "- Define CTA copy, heading hierarchy, and content priorities per page\n"
            "- Include character/word count targets for key sections\n"
            "- Specify tone, voice, and terminology standards\n"
            "- Do NOT write HTML, CSS, or JavaScript — that is the builder's job\n\n"
            "### Documentation / Manual\n"
            "- Use clear headings, numbered steps, and consistent formatting\n"
            "- Include code examples where appropriate\n"
            "- Add cross-references between related sections\n"
            "- Include a glossary for domain-specific terms\n\n"
            "### Copy Deck\n"
            "- Deliver final copy for every UI surface: headlines, subheads, body, CTAs, tooltips, error messages\n"
            "- Flag any copy that requires client review or legal approval\n"
            "- Ensure consistent tone across all touchpoints\n\n"
            "## HARD RULES\n"
            "1. NEVER write code (HTML/CSS/JS) unless the task explicitly requests source code\n"
            "2. Keep output compact and actionable — no filler prose\n"
            "3. Prefer structured formats: tables, checklists, decision matrices\n"
            "4. Every section must have real content — mark clearly if something needs client input\n"
            "5. Respect word/character limits when specified\n"
            "6. Document assumptions and decisions that impact other nodes\n"
            "7. Use the builder's expected file structure when defining content placement\n\n"
            "## OUTPUT FORMAT\n"
            "Use markdown with clear section headers. Include:\n"
            "- **Content Map**: Section-by-section inventory\n"
            "- **Copy Matrix**: All text content organized by page/section\n"
            "- **Style Guide Notes**: Tone, terminology, formatting rules\n"
            "- **Builder Integration Notes**: Where content maps to specific files/components\n"
        ),
    },
    "uidesign": {
        "instructions": (
            "You are a senior UI/UX designer and design system architect.\n"
            "Your mission is to produce a comprehensive, implementation-ready design specification that builders can execute directly without guessing.\n\n"
            "## DESIGN PROTOCOL\n"
            "1. **Research**: If browser or web_fetch tools are available, inspect 2-3 reference sites relevant to the project category. Extract color palettes, layout patterns, typography scales, and interaction paradigms.\n"
            "2. **Design system definition**: Establish the foundational tokens before any component design.\n"
            "3. **Component specification**: Define every UI component with visual and behavioral detail.\n"
            "4. **Interaction mapping**: Document all state transitions, animations, and user flows.\n\n"
            "## DESIGN SYSTEM DELIVERABLES\n"
            "### Color Palette\n"
            "- Primary, secondary, and accent colors with exact hex/HSL values\n"
            "- Surface/background hierarchy (at least 3 levels)\n"
            "- Text color hierarchy (primary, secondary, muted)\n"
            "- Status colors (success, warning, error, info)\n"
            "- Dark mode adaptation rules if applicable\n\n"
            "### Typography Scale\n"
            "- Font stack recommendation (with system fallbacks)\n"
            "- Heading sizes (h1-h6) with line heights and weights\n"
            "- Body text sizes and reading line lengths\n"
            "- Code/monospace sizing\n\n"
            "### Spacing & Layout\n"
            "- Base spacing unit and multipliers (4px/8px grid)\n"
            "- Container max-widths and padding rules\n"
            "- Section spacing rhythm\n"
            "- Breakpoint definitions for responsive design\n\n"
            "### Component Behaviors\n"
            "- Button states: default, hover, active, disabled, loading\n"
            "- Card patterns: shadow, border-radius, hover lift\n"
            "- Form elements: input focus, validation states\n"
            "- Navigation: desktop, tablet, mobile patterns\n"
            "- Modal/overlay transitions\n\n"
            "### Motion Design\n"
            "- Easing curve specifications (entry, exit, emphasis)\n"
            "- Duration standards (micro: 100-200ms, macro: 300-500ms)\n"
            "- Scroll-triggered reveal patterns\n"
            "- Hover/interaction micro-animations\n"
            "- Loading state transitions\n\n"
            "## HARD RULES\n"
            "1. Do NOT write production HTML/CSS/JS — focus on direction and constraints\n"
            "2. Every color, spacing, and typography value must be concrete (exact hex, px, rem) — no vague descriptions\n"
            "3. Include visual hierarchy annotations: what draws the eye first, second, third\n"
            "4. Specify responsive behavior for at least 3 breakpoints (mobile, tablet, desktop)\n"
            "5. All interactive elements must have defined hover/focus/active states\n"
            "6. Motion must respect prefers-reduced-motion\n"
            "7. Contrast ratios must meet WCAG AA minimum (4.5:1 for text, 3:1 for large text)\n"
            "8. Do NOT use emoji as UI elements — recommend inline SVG icons\n\n"
            "## OUTPUT FORMAT\n"
            "Use structured markdown with:\n"
            "- **Design Tokens**: Complete CSS variable list\n"
            "- **Component Specs**: Per-component visual specification\n"
            "- **Layout Blueprint**: Page structure with grid/flex annotations\n"
            "- **Motion Contract**: Animation specifications for builder\n"
            "- **Responsive Rules**: Breakpoint-specific adaptations\n"
            "- **Builder Constraints**: Things the builder MUST and MUST NOT do\n"
        ),
    },
    "imagegen": {
        "instructions": (
            "You are a game-asset and image-direction specialist.\n"
            "Produce production-ready asset briefs, prompt packs, shot lists, visual constraints, and builder-facing replacement guidance.\n"
            "For game tasks, prioritize implementation-ready character / monster / weapon / environment design over poster-only output.\n"
            "For 3D game tasks, land a minimum viable replacement pack first: 00_visual_target.md, 01_style_lock.md, manifest.json, character_hero_brief.md, monster_primary_brief.md, weapon_primary_brief.md, and environment_kit_brief.md.\n"
            "Do not create optional companion docs, extra variants, or long shortlists until that core pack is complete and substantive.\n"
            "If the comfyui plugin is available, call it to check health first and generate assets when the pipeline is configured.\n"
            "If no image tool is attached or the generation backend is unavailable, do NOT pretend to render images.\n"
            "Instead return modeling briefs, silhouette / topology guidance, material notes, rig-or-animation needs, open-source asset shortlist notes,\n"
            "negative prompts when helpful, and fallback directions that builder/spritesheet/assetimport can execute immediately.\n"
            "When source_fetch or browser is available, use source_fetch first on permissive asset/model libraries, GitHub READMEs, or docs pages so your pack contains exact file/page anchors; use browser only when a page needs interactive inspection or source_fetch fails.\n"
            "Gather only a small licensed-source shortlist from permissive libraries such as Kenney, Quaternius, ambientCG, clearly licensed OpenGameArt entries, or GitHub-hosted sample-asset repos with explicit license notes.\n"
            "Do not bulk-scrape asset marketplaces or sites whose terms make automated AI ingestion inappropriate; keep asset research to a few direct human-readable pages.\n"
            "Never suggest copyrighted game IP scraping or unlicensed model/image reuse."
        ),
    },
    "spritesheet": {
        "instructions": (
            "You are a game asset pipeline specialist. Plan sprite families, frame states, palette constraints, and export layout.\n"
            "Output ONLY compact JSON with asset_families, animation_states, palette_constraints, export_layout,\n"
            "frame_counts, style_lock_tokens, and builder_replacement_rules. No prose, no file writes, no speculative extras."
        ),
    },
    "assetimport": {
        "instructions": (
            "You are an asset pipeline coordinator. Organize imported assets, naming, folder structure, usage mapping, and handoff notes.\n"
            "Output ONLY compact JSON with naming_rules, folder_structure, manifest_fields, runtime_mapping,\n"
            "replacement_keys, source_candidates, license_matrix, download_strategy, and builder_integration_notes. No prose, no file writes, no speculative extras."
        ),
    },
}


class AIBridge:
    """
    Unified AI execution engine with LiteLLM for 100+ model support.
    3 execution paths:
      1. CUA Responses Loop (GPT-5.4 computer use)
      2. LiteLLM function calling (any model with tools)
      3. LiteLLM direct chat (models without tool support)
    """

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self._openai_client = None
        self._openai_api_key = None
        self._compat_gateway_health: Dict[str, Dict[str, Any]] = {}
        self._provider_auth_health: Dict[str, Dict[str, Any]] = {}
        self._setup_litellm()
        self._seed_provider_auth_health_from_recent_logs()
        self._seed_compatible_gateway_health_from_recent_logs()

        # ── v3.0: Agentic Runtime ──
        self._retry_strategy = get_retry_strategy() if _AGENTIC_RUNTIME_AVAILABLE else None
        self._connection_pool: Optional[ConnectionPoolManager] = None
        self._agentic_events: List[Dict[str, Any]] = []  # Buffer for current execution
        # v3.0: Shared aiohttp session for persistent HTTP connection pooling
        # Avoids new TCP+TLS connection per API call (~200ms saved per request)
        self._shared_http_session: Optional[Any] = None
        self._session_lock = asyncio.Lock() if asyncio else None
        # V4.2 OPT: Cache OpenAI clients by (api_key, base_url) to reuse TCP+TLS connections
        self._openai_client_cache: Dict[str, Any] = {}

    def _get_or_create_openai_client(self, api_key: str, base_url: str, extra_headers: Optional[Dict] = None, timeout: float = 120) -> "OpenAI":
        """Return a cached OpenAI client for the given (key, base_url) pair.
        Reuses TCP+TLS connections across calls (~100-300ms saved per request).
        Uses HTTP/2 when httpx[http2] is available for connection multiplexing.

        V4.3: Added max_retries=3 (SDK handles 429/5xx with exponential backoff),
        expanded connection pool limits, and keepalive for long-running streams.
        """
        from openai import OpenAI as _OpenAI
        cache_key = f"{api_key[:20]}:{base_url}"
        client = self._openai_client_cache.get(cache_key)
        if client is None:
            http_client = None
            try:
                import httpx as _httpx
                # V4.3: Tuned pool limits for concurrent node execution.
                # max_connections=40 supports parallel builders + other nodes.
                # max_keepalive=20 keeps warm connections for reuse.
                # keepalive_expiry=120 matches typical node idle gap.
                pool_limits = _httpx.Limits(
                    max_connections=40,
                    max_keepalive_connections=20,
                    keepalive_expiry=120,
                )
                http_client = _httpx.Client(
                    http2=True,
                    timeout=_httpx.Timeout(timeout, connect=30.0),
                    limits=pool_limits,
                )
            except (ImportError, Exception):
                pass

            kwargs: Dict[str, Any] = {
                "api_key": api_key,
                "base_url": base_url,
                "default_headers": extra_headers or {},
                "timeout": timeout,
                # V4.3: SDK-level retry — handles 429/500/502/503/504 with
                # exponential backoff (0.5s, 1s, 2s).  Saves a full
                # orchestrator-level retry cycle (~30-60s) on transient errors.
                "max_retries": 3,
            }
            if http_client is not None:
                kwargs["http_client"] = http_client
            client = _OpenAI(**kwargs)
            self._openai_client_cache[cache_key] = client
            logger.info(
                "Created OpenAI client: base_url=%s http2=%s max_retries=3 pool=40/20",
                base_url,
                http_client is not None,
            )
        else:
            # Update timeout for this call (timeout varies per node type)
            client.timeout = timeout
        return client

    def _invalidate_openai_client(self, api_key: str, base_url: str, reason: str = "") -> None:
        """Remove a cached OpenAI client after persistent connection failures.

        V4.3 connection self-healing: on repeated ConnectionError / transport
        failures, evict the stale client so the next call creates a fresh one
        with a new TCP+TLS handshake.

        NOTE: We intentionally do NOT close the old client's transport here.
        Concurrent streams in worker threads may still hold references to
        the evicted client. Let Python GC reclaim it once all in-flight
        requests finish naturally.
        """
        cache_key = f"{api_key[:20]}:{base_url}"
        old = self._openai_client_cache.pop(cache_key, None)
        if old is not None:
            logger.warning(
                "Invalidated OpenAI client cache: key=%s reason=%s",
                cache_key,
                reason[:200] if reason else "unknown",
            )

    def _setup_litellm(self):
        """Configure LiteLLM with API keys from config/env."""
        try:
            import litellm
            litellm.set_verbose = False
            key_map = {
                "openai_api_key": "OPENAI_API_KEY",
                "anthropic_api_key": "ANTHROPIC_API_KEY",
                "gemini_api_key": "GEMINI_API_KEY",
                "deepseek_api_key": "DEEPSEEK_API_KEY",
                "kimi_api_key": "KIMI_API_KEY",
                "qwen_api_key": "QWEN_API_KEY",
            }
            for config_key, env_key in key_map.items():
                if config_key in self.config:
                    value = self.config.get(config_key, "")
                    if value:
                        os.environ[env_key] = value
                    else:
                        os.environ.pop(env_key, None)
            self._openai_client = None
            self._openai_api_key = None
            self._litellm = litellm
            logger.info("LiteLLM initialized — 100+ models available")
        except ImportError:
            self._litellm = None
            logger.warning("LiteLLM not installed, falling back to direct API calls")

    # ── Quick one-shot completion for lightweight tasks (report writing, etc.) ──

    def quick_completion(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str = "kimi-coding",
        max_tokens: int = 1500,
        timeout_sec: int = 30,
        fallback_models: Optional[List[str]] = None,
    ) -> str:
        """Synchronous one-shot completion call for lightweight tasks.

        Returns the assistant message text, or empty string on any failure.
        Designed for non-critical tasks like report writing — failures are
        silently swallowed so they never break the main pipeline.

        If fallback_models is provided and the primary model fails, each
        fallback is tried in order before returning empty.
        """
        if not self._litellm:
            return ""
        chain = [model] + (fallback_models or [])
        for candidate in chain:
            model_info = MODEL_REGISTRY.get(candidate)
            if not model_info:
                continue
            litellm_id = model_info.get("litellm_id", candidate)
            messages: List[Dict[str, str]] = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            kwargs: Dict[str, Any] = {
                "model": litellm_id,
                "messages": messages,
                "max_tokens": max_tokens,
                "timeout": timeout_sec,
                "num_retries": 0,
            }
            if model_info.get("api_base"):
                kwargs["api_base"] = model_info["api_base"]
            if model_info.get("extra_headers"):
                kwargs["extra_headers"] = model_info["extra_headers"]
            resolved_key = self._resolved_api_key_for_model_info(model_info)
            if resolved_key:
                kwargs["api_key"] = resolved_key
            # V4.4 FIX: kimi-k2.5 defaults to thinking mode, which consumes
            # max_tokens on reasoning and returns empty content.  Disable
            # thinking for lightweight one-shot calls like speed tests.
            _provider = str(model_info.get("provider") or "").lower()
            if _provider == "kimi":
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                # Ensure at least 20 tokens for content after any overhead
                if kwargs["max_tokens"] < 20:
                    kwargs["max_tokens"] = 20
            try:
                kwargs["stream"] = True
                response = self._litellm.completion(**kwargs)
                content_parts = []
                for chunk in response:
                    if chunk.choices and chunk.choices[0].delta:
                        c = getattr(chunk.choices[0].delta, "content", None)
                        if c:
                            content_parts.append(c)
                result = "".join(content_parts).strip()
                if result:
                    return result
            except Exception as exc:
                logger.debug("quick_completion(%s) failed: %s", candidate, str(exc)[:200])
                continue
        return ""

    # ── Structured Output Validation (ported from Pydantic AI patterns) ──

    @staticmethod
    def validate_structured_output(
        raw: str,
        schema: Dict[str, Any],
        *,
        partial_ok: bool = False,
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Validate AI output against a JSON schema.

        Ported from Pydantic AI's validation pattern: validates extracted JSON
        against a schema, returns (parsed_dict, violations). If partial_ok is
        True, missing required fields are downgraded to warnings instead of errors.

        Returns:
            (parsed_dict, violations) — parsed_dict may be partial on failure,
            violations is empty list on success.
        """
        # Step 1: Extract JSON from raw output
        text = str(raw or "").strip()
        parsed: Dict[str, Any] = {}
        if not text:
            return {}, ["Empty output — no JSON found"]

        # Try direct parse first, then search for embedded JSON
        candidate = text
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                candidate = text[start:end]
            else:
                return {}, ["No JSON object found in output"]
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as e:
            return {}, [f"JSON parse error: {str(e)[:100]}"]
        if not isinstance(parsed, dict):
            return {}, ["Parsed value is not a dict"]

        # Step 2: Validate against schema
        if not schema:
            return parsed, []

        violations: List[str] = []
        try:
            import jsonschema as _jsonschema
            validator_cls = _jsonschema.Draft7Validator
            validator = validator_cls(schema)
            for error in sorted(validator.iter_errors(parsed), key=lambda e: list(e.path)):
                path_str = ".".join(str(p) for p in error.path) or "(root)"
                msg = f"{path_str}: {error.message}"
                if partial_ok and error.validator == "required":
                    continue  # Allow missing fields in partial mode
                violations.append(msg[:200])
        except ImportError:
            pass  # jsonschema not available — skip validation
        except Exception as exc:
            violations.append(f"Validation error: {str(exc)[:100]}")

        return parsed, violations[:10]

    # ── Output Guardrails (ported from OpenAI Agents SDK patterns) ──

    def run_output_guardrails(
        self,
        content: str,
        node_type: str,
    ) -> List[Dict[str, str]]:
        """Run output guardrails on AI model response.

        Returns list of {severity, message} dicts. Severity is 'reject' or 'warn'.
        Empty list means all checks passed.
        """
        results: List[Dict[str, str]] = []
        if not content:
            return results
        content_len = len(content)

        # Guard 1: Length sanity — builder output < 50 chars is almost certainly broken
        if node_type in ("builder", "merger", "polisher") and content_len < 50:
            results.append({"severity": "reject", "message": f"Output too short ({content_len} chars) for {node_type}"})

        # Guard 2: Prompt echo detection — if output contains system prompt fragments
        _prompt_echoes = [
            "You are a senior project planner",
            "IMPORTANT RULES for website",
            "EXECUTION REPORT (v4.0",
            "## OPTION ENUMERATION",
        ]
        for echo in _prompt_echoes:
            if echo in content:
                results.append({"severity": "warn", "message": f"Possible prompt echo detected: '{echo[:40]}...'"})
                break

        # Guard 3: Empty code block — builder wrote code fence but nothing inside
        if node_type in ("builder", "merger") and "```" in content:
            import re as _re
            empty_blocks = _re.findall(r'```\w*\s*```', content)
            if len(empty_blocks) > 2:
                results.append({"severity": "warn", "message": f"Multiple empty code blocks ({len(empty_blocks)}) — possible generation failure"})

        return results

    # ── API Preflight Probe & Timeout Tracking (v4.1) ──

    def preflight_api_probe(
        self,
        models: Optional[List[str]] = None,
        timeout_sec: int = 8,
    ) -> Dict[str, Dict[str, Any]]:
        """Probe relay/provider APIs before a run starts.

        V4.3: Runs probes CONCURRENTLY via ThreadPoolExecutor instead of
        serially. With 3 models at 12s timeout each, serial probes wasted
        up to 36s before the first subtask could start.  Concurrent probes
        complete in max(individual_latency) ≈ 3-8s.

        Returns: {model_name: {"ok": bool, "latency_ms": int, "error": str}}
        """
        if models is None:
            models = ["kimi-coding", "gpt-5.4", "gpt-5.3-codex"]
        results: Dict[str, Dict[str, Any]] = {}

        def _probe_one(model_name: str) -> tuple:
            model_info = MODEL_REGISTRY.get(model_name)
            if not model_info:
                return model_name, {"ok": False, "latency_ms": 0, "error": "not in registry"}
            t0 = time.time()
            try:
                reply = self.quick_completion(
                    "回复OK", model=model_name, max_tokens=5, timeout_sec=timeout_sec,
                )
                latency_ms = int((time.time() - t0) * 1000)
                ok = len(reply) > 0
                result = {"ok": ok, "latency_ms": latency_ms, "error": "" if ok else "empty reply"}
                if ok:
                    logger.info("[Preflight] %s OK in %dms", model_name, latency_ms)
                else:
                    logger.warning("[Preflight] %s returned empty in %dms", model_name, latency_ms)
                return model_name, result
            except Exception as exc:
                latency_ms = int((time.time() - t0) * 1000)
                error_str = str(exc)[:200]
                logger.warning("[Preflight] %s failed in %dms: %s", model_name, latency_ms, error_str)
                self._record_gateway_timeout(model_info, error_str)
                return model_name, {"ok": False, "latency_ms": latency_ms, "error": error_str}

        # V4.3: concurrent probes — all models tested in parallel
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=min(len(models), 4)) as pool:
            futures = {pool.submit(_probe_one, m): m for m in models}
            for fut in _cf.as_completed(futures, timeout=timeout_sec + 5):
                try:
                    name, result = fut.result(timeout=2)
                    results[name] = result
                except Exception:
                    results[futures[fut]] = {"ok": False, "latency_ms": 0, "error": "probe thread failed"}
        return results

    def _record_gateway_timeout(self, model_info: Optional[Dict[str, Any]], error_message: str = "", node_type: str = "") -> None:
        """Record a timeout/slowness event for a gateway+model combo.

        V4.5: Node-type aware cooldown. Analyst/planner timeouts use shorter
        cooldown (30s) to avoid penalizing Builder which needs the same gateway.
        Builder/merger get 60s. Default 120s for unknown nodes.
        """
        info = model_info if isinstance(model_info, dict) else {}
        provider = str(info.get("provider") or "").strip().lower()
        if provider in ("relay", "ollama", ""):
            return
        model_key = self._compatible_gateway_key(info, model_specific=True)
        if not model_key:
            return
        state = self._compat_gateway_health.setdefault(model_key, {
            "consecutive_rejections": 0,
            "last_rejection_at": 0.0,
            "last_success_at": 0.0,
            "last_error": "",
            "rejection_cooldown_until": 0.0,
            "circuit_open_until": 0.0,
        })
        now = time.time()
        state["last_rejection_at"] = now
        state["last_error"] = _sanitize_error(str(error_message or "timeout"))[:200]
        # V4.5: Node-type aware cooldown — analyst timeout shouldn't block builder
        _nt = normalize_node_role(str(node_type or "").strip())
        if _nt in ("analyst", "planner", "router"):
            cooldown = 30.0
        elif _nt in ("builder", "polisher"):
            cooldown = 60.0
        else:
            cooldown = 120.0
        state["rejection_cooldown_until"] = max(
            float(state.get("rejection_cooldown_until") or 0.0),
            now + cooldown,
        )
        logger.info(
            "[GatewayTimeout] Marked %s unhealthy for %ds (node=%s): %s",
            model_key, int(cooldown), _nt or "unknown", state["last_error"][:100],
        )

    def _tail_compat_gateway_log_lines(self, max_lines: int = 1200) -> List[str]:
        path = Path(COMPAT_GATEWAY_LOG_FILE)
        if not path.exists() or not path.is_file():
            return []
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                return list(deque(handle, maxlen=max_lines))
        except Exception:
            return []

    def _seed_compatible_gateway_health_from_recent_logs(self) -> None:
        lines = self._tail_compat_gateway_log_lines()
        if not lines:
            return

        latest_states: Dict[str, Dict[str, Any]] = {}
        for raw_line in lines:
            line = str(raw_line or "").strip()
            if not line:
                continue
            ts = _log_timestamp_epoch(line)

            rejection_match = _GATEWAY_REJECTION_RE.search(line)
            if rejection_match:
                provider = str(rejection_match.group("provider") or "").strip().lower()
                host = str(rejection_match.group("host") or "").strip()
                latest_states[f"{provider}:{host}"] = {
                    "status": "rejection_cooldown",
                    "provider": provider,
                    "host": host,
                    "observed_at": ts,
                    "cooldown_sec": int(rejection_match.group("cooldown") or 0),
                    "last_error": _sanitize_error(str(rejection_match.group("error") or ""))[:200],
                }
                continue

            circuit_match = _GATEWAY_CIRCUIT_RE.search(line)
            if circuit_match:
                provider = str(circuit_match.group("provider") or "").strip().lower()
                host = str(circuit_match.group("host") or "").strip()
                latest_states[f"{provider}:{host}"] = {
                    "status": "circuit_open",
                    "provider": provider,
                    "host": host,
                    "observed_at": ts,
                    "failure_count": int(circuit_match.group("failures") or 0),
                    "last_error": _sanitize_error(str(circuit_match.group("error") or ""))[:200],
                }
                continue

            recovered_match = _GATEWAY_RECOVERED_RE.search(line)
            if recovered_match:
                provider = str(recovered_match.group("provider") or "").strip().lower()
                host = str(recovered_match.group("host") or "").strip()
                latest_states[f"{provider}:{host}"] = {
                    "status": "healthy",
                    "provider": provider,
                    "host": host,
                    "observed_at": ts,
                    "last_error": "",
                }

        if not latest_states:
            return

        now = time.time()
        provider_to_base: Dict[str, str] = {}
        for provider in ("openai", "anthropic", "google", "deepseek", "kimi", "qwen"):
            env_key = self._provider_api_base_env_key(provider)
            base = str(os.getenv(env_key, "") if env_key else "").strip().rstrip("/")
            if base:
                provider_to_base[provider] = base

        for provider, base in provider_to_base.items():
            try:
                parsed = urlparse(base)
                host = str(parsed.netloc or parsed.path or base).strip()
            except Exception:
                host = base
            if not host:
                continue
            observed = latest_states.get(f"{provider}:{host}") or {}
            status = str(observed.get("status") or "").strip()
            if not status or status == "healthy":
                continue

            key = f"{provider}:{base}"
            state = self._compat_gateway_health.setdefault(key, {
                "consecutive_failures": 0,
                "failure_count": 0,
                "success_count": 0,
                "circuit_open_until": 0.0,
                "rejection_cooldown_until": 0.0,
                "last_error": "",
                "last_failure_at": 0.0,
                "last_rejection_at": 0.0,
                "last_success_at": 0.0,
                "last_latency_ms": 0,
                "ewma_latency_ms": 0.0,
            })

            observed_at = float(observed.get("observed_at") or 0.0)
            if status == "rejection_cooldown":
                cooldown_sec = int(observed.get("cooldown_sec") or 0)
                rejection_until = observed_at + cooldown_sec
                state["last_rejection_at"] = max(float(state.get("last_rejection_at") or 0.0), observed_at)
                if str(observed.get("last_error") or "").strip():
                    state["last_error"] = str(observed.get("last_error") or "")
                if rejection_until > now:
                    state["rejection_cooldown_until"] = rejection_until
                    logger.info(
                        "Seeded compatible gateway rejection cooldown from recent logs: provider=%s host=%s remaining=%ss",
                        provider,
                        host,
                        max(1, int(round(rejection_until - now))),
                    )
                elif (now - observed_at) <= self._compatible_gateway_recent_rejection_grace_seconds():
                    logger.info(
                        "Seeded compatible gateway recent rejection from logs: provider=%s host=%s age=%ss",
                        provider,
                        host,
                        max(1, int(round(now - observed_at))),
                    )
            elif status == "circuit_open":
                circuit_until = observed_at + self._compatible_gateway_circuit_open_seconds()
                if circuit_until > now:
                    state["circuit_open_until"] = circuit_until
                    state["consecutive_failures"] = max(
                        int(observed.get("failure_count") or 0),
                        self._compatible_gateway_failure_threshold(),
                    )
                    state["last_failure_at"] = observed_at
                    state["last_error"] = str(observed.get("last_error") or "")
                    logger.info(
                        "Seeded compatible gateway circuit-open state from recent logs: provider=%s host=%s remaining=%ss",
                        provider,
                        host,
                        max(1, int(round(circuit_until - now))),
                    )

    def _seed_provider_auth_health_from_recent_logs(self) -> None:
        lines = self._tail_compat_gateway_log_lines()
        if not lines:
            return

        latest_states: Dict[str, Dict[str, Any]] = {}
        for raw_line in lines:
            line = str(raw_line or "").strip()
            if not line:
                continue
            ts = _log_timestamp_epoch(line)

            failure_match = _PROVIDER_AUTH_FAILURE_RE.search(line)
            if failure_match:
                provider = str(failure_match.group("provider") or "").strip().lower()
                if not provider or provider in {"unknown", "relay", "ollama"}:
                    continue
                latest_states[provider] = {
                    "status": "blocked",
                    "provider": provider,
                    "observed_at": ts,
                    "cooldown_sec": int(failure_match.group("cooldown") or 0),
                    "last_error": _sanitize_error(str(failure_match.group("error") or ""))[:200],
                }
                continue

            cleared_match = _PROVIDER_AUTH_CLEARED_RE.search(line)
            if cleared_match:
                provider = str(cleared_match.group("provider") or "").strip().lower()
                if not provider or provider in {"unknown", "relay", "ollama"}:
                    continue
                latest_states[provider] = {
                    "status": "healthy",
                    "provider": provider,
                    "observed_at": ts,
                }

        if not latest_states:
            return

        now = time.time()
        max_seed_age = self._provider_auth_seed_max_age_seconds()
        for provider, observed in latest_states.items():
            state = self._provider_auth_state(provider)
            if not state:
                continue

            status = str(observed.get("status") or "").strip().lower()
            observed_at = float(observed.get("observed_at") or 0.0)
            age = now - observed_at if observed_at > 0 else float("inf")
            if status == "healthy":
                if observed_at >= float(state.get("last_failure_at") or 0.0):
                    state["blocked_until"] = 0.0
                    state["last_error"] = ""
                    state["last_success_at"] = max(float(state.get("last_success_at") or 0.0), observed_at)
                continue

            if status != "blocked":
                continue
            if max_seed_age > 0 and age > max_seed_age:
                continue

            cooldown_sec = int(observed.get("cooldown_sec") or 0)
            blocked_until = observed_at + cooldown_sec
            state["last_failure_at"] = max(float(state.get("last_failure_at") or 0.0), observed_at)
            if str(observed.get("last_error") or "").strip():
                state["last_error"] = str(observed.get("last_error") or "")
            if blocked_until > now:
                # V4.2 FIX: Previously we cleared blocked_until whenever an API
                # key was present.  But key presence is the NORMAL case for
                # configured providers — it says nothing about whether the auth
                # failure that triggered the cooldown has been resolved.
                # Now we only shorten the remaining cooldown: cap it to 60s so
                # the provider gets a fast retry on next request, but do NOT
                # clear it entirely (which would cause an immediate retry storm
                # on startup against still-broken credentials).
                remaining = blocked_until - now
                _max_seeded = 60.0  # cap seeded cooldowns to 60s on restart
                if remaining > _max_seeded:
                    state["blocked_until"] = now + _max_seeded
                    logger.info(
                        "Capped seeded cooldown for provider=%s: %ss → %ss",
                        provider,
                        max(1, int(round(remaining))),
                        int(_max_seeded),
                    )
                else:
                    state["blocked_until"] = max(float(state.get("blocked_until") or 0.0), blocked_until)
                    logger.info(
                        "Seeded provider auth failure cooldown from recent logs: provider=%s remaining=%ss",
                        provider,
                        max(1, int(round(remaining))),
                    )

    async def _get_openai(self):
        api_key = self.config.get("openai_api_key") or os.getenv("OPENAI_API_KEY")
        if api_key and (not self._openai_client or api_key != self._openai_api_key):
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(api_key=api_key)
            self._openai_api_key = api_key
        return self._openai_client

    def get_available_models(self) -> List[Dict]:
        """Return list of available models including relay models."""
        models = [{"id": k, **v} for k, v in MODEL_REGISTRY.items()]
        # Add relay models
        relay_mgr = get_relay_manager()
        for model_id, info in relay_mgr.get_all_models().items():
            models.append({"id": model_id, **info})
        return models

    def _resolve_model(self, model_name: str) -> Dict:
        """Resolve model info from registry or relay endpoints."""
        # Check static registry first
        if model_name in MODEL_REGISTRY:
            return MODEL_REGISTRY[model_name]
        # Check relay models
        relay_mgr = get_relay_manager()
        relay_models = relay_mgr.get_all_models()
        if model_name in relay_models:
            return relay_models[model_name]
        # Fallback — treat as raw LiteLLM model ID
        return {"litellm_id": model_name, "supports_tools": True, "supports_cua": False}

    def _normalize_model_chain(self, raw_values: Any, *, limit: int = 6) -> List[str]:
        if isinstance(raw_values, str):
            values = [part.strip() for part in raw_values.split(",")]
        elif isinstance(raw_values, (list, tuple)):
            values = raw_values
        else:
            values = []

        normalized: List[str] = []
        seen: set[str] = set()
        for raw in values:
            model_name = str(raw or "").strip()
            if not model_name or model_name in seen:
                continue
            seen.add(model_name)
            normalized.append(model_name[:100])
            if len(normalized) >= limit:
                break
        return normalized

    def _normalized_node_model_preferences(self) -> Dict[str, List[str]]:
        raw_preferences = self.config.get("node_model_preferences", {})
        if not isinstance(raw_preferences, dict):
            return {}

        normalized: Dict[str, List[str]] = {}
        for raw_role, raw_chain in raw_preferences.items():
            role = normalize_node_role(str(raw_role or "").strip())
            if not role:
                continue
            chain = self._normalize_model_chain(raw_chain)
            if chain:
                normalized[role] = chain
        return normalized

    def _configured_thinking_depth(self) -> str:
        """Return 'fast' or 'deep' from config. Default: 'deep'."""
        cfg = getattr(self, "config", None)
        if isinstance(cfg, dict):
            raw = str(cfg.get("thinking_depth") or "").strip().lower()
            if raw in ("fast", "deep"):
                return raw
        env_val = os.getenv("EVERMIND_THINKING_DEPTH", "").strip().lower()
        if env_val in ("fast", "deep"):
            return env_val
        return "deep"

    def _provider_api_base_env_key(self, provider: str) -> str:
        return {
            "openai": "OPENAI_API_BASE",
            "anthropic": "ANTHROPIC_API_BASE",
            "google": "GEMINI_API_BASE",
            "deepseek": "DEEPSEEK_API_BASE",
            "kimi": "KIMI_API_BASE",
            "qwen": "QWEN_API_BASE",
            "zhipu": "ZHIPU_API_BASE",
            "doubao": "DOUBAO_API_BASE",
            "yi": "YI_API_BASE",
            "minimax": "MINIMAX_API_BASE",
        }.get(str(provider or "").strip().lower(), "")

    def _provider_api_key_env_key(self, provider: str) -> str:
        return {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GEMINI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "kimi": "KIMI_API_KEY",
            "qwen": "QWEN_API_KEY",
            "zhipu": "ZHIPU_API_KEY",
            "doubao": "DOUBAO_API_KEY",
            "yi": "YI_API_KEY",
            "minimax": "MINIMAX_API_KEY",
        }.get(str(provider or "").strip().lower(), "")

    @staticmethod
    def _model_supports_temperature(model_name: str) -> bool:
        """GPT-5 series (including codex) only accepts temperature=1."""
        name_lower = str(model_name or "").lower()
        # Strip provider prefix (e.g. "openai/gpt-5.3-codex" → "gpt-5.3-codex")
        if "/" in name_lower:
            name_lower = name_lower.rsplit("/", 1)[-1]
        return not name_lower.startswith("gpt-5")

    # V4.3 PERF: Per-node temperature — builder/reviewer use low temperature
    # for deterministic output (fewer retries, higher cache hit rate).
    _NODE_TEMPERATURE = {
        "builder": 0.3,
        "reviewer": 0.3,
        "planner": 0.5,
        "merger": 0.3,
        "polisher": 0.5,
        "analyst": 0.7,
        "debugger": 0.5,
        "tester": 0.5,
    }

    def _temperature_for_node(self, node_type: str, model_name: str) -> Optional[float]:
        """Return optimal temperature for this node type, or None if model doesn't support it."""
        if not self._model_supports_temperature(model_name):
            return None
        role = normalize_node_role(node_type) or str(node_type or "").lower()
        return self._NODE_TEMPERATURE.get(role, 0.5)

    def _resolved_api_base_for_model_info(self, model_info: Optional[Dict[str, Any]]) -> str:
        info = model_info if isinstance(model_info, dict) else {}
        provider = str(info.get("provider") or "").strip().lower()
        env_base_key = self._provider_api_base_env_key(provider)
        env_base = str(os.getenv(env_base_key, "") if env_base_key else "").strip()
        configured_base = str(info.get("api_base") or "").strip()
        return (env_base or configured_base).rstrip("/")

    def _resolved_api_key_for_model_info(self, model_info: Optional[Dict[str, Any]]) -> str:
        info = model_info if isinstance(model_info, dict) else {}
        provider = str(info.get("provider") or "").strip().lower()
        env_key = self._provider_api_key_env_key(provider)
        if not env_key:
            return ""
        # V4.3.1 FIX: Check top-level flat key (e.g. kimi_api_key), then
        # nested api_keys dict (e.g. config.api_keys.kimi), then env var.
        # Speed-test endpoint passes keys nested; websocket session flattens them.
        key = str(self.config.get(env_key.lower()) or "").strip()
        if not key:
            api_keys = self.config.get("api_keys")
            if isinstance(api_keys, dict):
                key = str(api_keys.get(provider) or "").strip()
        if not key:
            key = str(os.getenv(env_key) or "").strip()
        return key

    def _custom_compatible_gateway_base(self, model_info: Optional[Dict[str, Any]]) -> str:
        info = model_info if isinstance(model_info, dict) else {}
        provider = str(info.get("provider") or "").strip().lower()
        if provider in ("relay", "ollama", ""):
            return ""
        env_base_key = self._provider_api_base_env_key(provider)
        env_base = str(os.getenv(env_base_key, "") if env_base_key else "").strip().rstrip("/")
        if not env_base:
            return ""
        # A non-official base (e.g. private-relay.com) IS a custom gateway even when
        # model config and env var match — we need the streaming-capable path.
        _official = ("https://api.openai.com", "https://api.anthropic.com",
                     "https://api.deepseek.com", "https://generativelanguage.googleapis.com")
        if any(env_base.lower().startswith(o) for o in _official):
            return ""
        return env_base

    def _compatible_gateway_key(self, model_info: Optional[Dict[str, Any]], *, model_specific: bool = False) -> str:
        info = model_info if isinstance(model_info, dict) else {}
        base = self._custom_compatible_gateway_base(info)
        if not base:
            return ""
        provider = str(info.get("provider") or "").strip().lower() or "unknown"
        key = f"{provider}:{base}"
        if model_specific:
            litellm_id = str(info.get("litellm_id") or "").strip()
            if litellm_id:
                key = f"{key}:{litellm_id}"
        return key

    def _compatible_gateway_host(self, model_info: Optional[Dict[str, Any]]) -> str:
        base = self._custom_compatible_gateway_base(model_info)
        if not base:
            return ""
        try:
            parsed = urlparse(base)
            return parsed.netloc or parsed.path or base
        except Exception:
            return base

    def _fallback_gateway_bases(self, model_info: Optional[Dict[str, Any]]) -> List[str]:
        """Return alternative API base URLs for a model when the primary gateway is blocked."""
        info = model_info if isinstance(model_info, dict) else {}
        provider = str(info.get("provider") or "").strip().lower()
        # Per-model configured fallbacks
        configured = list(info.get("fallback_api_bases") or [])
        # Provider-level env fallback
        if provider:
            env_key = f"EVERMIND_{provider.upper()}_FALLBACK_API_BASE"
            env_val = str(os.getenv(env_key, "")).strip().rstrip("/")
            if env_val and env_val not in configured:
                configured.append(env_val)
        return [b for b in configured if b]

    def _compatible_gateway_error_trips_circuit(self, error_message: str) -> bool:
        error_lower = str(error_message or "").lower()
        if not error_lower:
            return True
        request_rejection_markers = (
            "your request was blocked",
            "request was blocked",
            "content policy",
            "safety system",
            "invalid_request_error",
            "invalid request",
            "model not found",
            "does not exist",
            "unsupported model",
            "context length",
            "maximum context",
            "prompt too long",
            "messages must",
            "tool schema",
            "unsupported input",
            "invalid image",
            "api key",
            "api_key",
            "invalid key",
            "not configured",
            "unauthorized",
            "forbidden",
            "401",
            "403",
            "authentication",
            "auth failed",
            "permission",
            # V4.2 FIX: Timeouts should NOT trip the circuit breaker.
            # A timeout means OUR budget is too short, not that the gateway
            # is broken.  gpt-5.4 produced 9001 chars before hard-ceiling
            # timeout — the gateway was clearly working.
            "timeout",
            "hard-ceiling timeout",
            "initial-activity timeout",
            "api call exceeded",
            "timed out",
        )
        return not any(marker in error_lower for marker in request_rejection_markers)

    def _compatible_gateway_failure_threshold(self) -> int:
        return self._read_int_env("EVERMIND_COMPAT_GATEWAY_FAILURE_THRESHOLD", 2, 1, 10)

    def _compatible_gateway_circuit_open_seconds(self) -> float:
        return self._read_float_env("EVERMIND_COMPAT_GATEWAY_CIRCUIT_OPEN_SEC", 45.0, 5.0, 300.0)

    def _compatible_gateway_rejection_cooldown_seconds(self) -> float:
        # P0 FIX 2026-04-04: 45s cooldown was too short — relay.cn rejections are
        # policy-based, not rate-limit-based, so they don't resolve in 45s.
        # V4.2: Lowered from 300s to 120s — 300s caused excessive downtime when
        # gateway issues are transient (e.g. private-relay.com intermittent 503s).
        return self._read_float_env("EVERMIND_COMPAT_GATEWAY_REJECTION_COOLDOWN_SEC", 120.0, 15.0, 1800.0)

    def _compatible_gateway_recent_rejection_grace_seconds(self) -> float:
        # Persist blocked-gateway memory across app restarts for much longer.
        # In practice, policy blocks on third-party compatible gateways often
        # remain for hours, not minutes, so a 1h grace period still causes the
        # next desktop launch to waste a fresh request before falling back.
        return self._read_float_env("EVERMIND_COMPAT_GATEWAY_RECENT_REJECTION_GRACE_SEC", 3600.0, 300.0, 172800.0)

    def _compatible_gateway_error_trips_rejection_cooldown(self, error_message: str) -> bool:
        error_lower = str(error_message or "").lower()
        if not error_lower:
            return False
        rejection_cooldown_markers = (
            "your request was blocked",
            "request was blocked",
            "blocked by",
            "content policy",
            "safety system",
            "relay - ai api gateway",
            "model not found",
            "does not exist",
            "unsupported model",
            "empty or invalid response from llm endpoint",
            "invalid response from llm endpoint",
            "received: '<!doctype html",
        )
        return any(marker in error_lower for marker in rejection_cooldown_markers)

    def _compatible_gateway_state(self, model_info: Optional[Dict[str, Any]]) -> tuple[str, Optional[Dict[str, Any]]]:
        key = self._compatible_gateway_key(model_info)
        if not key:
            return "", None
        state = self._compat_gateway_health.setdefault(key, {
            "consecutive_failures": 0,
            "failure_count": 0,
            "success_count": 0,
            "circuit_open_until": 0.0,
            "rejection_cooldown_until": 0.0,
            "last_error": "",
            "last_failure_at": 0.0,
            "last_rejection_at": 0.0,
            "last_success_at": 0.0,
            "last_latency_ms": 0,
            "ewma_latency_ms": 0.0,
        })
        return key, state

    def _compatible_gateway_preflight_error(self, model_info: Optional[Dict[str, Any]]) -> str:
        _key, state = self._compatible_gateway_state(model_info)
        if not state:
            return ""
        # P0 FIX 2026-04-04: Check model-specific rejection first — if gpt-5.4 is
        # policy-blocked but gpt-4o is fine, only gpt-5.4 should be blocked.
        model_key = self._compatible_gateway_key(model_info, model_specific=True)
        ms_state = self._compat_gateway_health.get(model_key) if model_key and model_key != _key else None
        if ms_state:
            ms_rejection_until = float(ms_state.get("rejection_cooldown_until") or 0.0)
            if ms_rejection_until > time.time():
                remaining = max(1, int(round(ms_rejection_until - time.time())))
                host = self._compatible_gateway_host(model_info) or "custom gateway"
                model_id = str((model_info or {}).get("litellm_id") or "")
                last_error = _sanitize_error(str(ms_state.get("last_error") or "")).strip()
                detail = f"; last error: {last_error}" if last_error else ""
                return f"compatible gateway rejection cooldown for {host} model={model_id} ({remaining}s remaining{detail})"
        rejection_cooldown_until = float(state.get("rejection_cooldown_until") or 0.0)
        now = time.time()
        if rejection_cooldown_until > now:
            remaining = max(1, int(round(rejection_cooldown_until - now)))
            host = self._compatible_gateway_host(model_info) or "custom gateway"
            last_error = _sanitize_error(str(state.get("last_error") or "")).strip()
            detail = f"; last error: {last_error}" if last_error else ""
            return f"compatible gateway rejection cooldown for {host} ({remaining}s remaining{detail})"
        circuit_open_until = float(state.get("circuit_open_until") or 0.0)
        if circuit_open_until <= now:
            return ""
        remaining = max(1, int(round(circuit_open_until - now)))
        host = self._compatible_gateway_host(model_info) or "custom gateway"
        last_error = _sanitize_error(str(state.get("last_error") or "")).strip()
        detail = f"; last error: {last_error}" if last_error else ""
        return f"compatible gateway circuit open for {host} ({remaining}s remaining{detail})"

    def _compatible_gateway_recent_unhealthy_reason(self, model_info: Optional[Dict[str, Any]]) -> str:
        preflight_error = self._compatible_gateway_preflight_error(model_info)
        if preflight_error:
            return preflight_error
        _key, state = self._compatible_gateway_state(model_info)
        if not state:
            return ""
        last_rejection_at = float(state.get("last_rejection_at") or 0.0)
        if last_rejection_at <= 0:
            # V4.2 FIX (Codex #1 follow-up): gateway-level last_rejection_at
            # may be unset when the rejection was model-specific.  Check the
            # model-specific health state so that e.g. gpt-5.4 is still
            # deprioritised after a model-specific rejection, without blocking
            # sibling models on the same gateway.
            model_key = self._compatible_gateway_key(model_info, model_specific=True)
            ms_state = self._compat_gateway_health.get(model_key) if model_key and model_key != _key else None
            if not ms_state:
                return ""
            ms_rejection_at = float(ms_state.get("last_rejection_at") or 0.0)
            if ms_rejection_at <= 0:
                return ""
            ms_success_at = float(ms_state.get("last_success_at") or 0.0)
            if ms_success_at >= ms_rejection_at:
                return ""
            age_sec = max(0.0, time.time() - ms_rejection_at)
            grace_sec = self._compatible_gateway_recent_rejection_grace_seconds()
            if age_sec > grace_sec:
                return ""
            host = self._compatible_gateway_host(model_info) or "custom gateway"
            model_id = str((model_info or {}).get("litellm_id") or "")
            age_text = max(1, int(round(age_sec)))
            last_error = _sanitize_error(str(ms_state.get("last_error") or "")).strip()
            detail = f"; last error: {last_error}" if last_error else ""
            return f"compatible gateway recent rejection on {host} model={model_id} ({age_text}s ago{detail})"
        last_success_at = float(state.get("last_success_at") or 0.0)
        if last_success_at >= last_rejection_at:
            return ""
        age_sec = max(0.0, time.time() - last_rejection_at)
        grace_sec = self._compatible_gateway_recent_rejection_grace_seconds()
        if age_sec > grace_sec:
            return ""
        host = self._compatible_gateway_host(model_info) or "custom gateway"
        age_text = max(1, int(round(age_sec)))
        last_error = _sanitize_error(str(state.get("last_error") or "")).strip()
        detail = f"; last error: {last_error}" if last_error else ""
        return f"compatible gateway recent rejection on {host} ({age_text}s ago{detail})"

    def _record_compatible_gateway_success(self, model_info: Optional[Dict[str, Any]], latency_ms: float = 0.0) -> None:
        _key, state = self._compatible_gateway_state(model_info)
        if not state:
            return
        recovered = bool(
            state.get("consecutive_failures")
            or state.get("circuit_open_until")
            or state.get("rejection_cooldown_until")
        )
        now = time.time()
        state["consecutive_failures"] = 0
        state["circuit_open_until"] = 0.0
        state["rejection_cooldown_until"] = 0.0
        state["success_count"] = int(state.get("success_count") or 0) + 1
        state["last_success_at"] = now
        state["last_error"] = ""
        if latency_ms > 0:
            latency_value = int(round(latency_ms))
            prev_ewma = float(state.get("ewma_latency_ms") or 0.0)
            state["last_latency_ms"] = latency_value
            state["ewma_latency_ms"] = (
                latency_value if prev_ewma <= 0 else round(prev_ewma * 0.7 + latency_value * 0.3, 1)
            )
        if recovered:
            logger.info(
                "Compatible gateway recovered: provider=%s host=%s latency_ms=%s ewma_ms=%s",
                str((model_info or {}).get("provider") or "").strip().lower() or "unknown",
                self._compatible_gateway_host(model_info) or "custom gateway",
                state.get("last_latency_ms", 0),
                state.get("ewma_latency_ms", 0),
            )

    def _record_compatible_gateway_failure(
        self,
        model_info: Optional[Dict[str, Any]],
        error_message: str,
        latency_ms: float = 0.0,
    ) -> None:
        _key, state = self._compatible_gateway_state(model_info)
        if not state:
            return
        now = time.time()
        state["failure_count"] = int(state.get("failure_count") or 0) + 1
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        state["last_failure_at"] = now
        state["last_error"] = _sanitize_error(str(error_message or "gateway error"))[:200]
        if latency_ms > 0:
            state["last_latency_ms"] = int(round(latency_ms))
        if self._compatible_gateway_error_trips_rejection_cooldown(error_message):
            cooldown_sec = self._compatible_gateway_rejection_cooldown_seconds()
            state["consecutive_failures"] = 0
            state["circuit_open_until"] = 0.0
            # P0 FIX 2026-04-13: Only write rejection cooldown to MODEL-SPECIFIC key,
            # NOT the gateway-level key. A gpt-5.4 "model not found" must NOT block
            # gpt-5.3-codex/gpt-5.4-mini on the same gateway for 300s.
            model_key = self._compatible_gateway_key(model_info, model_specific=True)
            if model_key and model_key != _key:
                ms_state = self._compat_gateway_health.setdefault(model_key, {
                    "consecutive_failures": 0, "failure_count": 0, "success_count": 0,
                    "circuit_open_until": 0.0, "rejection_cooldown_until": 0.0,
                    "last_error": "", "last_failure_at": 0.0, "last_rejection_at": 0.0,
                    "last_success_at": 0.0, "last_latency_ms": 0, "ewma_latency_ms": 0.0,
                })
                ms_state["rejection_cooldown_until"] = now + cooldown_sec
                ms_state["last_rejection_at"] = now
                ms_state["last_error"] = state["last_error"]
                # V4.2 FIX (Codex #1): Do NOT set gateway-level last_rejection_at
                # when the rejection is model-specific.  Otherwise sibling models
                # on the same gateway (e.g. gpt-5.4-mini after gpt-5.4 "model not
                # found") are incorrectly skipped by _next_fallback_candidate.
            else:
                # No model-specific key — fall back to gateway-level as before
                state["rejection_cooldown_until"] = now + cooldown_sec
                state["last_rejection_at"] = now
            logger.warning(
                "Compatible gateway rejection cooldown: provider=%s host=%s model=%s cooldown=%ss error=%s",
                str((model_info or {}).get("provider") or "").strip().lower() or "unknown",
                self._compatible_gateway_host(model_info) or "custom gateway",
                str((model_info or {}).get("litellm_id") or ""),
                int(round(cooldown_sec)),
                state.get("last_error", ""),
            )
            return
        if not self._compatible_gateway_error_trips_circuit(error_message):
            state["consecutive_failures"] = 0
            state["circuit_open_until"] = 0.0
            state["rejection_cooldown_until"] = 0.0
            logger.warning(
                "Compatible gateway request rejected without opening circuit: provider=%s host=%s error=%s",
                str((model_info or {}).get("provider") or "").strip().lower() or "unknown",
                self._compatible_gateway_host(model_info) or "custom gateway",
                state.get("last_error", ""),
            )
            return
        threshold = self._compatible_gateway_failure_threshold()
        host = self._compatible_gateway_host(model_info) or "custom gateway"
        if int(state.get("consecutive_failures") or 0) >= threshold:
            state["circuit_open_until"] = now + self._compatible_gateway_circuit_open_seconds()
            logger.warning(
                "Compatible gateway circuit OPEN: provider=%s host=%s failures=%s error=%s",
                str((model_info or {}).get("provider") or "").strip().lower() or "unknown",
                host,
                state.get("consecutive_failures", 0),
                state.get("last_error", ""),
            )
            return
        logger.warning(
            "Compatible gateway failure: provider=%s host=%s failures=%s/%s error=%s",
            str((model_info or {}).get("provider") or "").strip().lower() or "unknown",
            host,
            state.get("consecutive_failures", 0),
            threshold,
            state.get("last_error", ""),
        )

    def _legacy_model_fallback_chain(self, primary_model: str) -> List[str]:
        chain: List[str] = []
        seen: set[str] = set()
        for model_name in [str(primary_model or "").strip(), *LEGACY_AUTO_FALLBACK_ORDER]:
            normalized = str(model_name or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            chain.append(normalized)
        return chain

    def _provider_auth_failure_grace_seconds(self) -> float:
        # V4.4: Reduced from 600s to 90s — 600s was catastrophically long.
        # A single transient 401 (network hiccup, rate limit) would block an
        # entire provider for 10 minutes, killing every node in the pipeline.
        return self._read_float_env("EVERMIND_PROVIDER_AUTH_FAILURE_GRACE_SEC", 90.0, 15.0, 600.0)

    # V4.4: Minimum consecutive auth failures before triggering cooldown.
    # Prevents a single transient 401 from cascading into full provider block.
    _PROVIDER_AUTH_MIN_STRIKES = 2

    def _provider_auth_seed_max_age_seconds(self) -> float:
        """
        Limit how long auth-failure cooldowns recovered from historical logs stay valid.
        This prevents stale failures from poisoning a fresh backend session.
        """
        return self._read_float_env("EVERMIND_PROVIDER_AUTH_SEED_MAX_AGE_SEC", 900.0, 0.0, 43200.0)

    def _provider_auth_state(self, provider: str) -> Optional[Dict[str, Any]]:
        normalized = str(provider or "").strip().lower()
        if normalized in ("", "relay", "ollama"):
            return None
        return self._provider_auth_health.setdefault(normalized, {
            "blocked_until": 0.0,
            "last_error": "",
            "last_failure_at": 0.0,
            "last_success_at": 0.0,
            "consecutive_auth_failures": 0,  # V4.4: strike counter
        })

    def _provider_recent_auth_failure_reason(self, model_info: Optional[Dict[str, Any]]) -> str:
        provider = str((model_info or {}).get("provider") or "").strip().lower()
        state = self._provider_auth_state(provider)
        if not state:
            return ""
        blocked_until = float(state.get("blocked_until") or 0.0)
        if blocked_until <= time.time():
            return ""
        remaining = max(1, int(round(blocked_until - time.time())))
        last_error = _sanitize_error(str(state.get("last_error") or "")).strip()
        detail = f"; last error: {last_error}" if last_error else ""
        return f"recent {provider} auth failure ({remaining}s remaining{detail})"

    def _record_provider_auth_failure(self, model_info: Optional[Dict[str, Any]], error_message: str) -> None:
        provider = str((model_info or {}).get("provider") or "").strip().lower()
        state = self._provider_auth_state(provider)
        if not state:
            return
        now = time.time()
        state["last_failure_at"] = now
        state["last_error"] = _sanitize_error(str(error_message or "authentication failed"))[:200]
        # V4.4: Require N consecutive auth failures before triggering cooldown.
        # A single transient 401 should NOT block the entire provider.
        strikes = int(state.get("consecutive_auth_failures") or 0) + 1
        state["consecutive_auth_failures"] = strikes
        if strikes < self._PROVIDER_AUTH_MIN_STRIKES:
            logger.info(
                "Provider auth strike %d/%d (no cooldown yet): provider=%s error=%s",
                strikes, self._PROVIDER_AUTH_MIN_STRIKES,
                provider or "unknown", state.get("last_error", ""),
            )
            return
        cooldown = self._provider_auth_failure_grace_seconds()
        state["blocked_until"] = now + cooldown
        logger.warning(
            "Provider auth failure cooldown: provider=%s cooldown=%ss strikes=%d error=%s",
            provider or "unknown",
            int(round(cooldown)),
            strikes,
            state.get("last_error", ""),
        )

    def _record_provider_auth_success(self, model_info: Optional[Dict[str, Any]]) -> None:
        provider = str((model_info or {}).get("provider") or "").strip().lower()
        state = self._provider_auth_state(provider)
        if not state:
            return
        if not any(state.get(k) for k in ("blocked_until", "last_error", "last_failure_at", "consecutive_auth_failures")):
            return
        state["blocked_until"] = 0.0
        state["last_error"] = ""
        state["consecutive_auth_failures"] = 0  # V4.4: reset strike counter
        state["last_success_at"] = time.time()
        logger.info("Provider auth failure cooldown cleared: provider=%s", provider or "unknown")

    def _filter_viable_model_candidates(self, candidates: List[str]) -> List[str]:
        filtered: List[str] = []
        seen: set[str] = set()
        blocked_by_auth: List[str] = []
        for raw_name in candidates or []:
            model_name = str(raw_name or "").strip()
            if not model_name or model_name in seen:
                continue
            seen.add(model_name)
            model_info = self._resolve_model(model_name)
            provider = str(model_info.get("provider") or "").strip().lower()
            if provider in ("relay", "ollama", ""):
                filtered.append(model_name)
                continue
            auth_reason = self._provider_recent_auth_failure_reason(model_info)
            if auth_reason:
                blocked_by_auth.append(model_name)
                continue
            if self._check_api_key(model_name, model_info):
                continue
            filtered.append(model_name)

        # V4.4: Last-resort circuit breaker — if ALL candidates were blocked
        # by auth cooldown and none survived, clear the oldest cooldown and
        # let those candidates through.  This prevents the catastrophic
        # scenario where transient 401s on two providers kill an entire run.
        if not filtered and blocked_by_auth:
            self._emergency_clear_oldest_auth_cooldown()
            for model_name in blocked_by_auth:
                model_info = self._resolve_model(model_name)
                if not self._provider_recent_auth_failure_reason(model_info):
                    filtered.append(model_name)
            if filtered:
                logger.warning(
                    "Emergency auth cooldown clear recovered %d candidate(s): %s",
                    len(filtered), filtered[:4],
                )
            else:
                # Even after clearing oldest, still blocked — return all as last resort
                filtered = list(blocked_by_auth)
                logger.warning(
                    "All providers auth-blocked, returning all %d as last resort: %s",
                    len(filtered), filtered[:4],
                )

        return filtered

    def _emergency_clear_oldest_auth_cooldown(self) -> None:
        """Clear the provider auth cooldown that was set earliest.
        Called when ALL model candidates are blocked by auth cooldowns."""
        oldest_provider = ""
        oldest_failure_at = float("inf")
        for provider, state in self._provider_auth_health.items():
            blocked_until = float(state.get("blocked_until") or 0.0)
            if blocked_until <= time.time():
                continue
            failure_at = float(state.get("last_failure_at") or 0.0)
            if failure_at < oldest_failure_at:
                oldest_failure_at = failure_at
                oldest_provider = provider
        if oldest_provider:
            state = self._provider_auth_health.get(oldest_provider)
            if state:
                state["blocked_until"] = 0.0
                state["consecutive_auth_failures"] = 0
                logger.warning(
                    "Emergency cleared auth cooldown for provider=%s (oldest failure at %s)",
                    oldest_provider,
                    time.strftime("%H:%M:%S", time.localtime(oldest_failure_at)) if oldest_failure_at < float("inf") else "unknown",
                )

    def _augment_candidates_for_compatible_gateway(self, node_type: str, candidates: List[str]) -> List[str]:
        if len(candidates or []) != 1:
            return candidates
        normalized_node_type = normalize_node_role(str(node_type or "").strip())
        if normalized_node_type not in self._compatible_gateway_fail_fast_node_types():
            return candidates
        primary = str(candidates[0] or "").strip()
        if not primary:
            return candidates
        if not self._custom_compatible_gateway_base(self._resolve_model(primary)):
            return candidates
        augmented = list(candidates)
        for model_name in self._legacy_model_fallback_chain(primary):
            if model_name not in augmented:
                augmented.append(model_name)
        return augmented

    def _relay_model_candidates_for(self, model_name: str) -> List[str]:
        target = str(model_name or "").strip()
        if not target:
            return []
        relay_mgr = get_relay_manager()
        return relay_mgr.relay_model_candidates_for(target)

    def _augment_candidates_with_matching_relays(self, candidates: List[str]) -> List[str]:
        augmented: List[str] = []
        seen: set[str] = set()
        _model_cache: Dict[str, Dict] = {}  # Local cache to avoid repeated _resolve_model calls
        for model_name in candidates or []:
            normalized = str(model_name or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            augmented.append(normalized)
            if normalized not in _model_cache:
                _model_cache[normalized] = self._resolve_model(normalized)
            provider = str(_model_cache[normalized].get("provider") or "").strip().lower()
            if provider == "relay":
                continue
            for relay_candidate in self._relay_model_candidates_for(normalized):
                if relay_candidate in seen:
                    continue
                seen.add(relay_candidate)
                augmented.append(relay_candidate)
        return augmented

    def _prefer_matching_relays_for_custom_gateway(self, node_type: str, candidates: List[str]) -> List[str]:
        normalized_node_type = normalize_node_role(str(node_type or "").strip())
        if normalized_node_type not in self._compatible_gateway_fail_fast_node_types():
            return candidates

        available = {
            str(model_name or "").strip()
            for model_name in (candidates or [])
            if str(model_name or "").strip()
        }
        preferred: List[str] = []
        seen: set[str] = set()
        _model_cache: Dict[str, Dict] = {}

        for raw_model_name in candidates or []:
            model_name = str(raw_model_name or "").strip()
            if not model_name or model_name in seen:
                continue

            if model_name not in _model_cache:
                _model_cache[model_name] = self._resolve_model(model_name)
            model_info = _model_cache[model_name]
            provider = str(model_info.get("provider") or "").strip().lower()
            if provider != "relay" and self._custom_compatible_gateway_base(model_info):
                for relay_candidate in self._relay_model_candidates_for(model_name):
                    relay_name = str(relay_candidate or "").strip()
                    if not relay_name or relay_name in seen or relay_name not in available:
                        continue
                    seen.add(relay_name)
                    preferred.append(relay_name)

            if model_name not in seen:
                seen.add(model_name)
                preferred.append(model_name)

        return preferred or candidates

    def _promote_matching_relays_for_unhealthy_gateway(self, node_type: str, candidates: List[str]) -> List[str]:
        normalized_node_type = normalize_node_role(str(node_type or "").strip())
        if normalized_node_type not in self._compatible_gateway_fail_fast_node_types():
            return candidates

        available = {
            str(model_name or "").strip()
            for model_name in (candidates or [])
            if str(model_name or "").strip()
        }
        promoted: List[str] = []
        seen: set[str] = set()
        _model_cache: Dict[str, Dict] = {}

        for raw_model_name in candidates or []:
            model_name = str(raw_model_name or "").strip()
            if not model_name or model_name in seen:
                continue

            if model_name not in _model_cache:
                _model_cache[model_name] = self._resolve_model(model_name)
            model_info = _model_cache[model_name]
            provider = str(model_info.get("provider") or "").strip().lower()
            if provider != "relay" and self._custom_compatible_gateway_base(model_info):
                unhealthy_reason = self._compatible_gateway_recent_unhealthy_reason(model_info)
                if unhealthy_reason:
                    for relay_candidate in self._relay_model_candidates_for(model_name):
                        relay_name = str(relay_candidate or "").strip()
                        if not relay_name or relay_name in seen or relay_name not in available:
                            continue
                        seen.add(relay_name)
                        promoted.append(relay_name)

            seen.add(model_name)
            promoted.append(model_name)

        return promoted or candidates

    def _deprioritize_unhealthy_compatible_gateway_candidates(self, node_type: str, candidates: List[str]) -> List[str]:
        normalized_node_type = normalize_node_role(str(node_type or "").strip())
        if normalized_node_type not in self._compatible_gateway_fail_fast_node_types():
            return candidates
        if len(candidates or []) < 2:
            return candidates

        healthy: List[str] = []
        unhealthy: List[str] = []
        for raw_model_name in candidates or []:
            model_name = str(raw_model_name or "").strip()
            if not model_name:
                continue
            model_info = self._resolve_model(model_name)
            if self._custom_compatible_gateway_base(model_info) and self._compatible_gateway_recent_unhealthy_reason(model_info):
                unhealthy.append(model_name)
            else:
                healthy.append(model_name)

        if not unhealthy or not healthy:
            return candidates
        return healthy + unhealthy

    def _augment_candidates_with_emergency_fallbacks(
        self,
        node_type: str,
        candidates: List[str],
        fallback_model: str,
    ) -> List[str]:
        normalized_node_type = normalize_node_role(str(node_type or "").strip())
        if normalized_node_type not in self._compatible_gateway_fail_fast_node_types():
            return candidates
        existing = [
            str(model_name or "").strip()
            for model_name in (candidates or [])
            if str(model_name or "").strip()
        ]
        if len(existing) >= 2:
            return existing
        if existing:
            primary_info = self._resolve_model(existing[0])
            primary_provider = str(primary_info.get("provider") or "").strip().lower()
            if primary_provider != "relay" and not self._custom_compatible_gateway_base(primary_info):
                return existing
        seen = set(existing)
        augmented = list(existing)
        for model_name in self._legacy_model_fallback_chain(fallback_model):
            normalized = str(model_name or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            augmented.append(normalized)
        return augmented

    def _fallback_skip_reason_for_candidate(self, node_type: str, model_name: str) -> str:
        normalized_node_type = normalize_node_role(str(node_type or "").strip())
        if normalized_node_type not in self._compatible_gateway_fail_fast_node_types():
            return ""
        normalized_model = str(model_name or "").strip()
        if not normalized_model:
            return ""
        model_info = self._resolve_model(normalized_model)
        if not self._custom_compatible_gateway_base(model_info):
            return ""
        return self._compatible_gateway_recent_unhealthy_reason(model_info)

    def _next_fallback_candidate(self, node_type: str, candidates: List[str], current_index: int) -> tuple[str, int]:
        for idx in range(max(-1, int(current_index)) + 1, len(candidates or [])):
            candidate_model = str(candidates[idx] or "").strip()
            if not candidate_model:
                continue
            skip_reason = self._fallback_skip_reason_for_candidate(node_type, candidate_model)
            if skip_reason:
                logger.info(
                    "Skipping unhealthy fallback candidate: node=%s model=%s reason=%s",
                    node_type,
                    candidate_model,
                    _sanitize_error(skip_reason[:200]),
                )
                continue
            return candidate_model, idx
        return "", -1

    def resolve_node_model_candidates(self, node: Dict, fallback_model: str) -> List[str]:
        effective_node = node or {}
        node_type = normalize_node_role(str(effective_node.get("type") or "").strip())
        configured_chain = self._normalized_node_model_preferences().get(node_type, [])
        explicit_chain = self._normalize_model_chain(
            effective_node.get("model_preferences") or effective_node.get("models") or []
        )
        explicit_model = self._normalize_model_chain([effective_node.get("model")])
        fallback_chain = self._normalize_model_chain([fallback_model])
        if explicit_model and bool(effective_node.get("model_is_default")):
            explicit_model = []

        candidates: List[str] = []
        seen: set[str] = set()

        def _push_many(values: List[str]) -> None:
            for model_name in values:
                normalized = str(model_name or "").strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                candidates.append(normalized)

        _push_many(explicit_model)
        _push_many(explicit_chain)
        _push_many(configured_chain)
        _push_many(fallback_chain)

        if not configured_chain and not explicit_chain:
            _push_many(self._legacy_model_fallback_chain(fallback_model))

        if not candidates:
            _push_many(["gpt-5.3-codex", "kimi-coding"])
        candidates = self._augment_candidates_for_compatible_gateway(node_type, candidates)
        candidates = self._augment_candidates_with_matching_relays(candidates)
        candidates = self._prefer_matching_relays_for_custom_gateway(node_type, candidates)
        filtered_candidates = self._filter_viable_model_candidates(candidates)
        filtered_candidates = self._augment_candidates_with_emergency_fallbacks(
            node_type,
            filtered_candidates,
            fallback_model,
        )
        filtered_candidates = self._augment_candidates_with_matching_relays(filtered_candidates)
        filtered_candidates = self._filter_viable_model_candidates(filtered_candidates)
        filtered_candidates = self._promote_matching_relays_for_unhealthy_gateway(node_type, filtered_candidates)
        filtered_candidates = self._deprioritize_unhealthy_compatible_gateway_candidates(node_type, filtered_candidates)
        # v3.0.5 FIX: Respect single-model pin in node_model_preferences.
        # When the user pins EXACTLY one model for a node type (e.g. ["kimi-coding"]),
        # gateway health deprioritisation should not push it behind auto-added fallbacks.
        # Multi-model chains (e.g. ["gpt-5.4", "kimi-coding"]) are left as-is so the
        # user's secondary choice can rise above an unhealthy primary.
        if configured_chain and len(configured_chain) == 1 and filtered_candidates:
            pinned = str(configured_chain[0] or "").strip()
            if (
                pinned
                and pinned in filtered_candidates
                and filtered_candidates[0] != pinned
            ):
                filtered_candidates.remove(pinned)
                filtered_candidates.insert(0, pinned)
        return filtered_candidates or candidates

    def preferred_model_for_node(self, node: Dict, fallback_model: str) -> str:
        candidates = self.resolve_node_model_candidates(node, fallback_model)
        for model_name in candidates:
            model_info = self._resolve_model(model_name)
            if not self._check_api_key(model_name, model_info):
                return model_name
        return candidates[0] if candidates else str(fallback_model or "kimi-coding")

    def _normalize_usage(self, usage: Any) -> Dict[str, int]:
        if not usage:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        elif hasattr(usage, "dict"):
            usage = usage.dict()
        elif not isinstance(usage, dict):
            usage = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
            }

        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def _merge_usage(self, base: Dict[str, int], delta: Any) -> Dict[str, int]:
        normalized = self._normalize_usage(delta)
        return {
            "prompt_tokens": base.get("prompt_tokens", 0) + normalized.get("prompt_tokens", 0),
            "completion_tokens": base.get("completion_tokens", 0) + normalized.get("completion_tokens", 0),
            "total_tokens": base.get("total_tokens", 0) + normalized.get("total_tokens", 0),
        }

    async def _litellm_stream_completion(self, **kwargs) -> Any:
        """Call litellm.acompletion with stream=True, collect chunks into a unified response.

        Returns a SimpleNamespace mimicking litellm's non-streaming response so
        callers need no changes.  Falls back to non-streaming acompletion if the
        stream setup itself fails before any data arrives.
        """
        from types import SimpleNamespace as _NS

        stream_kwargs = {**kwargs, "stream": True}
        content_parts: List[str] = []
        tool_calls_map: Dict[int, Dict[str, Any]] = {}
        usage_data: Any = None
        model_name = kwargs.get("model", "")

        try:
            response = await self._litellm.acompletion(**stream_kwargs)
            async for chunk in response:
                if not chunk.choices:
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage_data = chunk.usage
                    continue
                delta = chunk.choices[0].delta
                if delta:
                    if getattr(delta, "content", None):
                        content_parts.append(delta.content)
                    if getattr(delta, "tool_calls", None):
                        for tc_delta in delta.tool_calls:
                            idx = getattr(tc_delta, "index", 0)
                            if idx not in tool_calls_map:
                                tool_calls_map[idx] = {
                                    "id": getattr(tc_delta, "id", None) or f"tc_{idx}",
                                    "function": {"name": "", "arguments": ""},
                                }
                            fn = getattr(tc_delta, "function", None)
                            if fn:
                                if getattr(fn, "name", None):
                                    tool_calls_map[idx]["function"]["name"] += fn.name
                                if getattr(fn, "arguments", None):
                                    tool_calls_map[idx]["function"]["arguments"] += fn.arguments
                if hasattr(chunk, "usage") and chunk.usage:
                    usage_data = chunk.usage
                if getattr(chunk, "model", None):
                    model_name = chunk.model
        except Exception as _stream_err:
            if not content_parts and not tool_calls_map:
                # Stream never started — fall back to non-streaming async call
                return await self._litellm.acompletion(**kwargs)
            # Partial data received before error — discard tool_calls (may have
            # incomplete JSON arguments) and keep only text content.
            tool_calls_map.clear()
            logger.warning(
                "Streaming interrupted after partial data (%d chars); "
                "discarded tool_calls to avoid invalid JSON: %s",
                sum(len(p) for p in content_parts), _stream_err,
            )

        tool_calls = None
        if tool_calls_map:
            tool_calls = [
                _NS(
                    id=tool_calls_map[idx]["id"],
                    function=_NS(
                        name=tool_calls_map[idx]["function"]["name"],
                        arguments=tool_calls_map[idx]["function"]["arguments"],
                    ),
                )
                for idx in sorted(tool_calls_map.keys())
            ]

        if usage_data and not isinstance(usage_data, dict):
            if hasattr(usage_data, "model_dump"):
                usage_data = usage_data.model_dump()
            elif hasattr(usage_data, "dict"):
                usage_data = usage_data.dict()
            else:
                usage_data = {
                    "prompt_tokens": getattr(usage_data, "prompt_tokens", 0),
                    "completion_tokens": getattr(usage_data, "completion_tokens", 0),
                    "total_tokens": getattr(usage_data, "total_tokens", 0),
                }

        return _NS(
            choices=[_NS(
                message=_NS(
                    content="".join(content_parts) if content_parts else "",
                    tool_calls=tool_calls,
                ),
                finish_reason="tool_calls" if tool_calls else "stop",
            )],
            model=model_name,
            usage=usage_data or {},
        )

    def _estimate_litellm_cost(self, response: Any, model: str) -> float:
        if not self._litellm:
            return 0.0
        try:
            return float(self._litellm.completion_cost(completion_response=response, model=model))
        except Exception:
            return 0.0

    def _estimate_response_cost(self, model: str, usage: Any) -> float:
        normalized = self._normalize_usage(usage)
        pricing = {
            "computer-use-preview": {
                "input_per_million": float(os.getenv("COMPUTER_USE_INPUT_COST_PER_MILLION", "2.5")),
                "output_per_million": float(os.getenv("COMPUTER_USE_OUTPUT_COST_PER_MILLION", "15.0")),
            },
            "gpt-4o": {
                "input_per_million": 2.5,
                "output_per_million": 10.0,
            },
        }
        rates = pricing.get(model)
        if not rates:
            return 0.0
        return (
            normalized.get("prompt_tokens", 0) * rates["input_per_million"] / 1_000_000
            + normalized.get("completion_tokens", 0) * rates["output_per_million"] / 1_000_000
        )

    def _read_int_env(self, name: str, default: int, minimum: int, maximum: int) -> int:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        return max(minimum, min(maximum, value))

    def _read_float_env(self, name: str, default: float, minimum: float, maximum: float) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            return default
        return max(minimum, min(maximum, value))

    def _progress_event_timeout_sec(self) -> float:
        return self._read_float_env(
            "EVERMIND_PROGRESS_EVENT_TIMEOUT_SEC",
            1.0,
            0.05,
            5.0,
        )

    async def _get_shared_http_session(self):
        """Get or create a shared aiohttp session with persistent connection pooling.

        Benefits:
        - TCP connection reuse: avoids ~200ms TLS handshake per API call
        - DNS caching: avoids DNS lookup per call (~20ms)
        - Keep-alive: reuses existing connections for subsequent requests
        - Pool limit 100/host: supports concurrent multi-node calls
        """
        if self._shared_http_session is not None:
            if not getattr(self._shared_http_session, "closed", True):
                return self._shared_http_session
        try:
            import aiohttp
            connector = aiohttp.TCPConnector(
                limit=500,              # was 100 — match LiteLLM/openai-python scale
                limit_per_host=100,     # was 30 — relay hosts need more headroom
                ttl_dns_cache=300,      # DNS cache 5 min
                use_dns_cache=True,
                enable_cleanup_closed=True,
                keepalive_timeout=120,  # was 60 — LiteLLM uses 120s
            )
            timeout = aiohttp.ClientTimeout(total=180, connect=5, sock_read=120)
            self._shared_http_session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={
                    "User-Agent": "Evermind/3.0 (aiohttp; connection-pool)",
                    "Connection": "keep-alive",
                },
            )
            logger.info("Created shared HTTP session with connection pooling (limit=500, keepalive=120s)")
            return self._shared_http_session
        except ImportError:
            return None
        except Exception as exc:
            logger.warning("Failed to create shared HTTP session: %s", str(exc)[:200])
            return None

    async def _emit_noncritical_progress(
        self,
        on_progress: Optional[Callable],
        payload: Dict[str, Any],
        *,
        timeout_sec: Optional[float] = None,
    ) -> None:
        if not on_progress:
            return
        budget = float(timeout_sec if timeout_sec is not None else self._progress_event_timeout_sec())
        if budget <= 0:
            return
        stage = str((payload or {}).get("stage") or "").strip() or "unknown"
        try:
            await asyncio.wait_for(on_progress(payload), timeout=budget)
        except asyncio.TimeoutError:
            logger.warning(
                "Noncritical progress event timed out after %.2fs: stage=%s",
                budget,
                stage,
            )
        except Exception as exc:
            logger.warning(
                "Noncritical progress event failed: stage=%s error=%s",
                stage,
                _sanitize_error(str(exc)),
            )

    def _node_int_override(
        self,
        node: Optional[Dict[str, Any]],
        key: str,
        *,
        minimum: int,
        maximum: int,
    ) -> Optional[int]:
        if not isinstance(node, dict):
            return None
        raw = node.get(key)
        if raw in (None, ""):
            return None
        try:
            value = int(raw)
        except Exception:
            return None
        return max(minimum, min(maximum, value))

    def _node_capability_tier(self, node: Optional[Dict[str, Any]]) -> int:
        tier = self._node_int_override(node, "model_capability_tier", minimum=0, maximum=3)
        return tier or 0

    def _node_budget_tuning(self, node_type: str, node: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized_node_type = normalize_node_role(node_type)
        tier = self._node_capability_tier(node)
        tuning: Dict[str, Any] = {
            "token_scale": 1.0,
            "timeout_scale": 1.0,
            "stall_scale": 1.0,
            "tool_iterations_delta": 0,
            "browser_call_delta": 0,
            "builder_prewrite_scale": 1.0,
            "builder_repair_scale": 1.0,
            "builder_tool_only_scale": 1.0,
            "agentic_iteration_delta": 0,
            "agentic_tool_call_delta": 0,
        }
        if tier == 1:
            tuning.update({
                "token_scale": 1.15,
                "timeout_scale": 1.15,
                "stall_scale": 1.1,
                "builder_prewrite_scale": 1.1,
                "builder_repair_scale": 1.05,
                "builder_tool_only_scale": 1.05,
            })
            if normalized_node_type in {"builder", "merger", "debugger", "polisher"}:
                tuning["tool_iterations_delta"] = 2
                tuning["agentic_iteration_delta"] = 3
                tuning["agentic_tool_call_delta"] = 8
            else:
                tuning["tool_iterations_delta"] = 1
                tuning["agentic_iteration_delta"] = 1
                tuning["agentic_tool_call_delta"] = 4
            if normalized_node_type == "analyst":
                tuning["browser_call_delta"] = 2
            elif normalized_node_type == "polisher":
                tuning["browser_call_delta"] = 1
        elif tier == 3:
            tuning.update({
                "token_scale": 0.85,
                "timeout_scale": 0.85,
                "stall_scale": 0.8,
                "builder_prewrite_scale": 0.7,
                "builder_repair_scale": 0.8,
                "builder_tool_only_scale": 0.7,
            })
            if normalized_node_type in {"builder", "merger", "debugger", "polisher"}:
                tuning["tool_iterations_delta"] = -2
                tuning["agentic_iteration_delta"] = -3
                tuning["agentic_tool_call_delta"] = -8
            else:
                tuning["tool_iterations_delta"] = -1
                tuning["agentic_iteration_delta"] = -1
                tuning["agentic_tool_call_delta"] = -4
            if normalized_node_type == "analyst":
                tuning["browser_call_delta"] = -3
            elif normalized_node_type == "polisher":
                tuning["browser_call_delta"] = -1
        return tuning

    def _scale_budget_int(self, value: int, scale: float, *, minimum: int, maximum: int) -> int:
        scaled = int(round(float(value or 0) * float(scale or 1.0)))
        return max(minimum, min(maximum, scaled))

    def _delta_budget_int(self, value: int, delta: int, *, minimum: int, maximum: int) -> int:
        updated = int(value or 0) + int(delta or 0)
        return max(minimum, min(maximum, updated))

    def _agentic_loop_limits(self, node_type: str, node: Optional[Dict[str, Any]] = None) -> tuple[int, int]:
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type == "builder":
            max_iterations = 20
            max_tool_calls = 40
        elif normalized_node_type == "merger":
            max_iterations = 15
            max_tool_calls = 30
        elif normalized_node_type in {"debugger", "polisher"}:
            max_iterations = 12
            max_tool_calls = 25
        elif normalized_node_type in {"imagegen", "spritesheet", "assetimport"}:
            # v4.0: Asset/image nodes do limited work — 6 iterations, 12 tool calls.
            max_iterations = 6
            max_tool_calls = 12
        else:
            max_iterations = 10
            max_tool_calls = 20

        tuning = self._node_budget_tuning(node_type, node=node)
        max_iterations = self._delta_budget_int(
            max_iterations,
            int(tuning.get("agentic_iteration_delta", 0) or 0),
            minimum=4,
            maximum=32,
        )
        max_tool_calls = self._delta_budget_int(
            max_tool_calls,
            int(tuning.get("agentic_tool_call_delta", 0) or 0),
            minimum=max(8, max_iterations),
            maximum=80,
        )
        override_iterations = self._node_int_override(
            node,
            "agentic_max_iterations_override",
            minimum=4,
            maximum=32,
        )
        if override_iterations is not None:
            max_iterations = override_iterations
        override_tool_calls = self._node_int_override(
            node,
            "agentic_max_tool_calls_override",
            minimum=max(8, max_iterations),
            maximum=80,
        )
        if override_tool_calls is not None:
            max_tool_calls = override_tool_calls
        return max_iterations, max_tool_calls

    def _max_tokens_for_node(
        self,
        node_type: str,
        *,
        retry_attempt: int = 0,
        node: Optional[Dict[str, Any]] = None,
    ) -> int:
        normalized_node_type = normalize_node_role(node_type)
        # Builder often returns full HTML/CSS/JS; keep a higher budget.
        # P0 FIX: Escalated base from 24576→32768 to avoid finish=length death
        # chain on complex game/website tasks (Kimi K2.5 regularly needs 40K+ chars).
        # Escalate on retry: retry 0 → 32768, retry 1 → 40960, retry 2 → 49152
        if normalized_node_type == "builder":
            base = self._read_int_env("EVERMIND_BUILDER_MAX_TOKENS", 32768, 4096, 65536)
            escalated = base + retry_attempt * 8192
            value = min(escalated, 65536)
        elif normalized_node_type == "polisher":
            # P0 FIX: Polisher needs a much larger budget to avoid finish=length
            # truncation loops. Base increased 12288→16384, cap 32768→49152.
            # retry 0 → 16384, retry 1 → 24576, retry 2 → 32768
            base = self._read_int_env("EVERMIND_POLISHER_MAX_TOKENS", 16384, 4096, 49152)
            value = min(base + retry_attempt * 8192, 49152)
        elif normalized_node_type == "imagegen":
            # v4.0: ImageGen produces markdown briefs + asset manifests, not full code.
            # 32K was grossly oversized — encouraged verbose output and slow completion.
            # 16K comfortably fits asset packs + licensing notes; retry escalates if needed.
            base = self._read_int_env("EVERMIND_IMAGEGEN_MAX_TOKENS", 16384, 4096, 32768)
            value = min(base + retry_attempt * 4096, 32768)
        elif normalized_node_type in ("spritesheet", "assetimport"):
            # P0 FIX 2026-04-04: spritesheet/assetimport regularly hit finish=length
            # with 2048 tokens. Raised again so asset/source/license manifests fit.
            value = self._read_int_env("EVERMIND_ASSET_PLAN_MAX_TOKENS", 12288, 1024, 24576)
        elif normalized_node_type in ("planner", "planner_degraded"):
            # v4.0: Planner produces structured markdown (9 sections) + JSON appendix.
            # Previous 4096 default caused truncation → malformed output → fallback skeleton.
            value = self._read_int_env("EVERMIND_PLANNER_MAX_TOKENS", 8192, 2048, 16384)
        elif normalized_node_type == "router":
            # V4.3: Router only generates a JSON routing table — 2K tokens is
            # more than enough.  4K encouraged verbose reasoning that pushed
            # response time past the 120s ceiling.
            value = self._read_int_env("EVERMIND_ROUTER_MAX_TOKENS", 2048, 512, 4096)
        else:
            value = self._read_int_env("EVERMIND_MAX_TOKENS", 4096, 1024, 16384)

        override = self._node_int_override(node, "max_tokens_override", minimum=1024, maximum=65536)
        if override is not None:
            return override
        tuning = self._node_budget_tuning(node_type, node=node)
        return self._scale_budget_int(value, float(tuning.get("token_scale", 1.0) or 1.0), minimum=1024, maximum=65536)

    def _timeout_for_node(self, node_type: str, node: Optional[Dict[str, Any]] = None) -> int:
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type == "builder":
            value = self._read_int_env("EVERMIND_BUILDER_TIMEOUT_SEC", 960, 30, 960)
        elif normalized_node_type == "polisher":
            value = self._read_int_env("EVERMIND_POLISHER_TIMEOUT_SEC", 540, 60, 900)
        elif normalized_node_type == "imagegen":
            # v4.0: Reduced from 420→240. ImageGen writes briefs/manifests, not heavy code.
            # 420s was letting it idle excessively on retry loops.
            value = self._read_int_env("EVERMIND_IMAGEGEN_TIMEOUT_SEC", 240, 45, 420)
        elif normalized_node_type in ("spritesheet", "assetimport"):
            # v4.0: Reduced from 240→180. Asset plans are short structured outputs.
            value = self._read_int_env("EVERMIND_ASSET_PLAN_TIMEOUT_SEC", 180, 30, 300)
        elif normalized_node_type in ("planner", "planner_degraded"):
            # v3.1: Raised from 60s to 180s. v4.2: Raised to 270s.
            # gpt-5.4 produces more detailed blueprints (~9000+ chars for complex
            # game tasks) which takes ~250s at typical streaming rates. 180s caused
            # timeout → fallback to weaker model, losing planning quality.
            value = self._read_int_env("EVERMIND_PLANNER_TIMEOUT_SEC", 270, 30, 480)
        elif normalized_node_type == "analyst":
            # v3.1: Raised from 120s to 180s. Analyst with web research tools needs
            # time to fetch references and synthesize findings. Previous timeout caused
            # tool_iterations_exhausted → 8 critical handoff fields lost.
            value = self._read_int_env("EVERMIND_ANALYST_TIMEOUT_SEC", 180, 45, 360)
        elif normalized_node_type == "merger":
            # v3.1: Merger needs to read all builder outputs then produce merged result.
            # Give it the same budget as a builder.
            value = self._read_int_env("EVERMIND_MERGER_TIMEOUT_SEC", 720, 60, 1800)
        elif normalized_node_type == "router":
            # V4.3: Router must analyze the task and assign models to all nodes.
            # For complex pipelines (11+ nodes) with deep-thinking models, 120s
            # caused cascading timeouts → 2 model fallbacks before kimi picked up.
            value = self._read_int_env("EVERMIND_ROUTER_TIMEOUT_SEC", 240, 60, 480)
        else:
            value = self._read_int_env("EVERMIND_TIMEOUT_SEC", 120, 30, 600)

        override = self._node_int_override(node, "timeout_override_sec", minimum=20, maximum=3600)
        if override is not None:
            return override
        tuning = self._node_budget_tuning(node_type, node=node)
        return self._scale_budget_int(value, float(tuning.get("timeout_scale", 1.0) or 1.0), minimum=20, maximum=3600)

    def _stream_stall_timeout_for_node(self, node_type: str, node: Optional[Dict[str, Any]] = None) -> int:
        """
        Max allowed gap between streamed chunks before we treat the call as stalled.
        Builder can reasonably take longer before first meaningful chunk.
        Planner uses a short stall window — if it goes silent, it's stuck.
        """
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type == "builder":
            # v4.0: Reduced from 300→180. 300s stall was masking dead streams.
            # Kimi K2.5 large bursts still fit within 180s (typical burst < 120s).
            value = self._read_int_env("EVERMIND_BUILDER_STREAM_STALL_SEC", 180, 60, 600)
        elif normalized_node_type == "polisher":
            value = self._read_int_env("EVERMIND_POLISHER_STREAM_STALL_SEC", 90, 30, 360)
        elif normalized_node_type == "imagegen":
            # v4.0: Reduced from 210→60. ImageGen produces markdown briefs,
            # not large code — no reason to wait 3.5 min for next chunk.
            value = self._read_int_env("EVERMIND_IMAGEGEN_STREAM_STALL_SEC", 60, 15, 180)
        elif normalized_node_type in ("spritesheet", "assetimport"):
            # v4.0: Reduced from 90→45. These produce small planning docs.
            value = self._read_int_env("EVERMIND_ASSET_PLAN_STREAM_STALL_SEC", 45, 10, 120)
        elif normalized_node_type in ("planner", "planner_degraded"):
            # P0-2: Short stall window for planner — fail fast if stuck.
            value = self._read_int_env("EVERMIND_PLANNER_STREAM_STALL_SEC", 30, 10, 90)
        elif normalized_node_type == "analyst":
            # v4.0: Analyst-specific stall — was falling through to 180s default.
            # Analyst does web research; if stream dies, detect fast and retry.
            value = self._read_int_env("EVERMIND_ANALYST_STREAM_STALL_SEC", 60, 15, 180)
        else:
            # v4.0: Reduced default from 180→90 for non-builder nodes.
            value = self._read_int_env("EVERMIND_STREAM_STALL_SEC", 90, 20, 300)

        override = self._node_int_override(node, "stream_stall_timeout_override_sec", minimum=15, maximum=900)
        if override is not None:
            return override
        tuning = self._node_budget_tuning(node_type, node=node)
        return self._scale_budget_int(value, float(tuning.get("stall_scale", 1.0) or 1.0), minimum=15, maximum=900)

    def _effective_timeout_for_node(
        self,
        node_type: str,
        input_data: str = "",
        node: Optional[Dict[str, Any]] = None,
    ) -> int:
        timeout_sec = self._timeout_for_node(node_type, node=node)
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type != "builder":
            return timeout_sec
        if float(timeout_sec or 0) < 60:
            # Respect explicit tiny overrides used by tests or emergency ops.
            # Auto-boosting a 1-5s override up to 1800s would hide timeout/salvage
            # paths and make it impossible to validate the watchdog chain.
            return timeout_sec

        text = str(input_data or "")
        lower = text.lower()
        boosted = timeout_sec
        if text.strip():
            try:
                if task_classifier.classify(text).task_type == "game":
                    boosted = max(
                        boosted,
                        self._read_int_env("EVERMIND_BUILDER_GAME_TIMEOUT_SEC", 1800, 300, 7200),
                    )
            except Exception as _tc_err:
                logger.warning("Task classification failed in _effective_timeout_for_node, using base timeout: %s", _tc_err)
        if self._builder_input_wants_3d(text):
            boosted = max(
                boosted,
                self._read_int_env("EVERMIND_BUILDER_3D_TIMEOUT_SEC", 2400, 600, 7200),
            )
        if task_classifier.wants_multi_page(text):
            boosted = max(
                boosted,
                self._read_int_env("EVERMIND_BUILDER_MULTI_PAGE_TIMEOUT_SEC", 1800, 300, 7200),
            )
        retry_markers = (
            "previous attempt failed",
            "previous attempt timed out",
            "multiple timeouts detected",
            "retry 1/3",
            "retry 2/3",
            "retry 3/3",
            "previous error:",
            "navigation repair only",
        )
        if any(marker in lower for marker in retry_markers):
            boosted = max(
                boosted,
                self._read_int_env("EVERMIND_BUILDER_RETRY_TIMEOUT_SEC", 960, 600, 2400),
            )
        return boosted

    def _effective_stream_stall_timeout(
        self,
        node_type: str,
        input_data: str = "",
        node: Optional[Dict[str, Any]] = None,
    ) -> int:
        base = self._stream_stall_timeout_for_node(node_type, node=node)
        if normalize_node_role(node_type) != "builder":
            return base

        text = str(input_data or "")
        lower = text.lower()
        is_3d = self._builder_input_wants_3d(text)
        assigned_targets = len(self._builder_assigned_html_targets(text))
        if task_classifier.wants_multi_page(text) and assigned_targets >= 3:
            base = min(
                base,
                self._read_int_env("EVERMIND_BUILDER_MULTI_PAGE_STREAM_STALL_SEC", 180, 120, 600),
            )

        retry_markers = (
            "previous attempt failed",
            "previous attempt timed out",
            "multiple timeouts detected",
            "previous error:",
            "navigation repair only",
            "multi-page delivery incomplete",
            "html ownership / output contract violation",
        )
        if any(marker in lower for marker in retry_markers):
            if is_3d:
                base = max(
                    base,
                    self._read_int_env("EVERMIND_BUILDER_3D_RETRY_STREAM_STALL_SEC", 360, 150, 600),
                )
            else:
                base = min(
                    base,
                    self._read_int_env("EVERMIND_BUILDER_RETRY_STREAM_STALL_SEC", 150, 90, 600),
                )
        return base

    def _compatible_gateway_fail_fast_node_types(self) -> set[str]:
        return {
            "analyst",
            "assetimport",
            "builder",
            "debugger",
            "deployer",
            "imagegen",
            "planner",
            "planner_degraded",
            "polisher",
            "reviewer",
            "router",
            "scribe",
            "spritesheet",
            "tester",
            "uidesign",
        }

    def _gateway_initial_activity_timeout_for_node(self, node_type: str, input_data: str = "") -> int:
        """
        Compatible gateways can hang before the first usable token/tool-call arrives.
        Use a shorter initial-activity deadline so nodes fail over before the full
        hard ceiling expires.
        """
        normalized_node_type = normalize_node_role(node_type)
        text = str(input_data or "")
        if normalized_node_type == "builder":
            base = self._read_int_env("EVERMIND_COMPAT_GATEWAY_BUILDER_INITIAL_ACTIVITY_SEC", 55, 15, 180)
            if task_classifier.wants_multi_page(text):
                base = max(
                    base,
                    self._read_int_env("EVERMIND_COMPAT_GATEWAY_BUILDER_MULTI_PAGE_INITIAL_SEC", 55, 20, 240),
                )
            return base
        if normalized_node_type == "polisher":
            return self._read_int_env("EVERMIND_COMPAT_GATEWAY_POLISHER_INITIAL_ACTIVITY_SEC", 45, 15, 120)
        if normalized_node_type == "imagegen":
            return self._read_int_env("EVERMIND_COMPAT_GATEWAY_IMAGEGEN_INITIAL_ACTIVITY_SEC", 40, 15, 120)
        if normalized_node_type in ("spritesheet", "assetimport"):
            return self._read_int_env("EVERMIND_COMPAT_GATEWAY_ASSET_INITIAL_ACTIVITY_SEC", 35, 10, 90)
        if normalized_node_type in self._compatible_gateway_fail_fast_node_types():
            return self._read_int_env("EVERMIND_COMPAT_GATEWAY_INITIAL_ACTIVITY_SEC", 40, 10, 120)
        return 0

    def _builder_input_wants_3d(self, input_data: str = "") -> bool:
        """Detect whether the builder input is for a 3D engine game."""
        text = str(input_data or "").lower()
        # Technical markers (from system prompt / retry injections)
        _technical_markers = (
            "three.js", "three.scene", "webglrenderer", "3d_engine", "three.min.js",
            "three.module.js", "perspectivecamera", "webgl",
        )
        if any(marker in text for marker in _technical_markers):
            return True
        # User-facing markers (Chinese + English goal text)
        _goal_markers = (
            "3d射击", "3d游戏", "三维", "第三人称", "3d shooter", "third person",
            "tps", "3d game", "3d engine", "runtime mode is 3d",
            "_evermind_runtime/three",
        )
        if any(marker in text for marker in _goal_markers):
            return True
        # Classify via task_classifier as a fallback
        try:
            if task_classifier.game_runtime_mode(input_data) == "3d_engine":
                return True
        except Exception:
            pass
        return False

    def _builder_prewrite_call_timeout(
        self,
        node_type: str,
        input_data: str = "",
        node: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Absolute cap for a single builder model call before the first real HTML write.
        This guards against long planning / streaming loops that never reach file_ops.

        P0 FIX 2026-04-04: Kimi K2.5 takes 160-400s for the first tool call stream
        on complex game tasks (17K+ chunks). Base raised 90→150, 3D raised 180→300.
        P0 FIX 2026-04-05: Premium 3D/TPS repair passes keep the full first-write
        budget so retries patch the existing artifact instead of re-entering the
        same short timeout loop.
        """
        if normalize_node_role(node_type) != "builder":
            return 0
        base = self._read_int_env("EVERMIND_BUILDER_FIRST_WRITE_TIMEOUT_SEC", 150, 60, 480)
        text = str(input_data or "")
        text_lower = text.lower()
        is_3d = self._builder_input_wants_3d(input_data)
        is_support_lane = self._builder_is_support_lane(input_data)
        # v4.0 FIX: Detect merger-like builders and give them sufficient timeout.
        # Merger produces 120K+ char merged files at ~340 chars/s = ~350s streaming.
        # The previous 300s default caused systematic 3-retry timeout failures.
        is_merger = bool((node or {}).get("builder_merger_like")) or bool(
            re.search(r"\b(?:final merger|merger|integrator|integration|assemble|assembly|merge)\b", text_lower)
        )
        if is_merger and not is_support_lane:
            base = max(
                base,
                self._read_int_env("EVERMIND_MERGER_FIRST_WRITE_SEC", 600, 300, 900),
            )
        if task_classifier.wants_multi_page(text):
            base = max(
                base,
                self._read_int_env("EVERMIND_BUILDER_MULTI_PAGE_FIRST_WRITE_SEC", 180, 90, 420),
            )
        if is_support_lane:
            # v3.5: raised from 120→180. Support-lane builders need to read primary
            # builder's artifacts before writing, which adds ~60s overhead.
            base = min(
                base,
                self._read_int_env("EVERMIND_BUILDER_SUPPORT_FIRST_WRITE_SEC", 180, 60, 300),
            )
        # §FIX: 3D engine games need significantly more time — Three.js code is 20K+ chars
        if is_3d and not is_support_lane:
            base = max(
                base,
                self._read_int_env("EVERMIND_BUILDER_3D_FIRST_WRITE_SEC", 420, 180, 600),
            )
            goal_hint = self._builder_goal_hint_source(input_data=input_data)
            try:
                prefers_direct_text = task_classifier.premium_3d_builder_direct_text_first_pass(goal_hint)
            except Exception:
                prefers_direct_text = False
            if prefers_direct_text:
                # v3.5: raised from 210→360. 3D TPS games produce 20K+ char files;
                # 210s caused systematic Builder-1 pre-write timeouts on first attempt.
                base = min(
                    base,
                    self._read_int_env("EVERMIND_BUILDER_3D_DIRECT_TEXT_FIRST_WRITE_SEC", 360, 180, 480),
                )
        retry_markers = (
            "previous attempt failed",
            "previous attempt timed out",
            "retry 1/3",
            "retry 2/3",
            "retry 3/3",
            "multi-page delivery incomplete",
        )
        if any(marker in text_lower for marker in retry_markers):
            if is_support_lane:
                base = min(
                    base,
                    self._read_int_env("EVERMIND_BUILDER_SUPPORT_RETRY_FIRST_WRITE_SEC", 90, 45, 240),
                )
            elif is_merger and not is_support_lane:
                # v4.1: Merger retry timeout reduced from 600→420s. If the first
                # 600s attempt failed, repeating the same wait is wasteful. 420s
                # still covers 120K char streaming (~350s) with margin.
                base = max(
                    base,
                    self._read_int_env("EVERMIND_MERGER_RETRY_FIRST_WRITE_SEC", 420, 300, 900),
                )
            elif is_3d:
                base = max(
                    base,
                    self._read_int_env("EVERMIND_BUILDER_3D_RETRY_FIRST_WRITE_SEC", 420, 180, 600),
                )
            else:
                base = min(
                    base,
                    self._read_int_env("EVERMIND_BUILDER_RETRY_FIRST_WRITE_SEC", 180, 60, 360),
                )
        override = self._node_int_override(
            node,
            "builder_prewrite_timeout_override_sec",
            minimum=45,
            maximum=900,  # v4.0: raised from 600 for merger-like builders
        )
        if override is not None:
            return override
        tuning = self._node_budget_tuning(node_type, node=node)
        return self._scale_budget_int(
            base,
            float(tuning.get("builder_prewrite_scale", 1.0) or 1.0),
            minimum=45,
            maximum=900,  # v4.0: raised from 600 for merger-like builders
        )

    def _builder_repair_write_timeout(
        self,
        node_type: str,
        input_data: str = "",
        node: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Follow-up timeout after a builder tool turn that still produced no write.

        Premium 3D builders frequently spend one model turn listing or reading
        manifests before the actual HTML write turn. If we clamp that follow-up
        call to a short repair budget, the builder times out on the happy path.
        """
        if normalize_node_role(node_type) != "builder":
            return 0
        timeout = self._read_int_env("EVERMIND_BUILDER_REPAIR_WRITE_TIMEOUT_SEC", 60, 10, 360)
        # v4.0 FIX: Merger reads two large builder outputs before writing the
        # merged result. The default 60s repair budget is too tight — raise to
        # 300s so the model has time to process 120K+ chars of context.
        text_lower = str(input_data or "").lower()
        is_merger = bool((node or {}).get("builder_merger_like")) or bool(
            re.search(r"\b(?:final merger|merger|integrator|integration|assemble|assembly|merge)\b", text_lower)
        )
        if is_merger:
            timeout = max(
                timeout,
                self._read_int_env("EVERMIND_MERGER_REPAIR_WRITE_TIMEOUT_SEC", 300, 120, 600),
            )
        if self._builder_input_wants_3d(input_data):
            timeout = max(
                timeout,
                self._read_int_env(
                    "EVERMIND_BUILDER_3D_REPAIR_WRITE_TIMEOUT_SEC",
                    self._builder_prewrite_call_timeout(node_type, input_data, node=node),
                    180,
                    600,
                ),
            )
        override = self._node_int_override(
            node,
            "builder_repair_timeout_override_sec",
            minimum=10,
            maximum=600,
        )
        if override is not None:
            return override
        tuning = self._node_budget_tuning(node_type, node=node)
        return self._scale_budget_int(
            timeout,
            float(tuning.get("builder_repair_scale", 1.0) or 1.0),
            minimum=10,
            maximum=600,
        )

    def _builder_tool_only_call_timeout(
        self,
        node_type: str,
        input_data: str = "",
        node: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Early-abort budget for builder streams that stay stuck in tool-planning mode.

        This is intentionally lower than the full pre-write timeout, but only applies
        when the stream still has neither deliverable text nor a write-like file_ops
        payload in flight.
        """
        if normalize_node_role(node_type) != "builder":
            return 0
        prewrite_timeout = self._builder_prewrite_call_timeout(node_type, input_data, node=node)
        timeout = self._read_int_env("EVERMIND_BUILDER_TOOL_ONLY_TIMEOUT_SEC", 120, 30, 360)
        if self._builder_is_support_lane(input_data):
            timeout = min(
                timeout,
                self._read_int_env("EVERMIND_BUILDER_SUPPORT_TOOL_ONLY_TIMEOUT_SEC", 60, 20, 180),
            )
        elif self._builder_input_wants_3d(input_data):
            timeout = max(
                timeout,
                self._read_int_env("EVERMIND_BUILDER_3D_TOOL_ONLY_TIMEOUT_SEC", 150, 60, 420),
            )
        if prewrite_timeout > 0:
            timeout = min(timeout, prewrite_timeout)
        override = self._node_int_override(
            node,
            "builder_tool_only_timeout_override_sec",
            minimum=20,
            maximum=420,
        )
        if override is not None:
            return override
        tuning = self._node_budget_tuning(node_type, node=node)
        return self._scale_budget_int(
            timeout,
            float(tuning.get("builder_tool_only_scale", 1.0) or 1.0),
            minimum=20,
            maximum=420,
        )

    def _builder_force_text_threshold(self, input_data: str = "") -> int:
        """
        Limit how many non-write tool turns a builder may spend before switching to
        direct final-file delivery. Multi-page builders should write almost
        immediately; repeated list/read loops are usually wasted time.

        Retry attempts are stricter than first-pass execution, but game/creative
        builders still need extra room to assemble a minimally playable slice
        instead of getting forced into a text-only shell after two directory
        listings.
        """
        base = self._read_int_env("EVERMIND_BUILDER_FORCE_TEXT_STREAK", 5, 1, 20)
        text = str(input_data or "")
        lower = text.lower()
        assigned_targets = len(self._builder_assigned_html_targets(text))
        merger_like = bool(re.search(r"\b(?:final merger|merger|integrator|integration|assemble|assembly|merge)\b", lower))
        is_game_or_creative = False

        # Game/creative tasks need more research iterations (3D setup, physics, etc.)
        try:
            task_type = task_classifier.classify(text).task_type
            if task_type in ("game", "creative"):
                is_game_or_creative = True
                base = max(base, self._read_int_env("EVERMIND_BUILDER_GAME_FORCE_TEXT_STREAK", 6, 3, 20))
        except Exception:
            pass
        if self._builder_is_support_lane(input_data):
            # v3.1: Support lane needs to read main builder output before writing
            # complementary files. Previous value of 2 was too aggressive.
            base = min(
                base,
                self._read_int_env("EVERMIND_BUILDER_SUPPORT_FORCE_TEXT_STREAK", 5, 2, 10),
            )

        if task_classifier.wants_multi_page(text) and assigned_targets >= 3:
            base = min(
                base,
                self._read_int_env("EVERMIND_BUILDER_MULTI_PAGE_FORCE_TEXT_STREAK", 3, 1, 6),
            )

        if merger_like:
            # V4.3: Merger MUST read artifacts from BOTH builders before merging.
            # Typical read sequence: list dir (1) + read B1 index (1) + read B1
            # support JS (2-3) + read B2 output (1) + read B2 support (2-3) = 7-9.
            # Previous threshold of 10 triggered after reading, forcing text-only
            # mode that produced planning prose instead of merged HTML.
            base = max(
                base,
                self._read_int_env("EVERMIND_MERGER_FORCE_TEXT_STREAK", 16, 8, 25),
            )

        retry_markers = (
            "previous attempt failed",
            "previous attempt timed out",
            "retry 1/3",
            "retry 2/3",
            "retry 3/3",
            "multiple timeouts detected",
            "multi-page delivery incomplete",
            "html ownership / output contract violation",
            "navigation repair only",
        )
        if any(marker in lower for marker in retry_markers):
            retry_threshold = self._read_int_env("EVERMIND_BUILDER_RETRY_FORCE_TEXT_STREAK", 2, 1, 6)
            if is_game_or_creative:
                retry_threshold = self._read_int_env("EVERMIND_BUILDER_GAME_RETRY_FORCE_TEXT_STREAK", 4, 2, 10)
            base = min(base, retry_threshold)
        if merger_like and any(marker in lower for marker in retry_markers):
            # v3.1: Even on retry, merger needs to re-read artifacts before writing.
            # Previous value of 2 caused instant loop guard on every retry attempt.
            # Must use max() here because the general retry check above already set
            # base = min(base, 2) — using min() would keep it at 2, defeating the purpose.
            base = max(
                base,
                self._read_int_env("EVERMIND_MERGER_RETRY_FORCE_TEXT_STREAK", 6, 3, 12),
            )
        return base

    def _polisher_prewrite_call_timeout(self, node_type: str, input_data: str = "") -> int:
        """
        Polisher should inspect briefly, then start editing. Long tool-only calls
        before the first write are usually a stuck browser/read loop.
        """
        if normalize_node_role(node_type) != "polisher":
            return 0
        base = self._read_int_env("EVERMIND_POLISHER_FIRST_WRITE_TIMEOUT_SEC", 60, 30, 240)
        text = str(input_data or "")
        lower = text.lower()
        try:
            if task_classifier.wants_multi_page(text):
                base = max(
                    base,
                    self._read_int_env("EVERMIND_POLISHER_MULTI_PAGE_FIRST_WRITE_SEC", 75, 45, 240),
                )
        except Exception:
            pass
        retry_markers = (
            "previous attempt failed",
            "previous attempt timed out",
            "retry 1/3",
            "retry 2/3",
            "retry 3/3",
        )
        if any(marker in lower for marker in retry_markers):
            base = min(
                base,
                self._read_int_env("EVERMIND_POLISHER_RETRY_FIRST_WRITE_SEC", 45, 30, 180),
            )
        return base

    def _polisher_force_write_threshold(self, input_data: str = "") -> int:
        """
        Allow one quick inspection pass, but stop a polisher that keeps calling
        tools without producing a concrete file write.
        """
        base = self._read_int_env("EVERMIND_POLISHER_FORCE_WRITE_STREAK", 3, 1, 12)
        lower = str(input_data or "").lower()
        try:
            if task_classifier.wants_multi_page(str(input_data or "")):
                base = min(
                    base,
                    self._read_int_env("EVERMIND_POLISHER_MULTI_PAGE_FORCE_WRITE_STREAK", 3, 1, 8),
                )
        except Exception:
            pass
        retry_markers = (
            "previous attempt failed",
            "previous attempt timed out",
            "retry 1/3",
            "retry 2/3",
            "retry 3/3",
        )
        if any(marker in lower for marker in retry_markers):
            base = min(
                base,
                self._read_int_env("EVERMIND_POLISHER_RETRY_FORCE_WRITE_STREAK", 2, 1, 6),
            )
        return base

    def _polisher_browser_call_limit(self, node: Optional[Dict[str, Any]] = None) -> int:
        value = self._read_int_env("EVERMIND_POLISHER_MAX_BROWSER_CALLS", 1, 0, 8)
        override = self._node_int_override(node, "browser_call_limit", minimum=0, maximum=8)
        if override is not None:
            return override
        tuning = self._node_budget_tuning("polisher", node=node)
        return self._delta_budget_int(
            value,
            int(tuning.get("browser_call_delta", 0) or 0),
            minimum=0,
            maximum=8,
        )

    def _builder_direct_multifile_requested(self, node_type: str, input_data: str = "") -> bool:
        if normalize_node_role(node_type) != "builder":
            return False
        text = str(input_data or "")
        if not text:
            return False
        goal_hint = self._builder_goal_hint_source(input_data=text)
        if self._builder_retry_requires_existing_artifact_patch_context(
            node_type,
            input_data=text,
            goal_hint=goal_hint,
        ):
            return False
        return BUILDER_DIRECT_MULTIFILE_MARKER.lower() in text.lower()

    def _builder_goal_classification_candidates(
        self,
        *,
        goal_hint: str = "",
        input_data: str = "",
    ) -> List[str]:
        candidates: List[str] = []

        def _append(value: str) -> None:
            text = str(value or "").strip()
            if not text or text in candidates:
                return
            candidates.append(text)

        explicit = str(goal_hint or "").strip()
        if explicit:
            _append(explicit)

        text = str(input_data or "").strip()
        if text:
            for pattern in _BUILDER_GOAL_HINT_PATTERNS:
                match = pattern.search(text)
                if not match:
                    continue
                _append(str(match.group(1) or "").strip())
            _append(text)
        return candidates

    def _builder_retry_looks_like_premium_3d_game(
        self,
        *,
        goal_hint: str = "",
        input_data: str = "",
    ) -> bool:
        for candidate in self._builder_goal_classification_candidates(
            goal_hint=goal_hint,
            input_data=input_data,
        ):
            try:
                if task_classifier.wants_multi_page(candidate):
                    continue
            except Exception:
                pass
            try:
                if task_classifier.premium_3d_builder_patch_preferred(candidate):
                    return True
            except Exception:
                continue
        return False

    def _builder_has_retry_context(
        self,
        node_type: str,
        *,
        input_data: str = "",
        node: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if normalize_node_role(node_type) != "builder":
            return False
        lower = str(input_data or "").lower()
        retry_attempt = int(((node or {}).get("retry_attempt", 0) or 0))
        retry_markers = (
            "retry 1/",
            "retry 2/",
            "retry 3/",
            "previous attempt failed",
            "previous attempt timed out",
            "quality gate failed",
            "builder pre-write timeout",
            "builder first-write timeout",
            "builder direct-text idle timeout",
            "builder execution timeout",
            "reviewer rejected your output",
            "reviewer rejected",
            "reviewer rework",
            "patch mode only",
            "inspect the existing output artifacts first",
            "start from the current files and patch the failing areas only",
        )
        return retry_attempt > 0 or any(marker in lower for marker in retry_markers)

    def _builder_retry_requires_existing_artifact_patch_context(
        self,
        node_type: str,
        *,
        input_data: str = "",
        goal_hint: str = "",
        node: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if normalize_node_role(node_type) != "builder":
            return False
        if bool(
            (node or {}).get("builder_existing_artifact_patch_mode")
            or (node or {}).get("existing_artifact_patch_mode")
        ):
            return True
        if not self._builder_has_retry_context(node_type, input_data=input_data, node=node):
            return False
        if self._builder_retry_missing_artifact_context(
            node_type,
            input_data=input_data,
            goal_hint=goal_hint,
            node=node,
        ):
            return False
        return self._builder_retry_looks_like_premium_3d_game(
            goal_hint=goal_hint,
            input_data=input_data,
        )

    def _builder_retry_missing_artifact_context(
        self,
        node_type: str,
        *,
        input_data: str = "",
        goal_hint: str = "",
        node: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if normalize_node_role(node_type) != "builder":
            return False
        retry_context = "\n".join(
            str(part or "")
            for part in (
                input_data,
                goal_hint,
                (node or {}).get("error"),
            )
        ).lower()
        if not retry_context:
            return False
        missing_artifact_markers = (
            "tool-planning prose",
            "persistable html deliverable",
            "non-deliverable text",
            "no saved html artifact found",
            "no real file written",
            "no file write produced",
            "builder pre-write timeout",
            "builder first-write timeout",
            "returned empty output",
            "returned empty content",
            "empty output on retry",
        )
        return any(marker in retry_context for marker in missing_artifact_markers)

    def _builder_should_auto_direct_multifile(
        self,
        node_type: str,
        *,
        model_name: str = "",
        model_info: Optional[Dict[str, Any]] = None,
        input_data: str = "",
    ) -> bool:
        if normalize_node_role(node_type) != "builder":
            return False
        assigned_targets = len(self._builder_assigned_html_targets(input_data))
        resolved = model_info or self._resolve_model(model_name or "")
        provider = str((resolved or {}).get("provider") or "").strip().lower()
        if provider != "kimi":
            return False
        if self._builder_retry_requires_existing_artifact_patch_context(
            node_type,
            input_data=input_data,
            goal_hint=self._builder_goal_hint_source(input_data=input_data),
        ):
            return False
        lower = str(input_data or "").lower()
        if BUILDER_TARGET_OVERRIDE_MARKER.lower() in lower and assigned_targets >= 1:
            return True
        if assigned_targets < 2:
            return False
        try:
            return task_classifier.wants_multi_page(input_data) or assigned_targets >= 2
        except Exception:
            return True

    def _builder_should_auto_direct_text(
        self,
        node_type: str,
        *,
        model_name: str = "",
        model_info: Optional[Dict[str, Any]] = None,
        input_data: str = "",
        goal_hint: str = "",
        node: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if normalize_node_role(node_type) != "builder":
            return False
        # v4.0 FIX: Support-lane builders MUST use tool/file_ops mode to produce
        # JS/CSS/JSON artifacts. direct_text only outputs single HTML files which
        # is wrong for support lanes. This fixes Builder2 being wrongly routed to
        # chat/direct_text on Kimi, losing file_ops write capability.
        if isinstance(node, dict):
            if bool((node or {}).get("builder_is_support_lane_node")):
                return False
            if node.get("can_write_root_index") is False:
                return False
        support_lane_hint = "\n".join(
            part for part in (str(input_data or "").strip(), str(goal_hint or "").strip()) if part
        )
        if self._builder_is_support_lane(support_lane_hint):
            return False
        text = self._builder_goal_hint_source(goal_hint=goal_hint, input_data=input_data)
        if not text or task_classifier.wants_multi_page(text):
            return False
        if isinstance(node, dict):
            allowed_targets = self._builder_allowed_html_targets_from_node(node)
            can_write_root = node.get("can_write_root_index")
            if can_write_root is False and (not allowed_targets or "index.html" not in allowed_targets):
                return False
        assigned_targets = self._builder_assigned_html_targets(text)
        if len(assigned_targets) > 1:
            return False
        has_retry_context = self._builder_has_retry_context(
            node_type,
            input_data=input_data,
            node=node,
        )
        resolved = model_info or self._resolve_model(model_name or "")
        provider = str((resolved or {}).get("provider") or "").strip().lower()
        if provider != "kimi":
            return False
        if not has_retry_context and task_classifier.premium_3d_builder_direct_text_first_pass(text):
            return True
        if (
            task_classifier.premium_3d_builder_patch_preferred(text)
            and self._builder_retry_requires_existing_artifact_patch_context(
                node_type,
                input_data=input_data,
                goal_hint=goal_hint,
                node=node,
            )
        ):
            return False
        if task_classifier.game_direct_text_delivery_mode(text):
            return True
        return self._builder_retry_prefers_direct_text(
            node_type,
            input_data=input_data,
            goal_hint=goal_hint,
        )

    def _builder_retry_prefers_direct_text(
        self,
        node_type: str,
        *,
        input_data: str = "",
        goal_hint: str = "",
        node: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if normalize_node_role(node_type) != "builder":
            return False
        text = self._builder_goal_hint_source(goal_hint=goal_hint, input_data=input_data)
        if not text or task_classifier.wants_multi_page(text):
            return False
        assigned_targets = self._builder_assigned_html_targets(input_data or text)
        if len(assigned_targets) > 1:
            return False
        try:
            if task_classifier.classify(text).task_type != "game":
                return False
        except Exception:
            return False
        if self._builder_retry_requires_existing_artifact_patch_context(
            node_type,
            input_data=input_data,
            goal_hint=goal_hint,
            node=node,
        ):
            return False
        lower = str(input_data or "").lower()
        retry_attempt = int(((node or {}).get("retry_attempt", 0) or 0))
        retry_markers = (
            "retry 1/",
            "retry 2/",
            "retry 3/",
            "previous attempt failed",
            "previous attempt timed out",
        )
        direct_text_retry_markers = (
            "builder pre-write timeout",
            "builder first-write timeout",
            "builder direct-text idle timeout",
            "no real file written",
            "no file write produced",
            "stalled after scaffold generation",
            "no new meaningful html stream activity",
            "direct single-file delivery mode",
            "quality gate failed",
            "primitive placeholder geometry",
            "placeholder-grade",
            "canvas2d",
            "3d runtime",
            "tool-planning prose",
            "persistable html deliverable",
            "non-deliverable text",
            "no saved html artifact found",
        )
        is_retry = retry_attempt > 0 or any(marker in lower for marker in retry_markers)
        return is_retry and any(marker in lower for marker in direct_text_retry_markers)

    def _builder_goal_hint_source(
        self,
        *,
        goal_hint: str = "",
        input_data: str = "",
    ) -> str:
        explicit = str(goal_hint or "").strip()
        if explicit:
            return explicit
        text = str(input_data or "").strip()
        if not text:
            return ""
        for pattern in _BUILDER_GOAL_HINT_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            candidate = str(match.group(1) or "").strip()
            if candidate:
                return candidate
        return text

    def _max_tool_iterations_for_node(self, node_type: str, node: Optional[Dict[str, Any]] = None) -> int:
        normalized_node_type = normalize_node_role(node_type)
        # V4.3.1: Significantly increased default iterations for all nodes
        # to support Claude Code-style agentic multi-step tool loops.
        if normalized_node_type == "builder":
            value = self._read_int_env("EVERMIND_BUILDER_MAX_TOOL_ITERS", 25, 1, 30)
        elif normalized_node_type == "imagegen":
            value = self._read_int_env("EVERMIND_IMAGEGEN_MAX_TOOL_ITERS", 10, 2, 20)
        elif normalized_node_type == "polisher":
            value = self._read_int_env("EVERMIND_POLISHER_MAX_TOOL_ITERS", 18, 3, 25)
        elif normalized_node_type in ("reviewer", "tester"):
            value = self._read_int_env("EVERMIND_QA_MAX_TOOL_ITERS", 15, 4, 25)
        elif normalized_node_type == "analyst":
            # V4.5: Reduced 12→8. Research shows 8 iterations sufficient for analyst;
            # extra iterations cause context bloat (8K→120K) with diminishing returns.
            value = self._read_int_env("EVERMIND_ANALYST_MAX_TOOL_ITERS", 8, 2, 20)
        else:
            value = self._read_int_env("EVERMIND_DEFAULT_MAX_TOOL_ITERS", 8, 1, 15)

        override = self._node_int_override(node, "max_tool_iterations_override", minimum=1, maximum=30)
        if override is not None:
            return override
        tuning = self._node_budget_tuning(node_type, node=node)
        return self._delta_budget_int(
            value,
            int(tuning.get("tool_iterations_delta", 0) or 0),
            minimum=1,
            maximum=30,
        )

    def _analyst_browser_call_limit(self, node: Optional[Dict[str, Any]] = None) -> int:
        # v3.0.3: With up to 5 URLs allowed, each URL may need 1-2 browser actions
        # (navigate + observe/extract). Default 8 supports thorough research without
        # artificial stalls. Override via EVERMIND_ANALYST_MAX_BROWSER_CALLS.
        value = self._read_int_env("EVERMIND_ANALYST_MAX_BROWSER_CALLS", 8, 0, 16)
        override = self._node_int_override(node, "browser_call_limit", minimum=0, maximum=16)
        if override is not None:
            return override
        tuning = self._node_budget_tuning("analyst", node=node)
        return self._delta_budget_int(
            value,
            int(tuning.get("browser_call_delta", 0) or 0),
            minimum=0,
            maximum=16,
        )

    def _should_block_browser_call(
        self,
        node_type: str,
        tool_call_stats: Dict[str, int],
        node: Optional[Dict[str, Any]] = None,
    ) -> bool:
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type == "analyst":
            limit = self._analyst_browser_call_limit(node=node)
        elif normalized_node_type == "polisher":
            limit = self._polisher_browser_call_limit(node=node)
        else:
            return False
        if limit < 0:
            return False
        current = int(tool_call_stats.get("browser", 0) or 0)
        return current >= limit

    def _desktop_qa_browser_suppressed(self, node_type: str, input_data: Any) -> bool:
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type not in ("reviewer", "tester"):
            return False
        text = str(input_data or "")
        if "DESKTOP_QA_BROWSER_SUPPRESSED=1" not in text:
            return False
        return "[Desktop QA Session Evidence]" in text or "desktop Evermind QA Preview Session" in text

    def _qa_default_preview_url(self, node_type: str) -> str:
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type in ("reviewer", "tester"):
            port = str(os.getenv("PORT", "8765") or "8765").strip() or "8765"
            return f"http://127.0.0.1:{port}/preview/"
        return ""

    def _apply_qa_browser_tool_defaults(
        self,
        tool_name: str,
        node_type: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(params, dict):
            return params
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type not in ("reviewer", "tester"):
            return params
        preview_url = self._qa_default_preview_url(node_type)
        if not preview_url:
            return params

        normalized_tool = str(tool_name or "").strip().lower()
        patched = dict(params)
        if normalized_tool == "browser":
            action = str(patched.get("action") or "navigate").strip().lower() or "navigate"
            if action == "navigate" and not str(patched.get("url") or "").strip():
                patched["url"] = preview_url
        elif normalized_tool == "browser_use":
            if not str(patched.get("url") or "").strip():
                patched["url"] = preview_url
        return patched

    def _truncate_text(self, text: Any, max_chars: int) -> Any:
        if not isinstance(text, str):
            return text
        if max_chars <= 0:
            return ""
        if len(text) <= max_chars:
            return text
        suffix = "... [TRUNCATED]"
        if max_chars <= len(suffix):
            return text[:max_chars]
        return text[: max_chars - len(suffix)] + suffix

    def _safe_json_object(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _compact_tool_arguments_for_replay(self, function_name: str, raw_args: Any) -> Any:
        if not isinstance(raw_args, str):
            return raw_args

        normalized_name = str(function_name or "").strip().lower()
        if normalized_name != "file_ops":
            return self._truncate_text(raw_args, MAX_TOOL_ARGS_REPLAY_CHARS)

        parsed = self._safe_json_object(raw_args)
        action = str(parsed.get("action", "")).strip().lower()
        if not action and '"action"' in raw_args and '"write"' in raw_args.lower():
            action = "write"
        if action != "write":
            return self._truncate_text(raw_args, MAX_TOOL_ARGS_REPLAY_CHARS)

        path = str(parsed.get("path", "")).strip()
        if not path:
            match = re.search(r'"path"\s*:\s*"([^"]+)"', raw_args)
            if match:
                path = str(match.group(1)).strip()

        content = parsed.get("content")
        content_chars = len(content) if isinstance(content, str) else 0
        content_lines = content.count("\n") + 1 if isinstance(content, str) and content else 0
        replay_stub: Dict[str, Any] = {
            "action": "write",
            "path": path,
            # Do not inject placeholder source text back into the model context.
            # Placeholder strings can be mistakenly copied into real file writes.
            "content": "",
            "content_omitted": True,
        }
        if content_chars:
            replay_stub["content_chars"] = content_chars
        if content_lines:
            replay_stub["content_lines"] = content_lines
        return json.dumps(replay_stub, ensure_ascii=False)

    def _tool_result_has_write(self, result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        if bool(result.get("written")):
            return True
        try:
            if result.get("path") and int(result.get("bytes_written", 0) or 0) > 0:
                return True
        except Exception:
            pass
        data = result.get("data")
        if not isinstance(data, dict):
            return False
        if bool(data.get("written")):
            return True
        try:
            if data.get("path") and int(data.get("bytes_written", 0) or 0) > 0:
                return True
        except Exception:
            pass
        return False

    def _tool_result_write_path(self, result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        path = str(result.get("path") or "").strip()
        if path:
            return path
        data = result.get("data")
        if isinstance(data, dict):
            return str(data.get("path") or "").strip()
        return ""

    def _builder_game_delivery_signals(self, text: str) -> Dict[str, bool]:
        blob = str(text or "")
        flags = re.IGNORECASE
        return {
            "runtime_surface": bool(re.search(
                r"<canvas\b|new\s+THREE\.|THREE\.WebGLRenderer|new\s+Phaser\.Game|PIXI\.Application|"
                r"(?:id|class)=['\"][^'\"]*(game|arena|board|playfield|viewport|stage|scene|battlefield)[^'\"]*['\"]",
                blob,
                flags,
            )),
            "start_flow": bool(re.search(
                r"window\.startgame|function\s+startgame|start[-_ ]btn|play[-_ ]btn|"
                r"startoverlay|start[-_ ]screen|startscreen|"
                r"onclick\s*=\s*['\"][^'\"]*(?:startgame|(?:start|begin|launch|play)[A-Za-z0-9_]*\s*\()|"
                r"(?:function|(?:const|let|var))\s+(?:start|begin|launch|play)[A-Za-z0-9_]*\b|"
                r">\s*(?:start(?:\s+game|\s+mission)?|play|begin|launch|开始(?:游戏|任务|战斗)|进入战斗)\s*<|"
                r"showScreen\(['\"](?:start|level-select)",
                blob,
                flags,
            )),
            "game_loop": bool(re.search(
                r"requestanimationframe|setanimationloop|ticker\.add|runrenderloop|gameLoop|renderLoop|updateLoop",
                blob,
                flags,
            )),
        }

    def _builder_html_persist_rejection_reason(
        self,
        html_content: str,
        *,
        input_data: str = "",
        filename: str = "index.html",
    ) -> str:
        html = str(html_content or "").strip()
        if len(html) < 240:
            return f"{filename} is too short to trust as a final artifact"

        lower = html.lower()
        if "<!doctype" not in lower and "<html" not in lower:
            return f"{filename} is missing the HTML document shell"
        if "<body" not in lower:
            return f"{filename} is missing a <body> tag"

        body_match = re.search(r"<body\b[^>]*>(.*?)</body>", html, re.IGNORECASE | re.DOTALL)
        body_fragment = body_match.group(1) if body_match else html
        cleaned = re.sub(r"<head\b.*?</head\s*>", " ", body_fragment, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(
            r"<(style|script|noscript|template)\b.*?</\1\s*>",
            " ",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        )
        meaningful_tags = re.findall(
            r"<(?:canvas|main|section|article|header|footer|nav|aside|div|form|button|a|img|picture|video|svg|input|textarea|select|label|ul|ol|li|table|blockquote|h[1-6]|p)\b",
            cleaned,
            re.IGNORECASE,
        )
        visible_text = re.sub(r"<[^>]+>", " ", cleaned)
        visible_text = re.sub(r"\s+", " ", visible_text).strip()
        if len(meaningful_tags) == 0 and len(visible_text) < 40:
            return f"{filename} body lacks meaningful visible content"

        try:
            task_type = task_classifier.classify(input_data).task_type
        except Exception:
            task_type = ""

        if task_type == "game":
            signals = self._builder_game_delivery_signals(html)
            missing: List[str] = []
            if not signals.get("runtime_surface"):
                missing.append("gameplay surface")
            if not signals.get("start_flow"):
                missing.append("start/play flow")
            if not signals.get("game_loop"):
                missing.append("runtime loop")
            if missing:
                return f"{filename} still lacks a usable game slice ({', '.join(missing)})"
            continuation_reason = self._builder_game_text_continuation_reason(html, input_data)
            if continuation_reason:
                return f"{filename} is still incomplete for a game deliverable ({continuation_reason})"

        return ""

    def _builder_text_output_has_persistable_html(self, output_text: str, input_data: str = "") -> bool:
        extracted_files = self._extract_html_files_from_text_output(str(output_text or ""), input_data)
        for filename, html_content in extracted_files.items():
            if not self._builder_html_persist_rejection_reason(
                html_content,
                input_data=input_data,
                filename=filename,
            ):
                return True
        return False

    def _builder_output_looks_like_tool_planning_prose(self, output_text: str, input_data: str = "") -> bool:
        text = str(output_text or "").strip()
        if not text or self._builder_text_output_has_persistable_html(text, input_data):
            return False
        planning_markers = (
            r"\b(?:i(?:'ll| will)\s+first|let\s+me\s+first|let\s+me\s+check|let\s+me\s+inspect|"
            r"i(?:'ll| will)\s+inspect|i(?:'ll| will)\s+read|understand\s+the\s+existing|"
            r"check\s+the\s+existing\s+files|improve\s+it\s+in\s+place)\b"
        )
        tool_read_markers = (
            r"\bfile_ops\s+(?:read|list)\b|"
            r'"action"\s*:\s*"(?:read|list)"|'
            r'"file_path"\s*:\s*"[^"]+"|'
            r'"path"\s*:\s*"[^"]+"'
        )
        return bool(
            re.search(planning_markers, text, re.IGNORECASE)
            and re.search(tool_read_markers, text, re.IGNORECASE)
        )

    def _builder_non_deliverable_output_reason(self, output_text: str, input_data: str = "") -> str:
        text = str(output_text or "").strip()
        if not text:
            return "Builder returned empty content instead of a final HTML deliverable."
        if self._builder_text_output_has_persistable_html(text, input_data):
            return ""
        if self._builder_output_looks_like_tool_planning_prose(text, input_data):
            return "Builder returned tool-planning prose instead of a persistable HTML deliverable."
        return (
            "Builder returned non-deliverable text instead of persistable HTML. "
            "Repair/retry responses must emit final HTML, not process narration."
        )

    async def _auto_save_builder_text_output(
        self,
        *,
        output_text: str,
        input_data: str,
        node: Optional[Dict[str, Any]] = None,
        tool_results: List[Dict[str, Any]],
        tool_call_stats: Optional[Dict[str, int]] = None,
        on_progress=None,
    ) -> List[str]:
        text = str(output_text or "")
        if len(text) <= 120:
            return []

        extracted_files = self._extract_html_files_from_text_output(text, input_data)
        if not extracted_files:
            return []

        existing_paths = {
            self._tool_result_write_path(item)
            for item in (tool_results or [])
            if self._tool_result_has_write(item)
        }
        output_dir = self._current_output_dir()
        saved_paths: List[str] = []
        normalized_node_type = normalize_node_role(str((node or {}).get("type") or "").strip())
        builder_node = normalized_node_type == "builder"
        allowed_targets = (
            self._builder_allowed_html_targets_from_node(node)
            if builder_node
            else []
        )
        allowed_targets_set = {str(item or "").strip().lower() for item in allowed_targets if str(item or "").strip()}
        can_write_root_raw = (node or {}).get("can_write_root_index")
        if can_write_root_raw is None:
            can_write_root_raw = (node or {}).get("file_ops_can_write_root_index")
        can_write_root_index = True if can_write_root_raw is None else bool(can_write_root_raw)
        if bool((node or {}).get("builder_merger_like")):
            can_write_root_index = True
        support_lane_node = self._builder_is_support_lane_node(node) if builder_node else False
        stage_root_index_only = self._builder_stage_root_index_only_node(node) if builder_node else False
        staging_output_dir = self._builder_staging_output_dir_from_node(node) if builder_node else ""

        for filename, html_content in extracted_files.items():
            if len(html_content) < 120:
                continue
            filename = Path(str(filename or "").strip()).name
            filename_lower = filename.lower()
            if builder_node:
                if support_lane_node and filename_lower == "index.html":
                    logger.info(
                        "Skipping builder text-mode HTML auto-save for %s: support-lane builder cannot write root index.html",
                        filename,
                    )
                    continue
                if allowed_targets_set and filename_lower not in allowed_targets_set:
                    logger.info(
                        "Skipping builder text-mode HTML auto-save for %s: filename not owned by this builder lane (allowed=%s)",
                        filename,
                        sorted(allowed_targets_set),
                    )
                    continue
                if filename_lower == "index.html" and not can_write_root_index and "index.html" not in allowed_targets_set:
                    logger.info(
                        "Skipping builder text-mode HTML auto-save for %s: root index ownership is disabled for this lane",
                        filename,
                    )
                    continue
            rejection_reason = self._builder_html_persist_rejection_reason(
                html_content,
                input_data=input_data,
                filename=filename,
            )
            if rejection_reason:
                logger.info(
                    "Skipping builder text-mode HTML auto-save for %s: %s",
                    filename,
                    rejection_reason,
                )
                continue
            try:
                target_root = Path(output_dir)
                if stage_root_index_only and filename_lower == "index.html" and staging_output_dir:
                    target_root = Path(staging_output_dir)
                target_path = target_root / filename
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(html_content, encoding="utf-8")
                normalized_path = str(target_path)
                saved_paths.append(normalized_path)
                if normalized_path not in existing_paths:
                    tool_results.append({
                        "tool": "file_ops",
                        "success": True,
                        "written": True,
                        "path": normalized_path,
                        "started_at": time.time(),
                        "duration_ms": 0,
                        "args": {"action": "write", "path": normalized_path},
                        "data": {
                            "path": normalized_path,
                            "bytes_written": len(html_content.encode("utf-8")),
                            "written": True,
                        },
                        "error": None,
                        "artifacts": [normalized_path],
                    })
                    existing_paths.add(normalized_path)
                if on_progress:
                    metrics = self._written_file_code_metrics(normalized_path)
                    await self._emit_noncritical_progress(on_progress, {
                        "stage": "builder_write",
                        "plugin": "text_output_auto_save",
                        "path": normalized_path,
                        **metrics,
                    })
                logger.info(
                    "Auto-saved builder text-mode HTML: %s (%s chars)",
                    filename,
                    len(html_content),
                )
            except Exception as save_exc:
                logger.warning(
                    "Failed to auto-save builder text-mode HTML %s: %s",
                    filename,
                    str(save_exc)[:200],
                )

        if saved_paths and tool_call_stats is not None:
            tool_call_stats["file_ops"] = tool_call_stats.get("file_ops", 0) + len(saved_paths)
            logger.info(
                "Builder text-mode auto-save complete: %s files written to %s",
                len(saved_paths),
                output_dir,
            )
        return saved_paths

    def _infer_file_ops_action(self, raw_args: Any, result: Any) -> str:
        """
        Infer file_ops action robustly even when tool args are malformed/non-JSON.
        """
        parsed = self._safe_json_object(raw_args)
        action = str(parsed.get("action", "")).strip().lower()
        if action:
            return action

        if isinstance(raw_args, str):
            m = re.search(r'"action"\s*:\s*"([a-zA-Z_]+)"', raw_args)
            if m:
                return str(m.group(1)).strip().lower()

        if self._tool_result_has_write(result):
            return "write"
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict):
                if "entries" in data:
                    return "list"
                if "content" in data:
                    return "read"
                if "deleted" in data:
                    return "delete"
        return ""

    def _builder_needs_forced_text(self, node_type: str, output_text: str, tool_results: List[Dict[str, Any]]) -> bool:
        if node_type != "builder":
            return False
        lower = (output_text or "").lower()
        has_html = "<!doctype" in lower or "<html" in lower
        has_file_write = any(self._tool_result_has_write(tr) for tr in (tool_results or []))
        return (not has_html) and (not has_file_write)

    def _current_output_dir(self) -> str:
        current = str((self.config or {}).get("output_dir") or "").strip()
        return current or "/tmp/evermind_output"

    def _current_scratchpad_dir(self) -> str:
        output_dir = Path(self._current_output_dir().rstrip("/") or "/tmp/evermind_output")
        return str(output_dir.parent / "_evermind_scratch")

    def _builder_allowed_html_targets_from_node(self, node: Optional[Dict[str, Any]]) -> List[str]:
        if normalize_node_role(str((node or {}).get("type") or "").strip()) != "builder":
            return []
        raw_targets = (
            (node or {}).get("allowed_html_targets")
            or (node or {}).get("file_ops_allowed_html_targets")
            or []
        )
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        targets: List[str] = []
        seen: set[str] = set()
        for raw_name in raw_targets or []:
            name = Path(str(raw_name or "").strip()).name
            if not name or name in seen:
                continue
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html?", name, re.IGNORECASE):
                continue
            seen.add(name)
            targets.append(name)
        return targets

    def _builder_is_support_lane_node(self, node: Optional[Dict[str, Any]]) -> bool:
        if normalize_node_role(str((node or {}).get("type") or "").strip()) != "builder":
            return False
        if bool((node or {}).get("builder_merger_like")):
            return False
        return (node or {}).get("can_write_root_index") is False

    def _builder_stage_root_index_only_node(self, node: Optional[Dict[str, Any]]) -> bool:
        if normalize_node_role(str((node or {}).get("type") or "").strip()) != "builder":
            return False
        return bool((node or {}).get("builder_stage_root_index_only"))

    def _builder_staging_output_dir_from_node(self, node: Optional[Dict[str, Any]]) -> str:
        if normalize_node_role(str((node or {}).get("type") or "").strip()) != "builder":
            return ""
        return str((node or {}).get("builder_staging_output_dir") or "").strip()

    def _builder_support_snapshot_payload(self, path: Path) -> Optional[Dict[str, Any]]:
        suffix = path.suffix.lower()
        if suffix not in {".js", ".mjs", ".css", ".json"}:
            return None
        if not path.exists() or not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            size = int(path.stat().st_size)
        except Exception:
            return None
        compact = len(re.sub(r"\s+", "", text))
        if suffix == ".json":
            min_chars = 8
        elif suffix == ".css":
            min_chars = 40
        else:
            min_chars = 64
        if size <= 0 or compact < min_chars:
            return None
        return {
            "content": text,
            "size": size,
        }

    def _builder_output_root_for_node(self, node: Optional[Dict[str, Any]]) -> Path:
        output_root = Path(str((node or {}).get("output_dir") or self._current_output_dir()).strip() or "/tmp/evermind_output")
        try:
            return output_root.resolve()
        except Exception:
            return output_root

    def _builder_resolved_output_path(self, node: Optional[Dict[str, Any]], raw_path: str) -> Optional[Path]:
        path_text = str(raw_path or "").strip()
        if not path_text:
            return None
        output_root = self._builder_output_root_for_node(node)
        target_path = Path(path_text)
        if not target_path.is_absolute():
            target_path = output_root / target_path
        try:
            target_path = target_path.resolve()
        except Exception:
            target_path = target_path
        try:
            target_path.relative_to(output_root)
        except Exception:
            return None
        return target_path

    def _builder_prewrite_snapshot_payload(
        self,
        path: Path,
        *,
        input_data: str = "",
    ) -> Optional[Dict[str, Any]]:
        suffix = path.suffix.lower()
        if suffix in {".js", ".mjs", ".css", ".json"}:
            return self._builder_support_snapshot_payload(path)
        if suffix not in {".html", ".htm"}:
            return None
        if not path.exists() or not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            size = int(path.stat().st_size)
        except Exception:
            return None
        if size <= 0 or not text.strip():
            return None
        rejection_reason = self._builder_html_write_rejection_reason(
            text,
            input_data=input_data,
            filename=path.name,
        )
        if rejection_reason:
            return None
        return {
            "content": text,
            "size": size,
        }

    def _builder_capture_prewrite_snapshot(
        self,
        *,
        node: Optional[Dict[str, Any]],
        input_data: str,
        tool_action: str,
        parsed_args: Dict[str, Any],
        snapshot_cache: Dict[str, Dict[str, Any]],
    ) -> None:
        if normalize_node_role(str((node or {}).get("type") or "").strip()) != "builder":
            return
        if str(tool_action or "").strip().lower() != "write":
            return
        raw_path = (
            str(parsed_args.get("path") or "").strip()
            or str(parsed_args.get("file_path") or "").strip()
        )
        target_path = self._builder_resolved_output_path(node, raw_path)
        if not target_path:
            return
        snapshot_payload = self._builder_prewrite_snapshot_payload(
            target_path,
            input_data=input_data,
        )
        if snapshot_payload:
            snapshot_cache[str(target_path)] = snapshot_payload

    def _builder_html_write_rejection_reason(
        self,
        html_content: str,
        *,
        input_data: str = "",
        filename: str = "index.html",
    ) -> str:
        rejection_reason = self._builder_html_persist_rejection_reason(
            html_content,
            input_data=input_data,
            filename=filename,
        )
        if not rejection_reason:
            return ""

        try:
            task_type = task_classifier.classify(input_data).task_type
        except Exception:
            task_type = ""
        if task_type != "game":
            return rejection_reason

        html = str(html_content or "")
        lower = html.lower()
        html_bytes = len(html.encode("utf-8"))
        signals = self._builder_game_delivery_signals(html)
        local_script_ref = bool(re.search(
            r"<script\b[^>]*\bsrc\s*=\s*['\"](?!https?:|//|data:)[^'\"]+\.(?:js|mjs)(?:\?[^'\"]*)?['\"]",
            lower,
            re.IGNORECASE,
        ))
        marketing_stub = bool(re.search(
            r"(studio home|about studio|brand storytelling|interactive campaigns|premium brands|launch systems|crafted campaigns)",
            lower,
            re.IGNORECASE,
        ))

        if (
            ("still lacks a usable game slice" in rejection_reason or "incomplete for a game deliverable" in rejection_reason)
            and local_script_ref
            and (signals.get("runtime_surface") or signals.get("start_flow") or html_bytes >= 900)
            and not marketing_stub
        ):
            return ""

        if marketing_stub and html_bytes < 4000:
            return rejection_reason

        if html_bytes < 1200 and not local_script_ref:
            return rejection_reason

        if not signals.get("runtime_surface") and not signals.get("start_flow") and not local_script_ref:
            return rejection_reason

        return ""

    def _builder_guard_support_lane_write_result(
        self,
        *,
        node: Optional[Dict[str, Any]],
        tool_action: str,
        result: Any,
        snapshot_cache: Dict[str, Dict[str, Any]],
    ) -> Any:
        if tool_action != "write" or not isinstance(result, dict):
            return result
        if not bool(result.get("success")) or not self._builder_is_support_lane_node(node):
            return result

        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        raw_path = str(data.get("path") or result.get("path") or "").strip()
        if not raw_path:
            return result

        target_path = self._builder_resolved_output_path(node, raw_path)
        if not target_path:
            return result
        if target_path.name == "index.html":
            return result

        snapshot_key = str(target_path)
        snapshot_payload = self._builder_support_snapshot_payload(target_path)
        if snapshot_payload:
            snapshot_cache[snapshot_key] = snapshot_payload
            return result

        cached = snapshot_cache.get(snapshot_key)
        if not cached:
            return result

        restored_text = str(cached.get("content") or "")
        if not restored_text.strip():
            return result
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(restored_text, encoding="utf-8")
            restored_size = int(target_path.stat().st_size)
        except Exception:
            return result

        snapshot_cache[snapshot_key] = {
            "content": restored_text,
            "size": restored_size,
        }
        if isinstance(result.get("data"), dict):
            result["data"]["path"] = str(target_path)
            result["data"]["written"] = True
            result["data"]["size"] = restored_size
            result["data"]["restored_meaningful_snapshot"] = True
        else:
            result["path"] = str(target_path)
            result["written"] = True
            result["size"] = restored_size
        result["restored_meaningful_snapshot"] = True
        result["warning"] = (
            "Support-lane file_ops write would have truncated a meaningful support artifact; "
            "restored the latest non-empty snapshot instead."
        )
        return result

    def _builder_guard_html_write_result(
        self,
        *,
        node: Optional[Dict[str, Any]],
        input_data: str,
        tool_action: str,
        result: Any,
        snapshot_cache: Dict[str, Dict[str, Any]],
    ) -> Any:
        if normalize_node_role(str((node or {}).get("type") or "").strip()) != "builder":
            return result
        if str(tool_action or "").strip().lower() != "write" or not isinstance(result, dict):
            return result
        if not bool(result.get("success")):
            return result
        if not self._tool_result_has_write(result):
            return result

        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        raw_path = str(data.get("path") or result.get("path") or "").strip()
        target_path = self._builder_resolved_output_path(node, raw_path)
        if not target_path or target_path.suffix.lower() not in {".html", ".htm"}:
            return result
        if not target_path.exists() or not target_path.is_file():
            return result

        try:
            html = target_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return result

        rejection_reason = self._builder_html_write_rejection_reason(
            html,
            input_data=input_data,
            filename=target_path.name,
        )
        if not rejection_reason:
            snapshot_cache[str(target_path)] = {
                "content": html,
                "size": len(html.encode("utf-8")),
            }
            return result

        cache_key = str(target_path)
        cached = snapshot_cache.get(cache_key) or {}
        restored_snapshot = False
        removed_rejected_file = False
        restored_size = 0
        restored_text = str(cached.get("content") or "")
        if restored_text.strip():
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(restored_text, encoding="utf-8")
                restored_size = int(target_path.stat().st_size)
                snapshot_cache[cache_key] = {
                    "content": restored_text,
                    "size": restored_size,
                }
                restored_snapshot = True
            except Exception:
                restored_snapshot = False
        if not restored_snapshot:
            try:
                target_path.unlink(missing_ok=True)
                removed_rejected_file = True
            except Exception:
                removed_rejected_file = False

        error = f"Shipped HTML write rejected: {rejection_reason}."
        if restored_snapshot:
            error += " Restored the last accepted snapshot instead of keeping contamination."
        elif removed_rejected_file:
            error += " Removed the rejected HTML file so it cannot contaminate downstream nodes."
        else:
            error += " Overwrite the same assigned file immediately with a valid artifact."

        result["success"] = False
        result["written"] = False
        result["error"] = error
        result["rejected_html_write"] = True
        result["html_rejection_reason"] = rejection_reason
        if isinstance(result.get("data"), dict):
            result["data"]["written"] = False
            result["data"]["rejected_html_write"] = True
            result["data"]["html_rejection_reason"] = rejection_reason
            result["data"]["restored_meaningful_snapshot"] = restored_snapshot
            result["data"]["removed_rejected_file"] = removed_rejected_file
            result["data"]["size"] = restored_size if restored_snapshot else 0
        logger.warning(
            "Rejected builder HTML write for %s: %s (restored=%s removed=%s)",
            target_path.name,
            rejection_reason,
            restored_snapshot,
            removed_rejected_file,
        )
        return result

    def _apply_runtime_node_contracts(self, node: Optional[Dict[str, Any]], input_data: str) -> str:
        text = str(input_data or "")
        if normalize_node_role(str((node or {}).get("type") or "").strip()) != "builder":
            return text

        allowed_targets = self._builder_allowed_html_targets_from_node(node)
        stage_root_index_only = self._builder_stage_root_index_only_node(node)
        staging_output_dir = self._builder_staging_output_dir_from_node(node)
        if not allowed_targets:
            if self._builder_is_support_lane_node(node):
                contract = "\n".join(
                    [
                        "[BUILDER RUNTIME SUPPORT CONTRACT]",
                        "This builder is a support lane for the current run.",
                        "Do NOT emit or overwrite /tmp/evermind_output/index.html in this run.",
                        "Write browser-native support artifacts only under /tmp/evermind_output/, such as /tmp/evermind_output/js/weaponSystem.js, /tmp/evermind_output/css/hud.css, or /tmp/evermind_output/data/encounters.json.",
                        "Support JS must be browser-native and must not use CommonJS exports or require(...).",
                    ]
                )
                if contract not in text:
                    return f"{text.rstrip()}\n\n{contract}".strip()
            return text

        if stage_root_index_only and "index.html" in allowed_targets and staging_output_dir:
            staging_contract = "\n".join(
                [
                    "[BUILDER ROOT STAGING CONTRACT]",
                    "This builder owns the primary root shell for an upstream parallel pass, but the live preview must wait for merger.",
                    f"Do NOT overwrite the live root artifact at {self._current_output_dir().rstrip('/')}/index.html in this pass.",
                    f"Write or patch your full root-shell files under {staging_output_dir}/ instead, with {staging_output_dir}/index.html as the staged entry.",
                    "Merger will publish the final live root only after all parallel builder lanes finish.",
                ]
            )
            if staging_contract not in text:
                text = f"{text.rstrip()}\n\n{staging_contract}".strip()

        existing_targets = self._builder_assigned_html_targets(text)
        if existing_targets == allowed_targets:
            return text

        contract_lines = [
            "[BUILDER RUNTIME TARGET CONTRACT]",
            f"{BUILDER_TARGET_OVERRIDE_MARKER} {', '.join(allowed_targets)}",
            "Assigned HTML filenames for this builder: " + ", ".join(allowed_targets) + ".",
        ]
        if len(allowed_targets) >= 2:
            contract_lines.append("Shared asset filenames for this run: styles.css, app.js.")
        if not bool((node or {}).get("can_write_root_index")) and "index.html" not in allowed_targets:
            contract_lines.append("Do NOT emit index.html or any unassigned HTML filename in this run.")
        contract = "\n".join(contract_lines)
        if contract in text:
            return text
        return f"{text.rstrip()}\n\n{contract}".strip()

    def _builder_is_support_lane(self, input_data: str) -> bool:
        text = str(input_data or "")
        lower = text.lower()
        merger_like = bool(
            re.search(r"\b(?:final merger|merger|integrator|integration|assemble|assembly|merge)\b", lower)
        )
        if merger_like:
            return False
        markers = (
            "support-lane builder",
            "non-overlapping support subsystem",
            "support js/css/json",
            "support files",
            "root index.html unless explicitly reassigned",
            "do not overwrite /tmp/evermind_output/index.html",
            "builder 2 ships non-overlapping support systems/files",
            "browser-native support files",
        )
        return any(marker in lower for marker in markers)

    def _builder_support_lane_targets(self, input_data: str) -> List[str]:
        text = str(input_data or "")
        targets: List[str] = []

        def _add(raw_name: str) -> None:
            raw = str(raw_name or "").strip().strip("\"'`")
            if not raw:
                return
            cleaned = raw.replace("\\", "/").lstrip("/")
            if cleaned.startswith("tmp/evermind_output/"):
                cleaned = cleaned[len("tmp/evermind_output/") :]
            elif cleaned.startswith("/tmp/evermind_output/"):
                cleaned = cleaned[len("/tmp/evermind_output/") :]
            parts = [part for part in cleaned.split("/") if part not in ("", ".", "..")]
            if not parts:
                return
            suffix = Path(parts[-1]).suffix.lower()
            if suffix not in {".js", ".mjs", ".css", ".json"}:
                return
            candidate = "/".join(parts)
            if candidate not in targets:
                targets.append(candidate)

        for match in re.findall(r"([A-Za-z0-9_./-]+\.(?:js|mjs|css|json))", text, re.IGNORECASE):
            _add(match)

        if targets:
            return targets[:8]

        try:
            task_type = task_classifier.classify(text).task_type
        except Exception:
            task_type = ""

        fallback = (
            [
                "js/weaponSystem.js",
                "js/enemyAI.js",
                "js/effectsManager.js",
                "js/hudController.js",
                "css/hud.css",
                "assets/manifest.json",
            ]
            if task_type == "game"
            else [
                "js/support.js",
                "css/support.css",
                "data/support.json",
            ]
        )
        return fallback

    def _builder_support_lane_write_prompt(
        self,
        input_data: str,
        *,
        opener: str,
        note: str = "",
    ) -> str:
        output_dir = self._current_output_dir().rstrip("/")
        targets = self._builder_support_lane_targets(input_data)
        absolute_targets = ", ".join(f"{output_dir}/{name}" for name in targets[:6])
        first_target = f"{output_dir}/{targets[0]}" if targets else f"{output_dir}/js/support.js"
        note_line = f"{note.strip()} " if str(note or "").strip() else ""
        return (
            f"{opener} "
            f"{note_line}"
            "This builder lane owns browser-native JS/CSS/JSON support artifacts, not the shipped root HTML. "
            f"Do NOT write or overwrite {output_dir}/index.html in this lane. "
            f"Your VERY NEXT response must be one or more file_ops write calls that create or patch non-empty support files under {output_dir}/, for example: {absolute_targets}. "
            "Patch existing support modules in place when they already exist, preserve browser-facing APIs/symbols when possible, "
            "and never replace a meaningful support file with an empty stub. "
            f"If you need a concrete first target, start with {first_target}. "
            "Do not return prose before the file writes are saved."
        )

    def _builder_assigned_html_targets(self, input_data: str) -> List[str]:
        text = str(input_data or "")
        targets: List[str] = []

        def _add(raw_name: str) -> None:
            name = Path(str(raw_name or "").strip()).name
            if not name:
                return
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html?", name, re.IGNORECASE):
                return
            if name.lower().startswith("index_part"):
                return
            if name not in targets:
                targets.append(name)

        override_targets: List[str] = []
        for block in re.findall(
            r"html target override(?:[^\n:]*)?:\s*([^\n]+)",
            text,
            re.IGNORECASE,
        ):
            for match in re.findall(r"([A-Za-z0-9][A-Za-z0-9._/-]*\.html?)", block, re.IGNORECASE):
                name = Path(str(match or "").strip()).name
                if name and name not in override_targets:
                    override_targets.append(name)
        if override_targets:
            return override_targets

        positive_index_patterns = [
            r"must create\s+/tmp/evermind_output/index\.html",
            r"save to\s+/tmp/evermind_output/index\.html\s+via file_ops write",
            r"save final html via file_ops write to\s+/tmp/evermind_output/index\.html",
        ]
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in positive_index_patterns):
            _add("index.html")

        for block in re.findall(
            r"fallback set(?:[^\n:]*)?:\s*([^\n]+)",
            text,
            re.IGNORECASE,
        ):
            for match in re.findall(r"([A-Za-z0-9][A-Za-z0-9._/-]*\.html?)", block, re.IGNORECASE):
                _add(match)

        for block in re.findall(
            r"assigned html filenames?(?:[^\n:]*)?:\s*([^\n]+)",
            text,
            re.IGNORECASE,
        ):
            for match in re.findall(r"([A-Za-z0-9][A-Za-z0-9._/-]*\.html?)", block, re.IGNORECASE):
                _add(match)

        for match in re.findall(r"for example\s+([A-Za-z0-9][A-Za-z0-9._/-]*\.html?)", text, re.IGNORECASE):
            _add(match)

        return targets

    def _builder_effective_multifile_target_count(self, input_data: str) -> int:
        assigned_targets = len(self._builder_assigned_html_targets(input_data))
        if assigned_targets > 0:
            return max(1, assigned_targets)
        try:
            requested_pages = task_classifier.requested_page_count(str(input_data or ""))
        except Exception:
            requested_pages = 0
        return max(1, requested_pages)

    def _builder_direct_multifile_budget(self, input_data: str, *, max_tokens: int, timeout_sec: int) -> tuple[int, int]:
        text = str(input_data or "")
        assigned_targets = len(self._builder_assigned_html_targets(text))
        try:
            wants_multi_page = task_classifier.wants_multi_page(text)
        except Exception:
            wants_multi_page = False
        retryish_single_target = (
            assigned_targets == 1
            and not wants_multi_page
            and any(
                marker in text.lower()
                for marker in (
                    "retry 1/",
                    "retry 2/",
                    "retry 3/",
                    "previous attempt failed",
                    "previous attempt timed out",
                    "quality gate failed",
                    "pitch appears inverted",
                    "yaw appears mirrored",
                )
            )
        )
        if retryish_single_target:
            boosted_tokens = max(
                max_tokens,
                self._read_int_env(
                    "EVERMIND_BUILDER_SINGLE_FILE_REPAIR_MAX_TOKENS",
                    49152,
                    16384,
                    65536,
                ),
            )
            return boosted_tokens, timeout_sec
        if not wants_multi_page and assigned_targets < 3:
            return max_tokens, timeout_sec

        requested_pages = self._builder_effective_multifile_target_count(text)
        boosted_tokens = max_tokens
        boosted_timeout = timeout_sec

        if requested_pages >= 6:
            boosted_tokens = max(
                boosted_tokens,
                self._read_int_env(
                    "EVERMIND_BUILDER_DIRECT_MULTIFILE_LARGE_MAX_TOKENS",
                    14336,
                    4096,
                    16384,
                ),
            )
            boosted_timeout = max(
                boosted_timeout,
                self._read_int_env(
                    "EVERMIND_BUILDER_DIRECT_MULTIFILE_LARGE_TIMEOUT_SEC",
                    420,
                    180,
                    720,
                ),
            )
        elif requested_pages >= 3:
            boosted_tokens = max(
                boosted_tokens,
                self._read_int_env(
                    "EVERMIND_BUILDER_DIRECT_MULTIFILE_MAX_TOKENS",
                    12288,
                    4096,
                    16384,
                ),
            )
            boosted_timeout = max(
                boosted_timeout,
                self._read_int_env(
                    "EVERMIND_BUILDER_DIRECT_MULTIFILE_TIMEOUT_SEC",
                    300,
                    120,
                    600,
                ),
            )

        builder_timeout_cap = max(self._timeout_for_node("builder"), timeout_sec)
        return min(boosted_tokens, 16384), min(boosted_timeout, builder_timeout_cap)

    def _builder_direct_multifile_initial_batch_size(self, input_data: str) -> int:
        text = str(input_data or "")
        requested_pages = self._builder_effective_multifile_target_count(text)
        if requested_pages >= 6:
            return self._read_int_env(
                "EVERMIND_BUILDER_DIRECT_MULTIFILE_LARGE_INITIAL_BATCH_SIZE",
                1,
                1,
                2,
            )
        if requested_pages >= 3:
            return self._read_int_env(
                "EVERMIND_BUILDER_DIRECT_MULTIFILE_INITIAL_BATCH_SIZE",
                2,
                1,
                4,
            )
        return max(1, min(4, requested_pages))

    def _builder_direct_multifile_batch_size(self, input_data: str) -> int:
        text = str(input_data or "")
        requested_pages = self._builder_effective_multifile_target_count(text)
        if requested_pages >= 6:
            return self._read_int_env(
                "EVERMIND_BUILDER_DIRECT_MULTIFILE_LARGE_BATCH_SIZE",
                2,
                1,
                3,
            )
        if requested_pages >= 3:
            return self._read_int_env(
                "EVERMIND_BUILDER_DIRECT_MULTIFILE_BATCH_SIZE",
                2,
                1,
                4,
            )
        return max(1, min(4, requested_pages))

    def _builder_direct_multifile_continuation_limit(self, input_data: str) -> int:
        base_limit = self._read_int_env(
            "EVERMIND_BUILDER_DIRECT_MULTIFILE_CONTINUATIONS",
            3,
            0,
            10,
        )
        assigned_targets = len(self._builder_assigned_html_targets(input_data))
        if assigned_targets <= 0:
            return base_limit
        initial_batch_size = max(1, self._builder_direct_multifile_initial_batch_size(input_data))
        continuation_batch_size = max(1, self._builder_direct_multifile_batch_size(input_data))
        remaining = max(0, assigned_targets - initial_batch_size)
        needed = (
            (remaining + continuation_batch_size - 1) // continuation_batch_size
            if remaining else 0
        )
        return min(10, max(base_limit, needed))

    def _builder_returned_html_targets(self, output_text: str) -> List[str]:
        targets: List[str] = []
        for block in self._builder_named_code_blocks(output_text):
            candidate = str(block.get("filename") or "").strip()
            if not candidate.lower().endswith((".html", ".htm")):
                continue
            if candidate not in targets:
                targets.append(candidate)
        return targets

    def _builder_named_code_blocks(self, output_text: str) -> List[Dict[str, Any]]:
        text = str(output_text or "")
        if not text:
            return []

        closed_pattern = re.compile(
            r"```(?P<lang>html|htm|css|js|javascript)\s+(?P<name>[^\n`]+)\n(?P<code>.*?)(?:\n```)",
            re.IGNORECASE | re.DOTALL,
        )
        open_pattern = re.compile(
            r"```(?P<lang>html|htm|css|js|javascript)\s+(?P<name>[^\n`]+)\n",
            re.IGNORECASE,
        )

        blocks: List[Dict[str, Any]] = []
        closed_spans: List[tuple[int, int]] = []
        for match in closed_pattern.finditer(text):
            header_name = str(match.group("name") or "").strip()
            filename = Path(header_name.split()[0].strip().strip("\"'`")).name
            lang = str(match.group("lang") or "").strip().lower()
            if lang == "javascript":
                lang = "js"
            blocks.append({
                "lang": lang,
                "filename": filename,
                "code": str(match.group("code") or ""),
                "closed": True,
            })
            closed_spans.append(match.span())

        last_closed_end = max((end for _, end in closed_spans), default=0)
        trailing_open = None
        for match in open_pattern.finditer(text):
            start = match.start()
            if any(span_start <= start < span_end for span_start, span_end in closed_spans):
                continue
            if start >= last_closed_end:
                trailing_open = match

        if trailing_open is not None:
            header_name = str(trailing_open.group("name") or "").strip()
            filename = Path(header_name.split()[0].strip().strip("\"'`")).name
            lang = str(trailing_open.group("lang") or "").strip().lower()
            if lang == "javascript":
                lang = "js"
            blocks.append({
                "lang": lang,
                "filename": filename,
                "code": text[trailing_open.end():],
                "closed": False,
            })

        return blocks

    def _builder_html_block_looks_complete(self, code: str, *, closed: bool) -> bool:
        if not closed:
            return False
        lower = str(code or "").lower()
        if "</html>" in lower:
            return True
        if "<html" not in lower and "<!doctype" not in lower:
            return False
        return "</body>" in lower

    def _builder_completed_html_targets(self, output_text: str) -> List[str]:
        targets: List[str] = []
        for block in self._builder_named_code_blocks(output_text):
            candidate = str(block.get("filename") or "").strip()
            if not candidate.lower().endswith((".html", ".htm")):
                continue
            if not self._builder_html_block_looks_complete(
                str(block.get("code") or ""),
                closed=bool(block.get("closed")),
            ):
                continue
            if candidate not in targets:
                targets.append(candidate)
        return targets

    def _builder_missing_html_targets(self, input_data: str, output_text: str) -> List[str]:
        assigned = self._builder_assigned_html_targets(input_data)
        if not assigned:
            return []
        if len(assigned) == 1 and self._builder_text_output_has_persistable_html(output_text, input_data):
            return []
        returned = set(self._builder_completed_html_targets(output_text))
        return [name for name in assigned if name not in returned]

    def _builder_requires_shared_assets(self, input_data: str) -> bool:
        assigned_targets = self._builder_assigned_html_targets(input_data)
        if "index.html" not in assigned_targets:
            return False
        assigned_count = len(assigned_targets)
        try:
            wants_multi_page = task_classifier.wants_multi_page(input_data)
        except Exception:
            wants_multi_page = False
        return assigned_count >= 2 and (wants_multi_page or assigned_count >= 3)

    def _builder_returned_named_assets(self, output_text: str) -> List[str]:
        text = str(output_text or "")
        assets: List[str] = []
        for match in re.finditer(r"```(?:css|js|javascript)\s+([^\n`]+)", text, re.IGNORECASE):
            header = str(match.group(1) or "").strip()
            if not header:
                continue
            candidate = Path(header.split()[0].strip().strip("\"'`")).name
            normalized = candidate.lower()
            if normalized not in {"styles.css", "app.js"}:
                continue
            if normalized not in assets:
                assets.append(normalized)
        return assets

    def _builder_missing_shared_assets(self, input_data: str, output_text: str) -> List[str]:
        if not self._builder_requires_shared_assets(input_data):
            return []
        returned = set(self._builder_returned_named_assets(output_text))
        return [name for name in ("styles.css", "app.js") if name not in returned]

    def _builder_direct_multifile_initial_messages(
        self,
        system_prompt: str,
        input_data: str,
    ) -> List[Dict[str, str]]:
        assigned_targets = self._builder_assigned_html_targets(input_data)
        if not assigned_targets:
            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_data},
            ]
        if len(assigned_targets) == 1:
            target = assigned_targets[0]
            output_dir = self._current_output_dir().rstrip("/")
            prompt = (
                "[DIRECT SINGLE-FILE DELIVERY]\n"
                f"{BUILDER_TARGET_OVERRIDE_MARKER} {target}\n"
                f"Return ONLY one fenced ```html {target}``` block.\n"
                "This is a single-file builder delivery or repair pass, not a multi-page batch.\n"
                "Treat the current output as the source of truth and patch in place instead of re-planning the whole game/site.\n"
                "Preserve working systems and change only what is required by the retry notes or current brief.\n"
                "Keep the file compact enough to finish in one response: remove dead comments, duplicate helper branches, repeated geometry boilerplate, and unused fallback code.\n"
                "Do NOT output prose, planning text, or extra filenames.\n"
                f"Use the exact runtime path under {output_dir}/{target}.\n"
                f"Output ONLY:\n```html {target}\n<!DOCTYPE html>...\n```"
            )
            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"{str(input_data or '').strip()}\n\n{prompt}"},
            ]
        batch_size = self._builder_direct_multifile_initial_batch_size(input_data)
        first_batch = assigned_targets[:batch_size]
        remaining = assigned_targets[batch_size:]
        output_dir = self._current_output_dir().rstrip("/")
        batch_line = ", ".join(first_batch)
        allowed_route_line = ", ".join(assigned_targets)
        target_override_line = f"{BUILDER_TARGET_OVERRIDE_MARKER} {batch_line}"
        shared_assets_required = self._builder_requires_shared_assets(input_data)
        remaining_note = (
            "If more assigned files remain after this batch, another continuation will request them immediately."
            if remaining else
            "This batch covers the full assignment."
        )
        example_blocks_parts: List[str] = []
        if shared_assets_required:
            example_blocks_parts.extend([
                "```css styles.css\n:root { --bg: #08111f; --accent: #d9b36c; }\n```",
                "```js app.js\ndocument.addEventListener('DOMContentLoaded', () => {\n  document.querySelectorAll('[data-reveal]').forEach((el) => el.classList.add('is-ready'));\n});\n```",
            ])
        for name in first_batch[: max(1, min(2, len(first_batch)))]:
            example_blocks_parts.append(
                f"```html {name}\n<!DOCTYPE html>...\n<link rel=\"stylesheet\" href=\"styles.css\">\n<script src=\"app.js\" defer></script>\n```"
            )
        example_blocks = "\n".join(example_blocks_parts)
        shared_asset_contract = ""
        if shared_assets_required:
            shared_asset_contract = (
                "FIRST-BATCH ASSET CONTRACT:\n"
                "- You MUST return ```css styles.css``` and ```js app.js``` in this response before the HTML block(s).\n"
                "- Every HTML page in this batch must link the shared local assets with rel=stylesheet href=\"styles.css\" and script src=\"app.js\" defer.\n"
                "- styles.css must contain the core layout, color, spacing, responsive, and motion system.\n"
                "- app.js must be route-safe: every optional selector must be null-guarded before addEventListener/classList/style access.\n"
            )
        shared_asset_delivery = (
            "Prefer one shared ```css styles.css``` block and one shared ```js app.js``` block in this first batch if the site reuses the same design system or motion logic.\n"
            "Link those shared assets from the HTML pages instead of duplicating the same CSS/JS inside every page.\n"
            "Do NOT inline large CSS or JS blobs into the HTML when shared assets are available.\n"
            "If you use shared app.js, it MUST be route-safe: every queried element needs a null guard before classList/addEventListener/style access, unless every linked page contains the same DOM structure.\n"
            if shared_assets_required else
            "Builder 1 owns the root shared assets for this site. Do NOT invent or overwrite root-level styles.css/app.js in this batch.\n"
        )
        prompt = (
            "[DIRECT MULTI-FILE INITIAL DELIVERY]\n"
            f"{target_override_line}\n"
            "Deliver the assigned HTML files in small batches to reduce stream stalls.\n"
            f"Return ONLY this first batch now: {batch_line}.\n"
            f"The ONLY valid local HTML route set for this site is: {allowed_route_line}.\n"
            "Every internal href that points to a local .html page MUST use one of those exact filenames.\n"
            "Do NOT invent new local routes such as destinations.html, collections.html, services.html, or planning.html unless they are explicitly in that allowed route set.\n"
            "If you need more navigation items than routes, point them to existing assigned pages or same-page #anchors instead of creating new HTML filenames.\n"
            "Any extra HTML filename outside this batch will be discarded and counted as failure.\n"
            "Do NOT try to emit every assigned page in one response.\n"
            "Do NOT output prose, planning text, or explanations.\n"
            f"{shared_asset_contract}"
            f"{shared_asset_delivery}"
            "Keep each page compact and shippable: use concise copy, semantic sections, and avoid giant repeated markup.\n"
            "Do not fill destination, package, gallery, or story modules with a single oversized outline icon; use real media or a rich finished composition instead.\n"
            "Avoid base64 assets, giant SVG path dumps, or repeating the entire site content inside one page.\n"
            "Overwrite the assigned scaffold files by returning final HTML for these exact filenames.\n"
            f"Use runtime paths under {output_dir}/.\n"
            f"{remaining_note}\n"
            "Output ONLY fenced HTML code blocks, for example:\n"
            f"{example_blocks}"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{str(input_data or '').strip()}\n\n{prompt}"},
        ]

    def _builder_direct_multifile_continuation_messages(
        self,
        system_prompt: str,
        input_data: str,
        accumulated_output: str,
        remaining_targets: List[str],
    ) -> List[Dict[str, str]]:
        assigned_targets = self._builder_assigned_html_targets(input_data)
        if len(assigned_targets) == 1:
            target = assigned_targets[0]
            output_dir = self._current_output_dir().rstrip("/")
            prompt = (
                "[DIRECT SINGLE-FILE CONTINUATION]\n"
                f"{BUILDER_TARGET_OVERRIDE_MARKER} {target}\n"
                "The previous response for this single file was truncated by the model token limit.\n"
                f"Return ONE complete replacement ```html {target}``` block only.\n"
                "Do NOT restart the architecture from zero. Preserve the working gameplay/site shell and apply only the targeted repair.\n"
                "Keep the file compact enough to finish in one response: trim duplicate sections, dead helper code, repeated geometry comments, and unused fallback branches.\n"
                "Do NOT emit prose, planning text, or any extra filename.\n"
                f"Use the exact runtime path under {output_dir}/{target}.\n"
                f"Output ONLY:\n```html {target}\n<!DOCTYPE html>...\n```"
            )
            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"{str(input_data or '').strip()}\n\n{prompt}"},
            ]
        delivered_targets = self._builder_returned_html_targets(accumulated_output)
        batch_size = self._builder_direct_multifile_batch_size(input_data)
        next_batch = remaining_targets[:batch_size]
        output_dir = self._current_output_dir().rstrip("/")
        remaining_line = ", ".join(next_batch[:12])
        allowed_route_line = ", ".join(assigned_targets)
        target_override_line = f"{BUILDER_TARGET_OVERRIDE_MARKER} {remaining_line}" if remaining_line else ""
        delivered_line = ", ".join(delivered_targets[:12]) if delivered_targets else "none"
        missing_shared_assets = self._builder_missing_shared_assets(input_data, accumulated_output)
        example_blocks_parts: List[str] = []
        if missing_shared_assets:
            if "styles.css" in missing_shared_assets:
                example_blocks_parts.append("```css styles.css\n:root { --bg: #08111f; --accent: #d9b36c; }\n```")
            if "app.js" in missing_shared_assets:
                example_blocks_parts.append("```js app.js\ndocument.addEventListener('DOMContentLoaded', () => {\n  document.querySelectorAll('[data-reveal]').forEach((el) => el.classList.add('is-ready'));\n});\n```")
        for name in next_batch[: max(1, min(2, len(next_batch)))]:
            example_blocks_parts.append(
                f"```html {name}\n<!DOCTYPE html>...\n<link rel=\"stylesheet\" href=\"styles.css\">\n<script src=\"app.js\" defer></script>\n```"
            )
        example_blocks = "\n".join(example_blocks_parts)
        shared_asset_note = (
            "Shared assets are still missing from earlier batches: "
            + ", ".join(missing_shared_assets)
            + ". Return those named asset blocks first in this response, then return the HTML batch below.\n"
            if missing_shared_assets else
            ""
        )
        continuation_prompt = (
            "[DIRECT MULTI-FILE CONTINUATION]\n"
            f"{target_override_line}\n"
            "The previous response stopped before all assigned HTML files were delivered.\n"
            f"Already returned: {delivered_line}.\n"
            f"Continue now with ONLY this next batch of HTML files: {remaining_line}.\n"
            f"The ONLY valid local HTML route set for this site is: {allowed_route_line}.\n"
            "Rewrite or remove any local href that points to a non-assigned HTML filename; use an existing assigned page or a same-page #anchor instead.\n"
            f"{shared_asset_note}"
            "Do NOT restart from index.html unless index.html is still in the remaining list.\n"
            "Do NOT repeat files that were already returned.\n"
            "Do NOT skip ahead to future batches.\n"
            "Any extra HTML filename outside this batch will be discarded and counted as failure.\n"
            "Return ONLY the next HTML file(s) in this batch.\n"
            "Do NOT re-emit styles.css or app.js unless you are intentionally revising those shared assets.\n"
            "Do NOT inline large CSS or JS into this continuation page; rely on the shared assets already delivered.\n"
            "Maintain route-safe shared JS across all pages; do not assume a selector exists on every page unless the markup is truly consistent.\n"
            "Keep the continuation compact: avoid giant repeated sections, base64 assets, long placeholder essays, or duplicated site-wide markup.\n"
            "Do not leave media-led sections as icon-only cards or thin decorative shells; repair them with real imagery or a finished visual composition.\n"
            "If one page starts becoming excessively long, simplify the copy and section count before returning it.\n"
            "Keep the same design system, content direction, navigation, and motion language.\n"
            "Output ONLY fenced code blocks, one per remaining file, for example:\n"
            f"{example_blocks}\n"
            f"Use the exact runtime paths under {output_dir}/ and no extra prose."
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{str(input_data or '').strip()}\n\n{continuation_prompt}"},
        ]

    def _response_finish_reason(self, response: Any) -> str:
        try:
            choice = response.choices[0]
        except Exception:
            return ""
        if isinstance(choice, dict):
            return str(choice.get("finish_reason") or "")
        return str(getattr(choice, "finish_reason", "") or "")

    def _runtime_output_prompt_block(self) -> str:
        current = self._current_output_dir()
        scratch = self._current_scratchpad_dir()
        return (
            "\n\nRUNTIME OUTPUT CONTRACT:\n"
            f"- Current output directory: {current}\n"
            "- /tmp/evermind_output may be a compatibility alias, but you must treat the current output directory as the source of truth.\n"
            "- Do not inherit stale placeholder files from any other directory.\n"
            f"\nSCRATCHPAD DIRECTORY: {scratch}\n"
            "- Use this for temporary or intermediate files (draft CSS, test data, asset manifests) that should NOT be in the final deliverables.\n"
            "- Create this directory with file_ops if it does not exist yet.\n"
            "- Only use /tmp if you explicitly need system-level temp storage.\n"
        )

    def _builder_forced_text_prompt(self, input_data: str) -> str:
        assigned_targets = self._builder_assigned_html_targets(input_data)
        support_lane = self._builder_is_support_lane(input_data) and not assigned_targets
        assigned_line = ""
        exact_paths_line = ""
        forbidden_index_line = ""
        example_blocks = ""
        game_contract = ""
        try:
            if task_classifier.classify(input_data).task_type == "game":
                game_contract = (
                    "GAME FINAL DELIVERY CONTRACT: "
                    "include a visible start/play flow, gameplay surface (canvas/arena/viewport), "
                    "requestAnimationFrame loop, keyboard/pointer/touch bindings, HUD/progression UI, "
                    "and a fail/win/restart path. "
                    "Bind gameplay input in script with addEventListener or an equivalent runtime hook; "
                    "do not output a visually polished but non-playable shell. "
                )
        except Exception:
            game_contract = ""
        if assigned_targets:
            assigned_line = (
                "Your assigned HTML filenames are: "
                + ", ".join(assigned_targets)
                + ". "
            )
            output_dir = self._current_output_dir().rstrip("/")
            exact_paths_line = (
                "Use these exact runtime output paths: "
                + ", ".join(f"{output_dir}/{name}" for name in assigned_targets[:8])
                + ". "
            )
            if "index.html" not in assigned_targets:
                forbidden_index_line = (
                    "Do NOT emit ```html index.html``` or any other unassigned filename; "
                    "those blocks will be discarded and counted as failure. "
                )
            example_targets = assigned_targets[: max(1, min(2, len(assigned_targets)))]
            example_blocks = "\n".join(
                f"```html {name}\n<!DOCTYPE html>...\n```"
                for name in example_targets
            )
        if task_classifier.wants_multi_page(input_data):
            if assigned_targets:
                delivery_line = (
                    "Return ONLY the assigned HTML files listed above. "
                    "Do not invent extra slugs and do not omit any assigned file."
                )
            else:
                count = task_classifier.requested_page_count(input_data)
                if count > 1:
                    delivery_line = f"Return index.html plus at least {count - 1} additional linked HTML files."
                else:
                    delivery_line = "Return index.html plus the additional linked HTML files required by the brief."
                example_blocks = (
                    "```html index.html\n<!DOCTYPE html>...\n```\n"
                    "```html page-2.html\n<!DOCTYPE html>...\n```"
                )
            return (
                "You have used all your tool calls. Now output ONLY the final files as text. "
                "This is a MULTI-PAGE website request, so do NOT collapse it into one long page. "
                f"{game_contract}"
                f"{assigned_line}"
                f"{exact_paths_line}"
                f"{forbidden_index_line}"
                f"{delivery_line} "
                "Use EXACTLY one fenced code block per file with the filename in the fence header, for example:\n"
                f"{example_blocks}\n"
                "Unnamed ```html``` blocks are invalid and will be discarded. "
                "If you need shared assets, you may also output blocks like ```css styles.css``` or ```js app.js```. "
                "Every HTML file must be full, standalone, and linked by working navigation. "
                "Do NOT add explanations before or after the file blocks."
            )
        if support_lane:
            support_targets = self._builder_support_lane_targets(input_data)
            output_dir = self._current_output_dir().rstrip("/")
            example_blocks = "\n".join(
                (
                    f"```{('css' if name.endswith('.css') else 'json' if name.endswith('.json') else 'js')} {name}\n"
                    + ("{ \"status\": \"ready\" }\n" if name.endswith(".json") else "/* implementation */\n")
                    + "```"
                )
                for name in support_targets[:3]
            )
            return (
                "You have used all your tool calls. Now output ONLY the final support files as text. "
                "This is a support-lane builder pass for a single-entry app/game, so do NOT emit a root index.html rewrite. "
                f"{game_contract}"
                f"Use runtime paths under {output_dir}/ such as: "
                + ", ".join(f"{output_dir}/{name}" for name in support_targets[:6])
                + ". "
                "Return ONLY non-empty browser-native JS/CSS/JSON files that deepen the assigned subsystem. "
                "Use EXACTLY one fenced code block per file with the filename in the fence header, for example:\n"
                f"{example_blocks}\n"
                "Do NOT output prose, unnamed blocks, or any ```html index.html``` block."
            )
        return (
            "You have used all your tool calls. Now output the COMPLETE HTML code directly as text. "
            f"{game_contract}"
            "If you are repairing or continuing an existing partial page, output the FULL merged file from <!DOCTYPE html> to </html>, not only the tail fragment or patch chunk. "
            "Start with <!DOCTYPE html> and end with </html>. "
            "Put it inside a ```html code block. Do NOT describe it — output the full code NOW."
        )

    def _builder_tool_results_summary(self, tool_results: List[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for result in (tool_results or [])[-8:]:
            if not isinstance(result, dict):
                continue
            data = result.get("data")
            error = str(result.get("error") or "").strip()
            if isinstance(data, dict):
                path = str(data.get("path") or "").strip()
                label = Path(path).name if path else ""
                if bool(data.get("written")):
                    lines.append(f"- Wrote file: {label or path or 'unknown file'}")
                    continue
                if "content" in data and path:
                    lines.append(f"- Read file: {label}")
                    continue
                entries = data.get("entries")
                if isinstance(entries, list) and path:
                    sample = [
                        str(item.get("name") or "").strip()
                        for item in entries[:8]
                        if isinstance(item, dict) and str(item.get("name") or "").strip()
                    ]
                    if sample:
                        lines.append(f"- Listed {label or path}: {', '.join(sample)}")
                        continue
            if error:
                lines.append(f"- Tool error: {error[:180]}")
        return "\n".join(lines[:8]).strip()

    def _builder_forced_text_messages(
        self,
        system_prompt: str,
        input_data: str,
        tool_results: List[Dict[str, Any]],
        output_text: str,
        force_text_reason: str,
    ) -> List[Dict[str, str]]:
        context_parts: List[str] = []
        reason = str(force_text_reason or "").strip()
        if reason:
            context_parts.append(f"Forced final delivery reason: {reason}.")
        tool_summary = self._builder_tool_results_summary(tool_results)
        if tool_summary:
            context_parts.append("Workspace/tool summary:\n" + tool_summary)
        partial = self._truncate_text(str(output_text or "").strip(), 1600)
        if partial:
            context_parts.append(
                "Incomplete prior draft for salvage only. Reuse any good ideas, but output the final files directly now:\n"
                + partial
            )

        user_content = str(input_data or "").strip()
        if context_parts:
            user_content += "\n\n[FORCED FINAL DELIVERY CONTEXT]\n" + "\n\n".join(context_parts)
        user_content += "\n\n" + self._builder_forced_text_prompt(input_data)
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

    def _plain_text_node_write_guard_error(self, node_type: str) -> str:
        normalized = normalize_node_role(node_type)
        if normalized == "analyst":
            return (
                "Analyst nodes must NOT write code files. "
                "Produce your research report as plain text content in your response using the required XML tags. "
                "Do NOT use file_ops write."
            )
        if normalized == "scribe":
            return (
                "Scribe nodes must NOT write files in this workflow stage. "
                "Produce your documentation or content handoff as plain text in your response. "
                "Do NOT use file_ops write."
            )
        if normalized == "uidesign":
            return (
                "UIDesign nodes must NOT write production code files. "
                "Produce your design brief as plain text in your response. "
                "Do NOT use file_ops write."
            )
        return "This node must produce plain text in its response instead of file_ops write."

    def _plain_text_node_final_prompt(self, node_type: str) -> str:
        normalized = normalize_node_role(node_type)
        if normalized == "analyst":
            return (
                "Output ONLY the final analyst handoff as plain text using ALL required XML tags.\n"
                "Do NOT call any tools.\n"
                "Use ONLY the sources already gathered. If the collected sources are insufficient, state that explicitly in <risk_register> instead of inventing citations.\n"
                "Do NOT write HTML, CSS, or JavaScript."
            )
        if normalized == "scribe":
            return (
                "Output ONLY the final scribe handoff as plain text.\n"
                "Do NOT call any tools.\n"
                "Do NOT invent XML tags unless the prompt already required them.\n"
                "Do NOT write files or source code in this response."
            )
        if normalized == "uidesign":
            return (
                "Output ONLY the final UI design brief as plain text.\n"
                "Do NOT call any tools.\n"
                "Do NOT write production HTML, CSS, JavaScript, or files."
            )
        return "Output ONLY the final plain-text handoff. Do NOT call any tools."

    def _plain_text_node_output_is_sufficient(self, node_type: str, output_text: str) -> bool:
        text = str(output_text or "").strip()
        if len(text) < 80:
            return False
        normalized = normalize_node_role(node_type)
        if normalized != "analyst":
            return True
        lower = text.lower()
        required_tags = (
            "<reference_sites>",
            "<design_direction>",
            "<non_negotiables>",
            "<deliverables_contract>",
            "<risk_register>",
            "<builder_1_handoff>",
            "<builder_2_handoff>",
            "<reviewer_handoff>",
            "<tester_handoff>",
            "<debugger_handoff>",
        )
        return all(tag in lower for tag in required_tags)

    def _plain_text_node_output_is_materializable(self, node_type: str, output_text: str) -> bool:
        text = str(output_text or "").strip()
        if not text:
            return False
        if self._plain_text_node_output_is_sufficient(node_type, text):
            return True
        normalized = normalize_node_role(node_type)
        if normalized != "analyst":
            return False
        lower = text.lower()
        essential_tags = (
            "<reference_sites>",
            "<design_direction>",
            "<non_negotiables>",
            "<deliverables_contract>",
            "<risk_register>",
        )
        downstream_tags = (
            "<builder_1_handoff>",
            "<builder_2_handoff>",
            "<reviewer_handoff>",
            "<tester_handoff>",
            "<debugger_handoff>",
        )
        essential_hits = sum(1 for tag in essential_tags if tag in lower)
        downstream_hits = sum(1 for tag in downstream_tags if tag in lower)
        if essential_hits == len(essential_tags):
            return True
        if len(text) >= 900 and essential_hits >= 4 and downstream_hits >= 2:
            return True
        if len(text) >= 1600 and essential_hits >= 3 and downstream_hits >= 4:
            return True
        return False

    def _plain_text_node_needs_forced_output(
        self,
        node_type: str,
        output_text: str,
        tool_results: List[Dict[str, Any]],
    ) -> bool:
        normalized = normalize_node_role(node_type)
        if normalized not in {"analyst", "scribe", "uidesign"}:
            return False
        if self._plain_text_node_output_is_materializable(node_type, output_text):
            return False
        return bool(tool_results or str(output_text or "").strip())

    def _plain_text_final_timeout_for_node(
        self,
        node_type: str,
        force_text_reason: str = "",
    ) -> int:
        normalized = normalize_node_role(node_type)
        reason = str(force_text_reason or "").strip().lower()
        if normalized == "analyst":
            # V4.2: 35s was too short — analyst generates a full research report
            # after 20+ tool iterations; 60s gives enough headroom for the final
            # synthesis call without wasting budget on a model that gets cut off.
            base = self._read_int_env("EVERMIND_ANALYST_FINAL_SYNTH_TIMEOUT_SEC", 60, 15, 180)
            if reason in {"tool_iterations_exhausted", "missing_final_handoff", "blocked_file_write"}:
                return base
        if normalized in {"scribe", "uidesign"}:
            return self._read_int_env("EVERMIND_PLAIN_TEXT_FINAL_SYNTH_TIMEOUT_SEC", 25, 10, 90)
        return 0

    def _plain_text_node_forced_messages(
        self,
        system_prompt: str,
        input_data: str,
        tool_results: List[Dict[str, Any]],
        output_text: str,
        node_type: str,
        force_text_reason: str,
    ) -> List[Dict[str, str]]:
        context_parts: List[str] = []
        reason = str(force_text_reason or "").strip()
        if reason:
            context_parts.append(f"Forced final handoff reason: {reason}.")
        tool_summary = self._builder_tool_results_summary(tool_results)
        if tool_summary:
            context_parts.append("Workspace/tool summary:\n" + tool_summary)
        partial = self._truncate_text(str(output_text or "").strip(), 1600)
        if partial:
            context_parts.append(
                "Incomplete prior draft for salvage only. Reuse any valid findings, but output the final plain-text handoff now:\n"
                + partial
            )

        user_content = str(input_data or "").strip()
        if context_parts:
            user_content += "\n\n[FORCED FINAL HANDOFF CONTEXT]\n" + "\n\n".join(context_parts)
        user_content += "\n\n" + self._plain_text_node_final_prompt(node_type)
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

    def _reviewer_needs_forced_verdict(
        self,
        node_type: str,
        output_text: str,
        tool_results: List[Dict[str, Any]],
    ) -> bool:
        if normalize_node_role(node_type) != "reviewer":
            return False
        if not str(output_text or "").strip() and not tool_results:
            return False
        return self._review_output_format_followup_reason(node_type, output_text) is not None

    def _reviewer_forced_verdict_messages(
        self,
        system_prompt: str,
        input_data: str,
        tool_results: List[Dict[str, Any]],
        output_text: str,
        force_reason: str,
    ) -> List[Dict[str, str]]:
        context_parts: List[str] = []
        reason = str(force_reason or "").strip()
        if reason:
            context_parts.append(f"Forced reviewer verdict reason: {reason}.")
        tool_summary = self._builder_tool_results_summary(tool_results)
        if tool_summary:
            context_parts.append("QA/tool evidence summary:\n" + tool_summary)
        partial = self._truncate_text(str(output_text or "").strip(), 2200)
        if partial:
            context_parts.append(
                "Malformed prior reviewer draft for salvage only. Reuse its concrete findings, but DO NOT repeat the prose:\n"
                + partial
            )

        user_content = str(input_data or "").strip()
        if context_parts:
            user_content += "\n\n[FORCED REVIEWER VERDICT CONTEXT]\n" + "\n\n".join(context_parts)
        user_content += (
            "\n\nReturn ONLY one strict JSON object. No markdown fences. No prose before or after.\n"
            'Required shape:\n'
            '{"verdict":"APPROVED" or "REJECTED","scores":{"layout":N,"color":N,"typography":N,"animation":N,"responsive":N,"functionality":N,"completeness":N,"originality":N},"ship_readiness":N,"average":N.N,"issues":[],"blocking_issues":[],"missing_deliverables":[],"required_changes":[],"acceptance_criteria":[],"strengths":[]}\n'
            'If the evidence is mixed, incomplete, or uncertain, set "verdict" to "REJECTED" rather than "APPROVED".'
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

    def _builder_partial_text_is_salvageable(self, input_data: str, output_text: str) -> bool:
        text = str(output_text or "").strip()
        if len(text) < 120:
            return False
        lower = text.lower()
        if re.search(r"```(?:html|htm)(?:\s+[^\n`]+\.html?)?", text, re.IGNORECASE):
            return True
        if "<!doctype" in lower or "<html" in lower:
            return True
        if task_classifier.wants_multi_page(input_data):
            return bool(re.search(r"[A-Za-z0-9._/-]+\.html?", text, re.IGNORECASE))
        return False

    def _builder_streaming_tool_call_looks_like_write(self, fn_name: str, args_str: str) -> bool:
        name = str(fn_name or "").strip().lower()
        if name not in {"file_ops", "write_file", "write"}:
            return False

        raw = str(args_str or "")
        if len(raw) < 24:
            return False
        lower = raw.lower()

        if any(
            marker in lower
            for marker in (
                '"action":"list"',
                '"action": "list"',
                '"action":"read"',
                '"action": "read"',
                '"action":"exists"',
                '"action": "exists"',
                '"action":"stat"',
                '"action": "stat"',
                '"action":"mkdir"',
                '"action": "mkdir"',
                '"action":"delete"',
                '"action": "delete"',
            )
        ):
            return False

        recovered_html = self._extract_html_from_truncated_tool_args(raw)
        if recovered_html and len(recovered_html) >= 200:
            return True

        parsed = self._safe_json_object(raw)
        action = str(parsed.get("action", "") or "").strip().lower()
        if action and action != "write":
            return False

        content = str(parsed.get("content", "") or "")
        path = str(parsed.get("path", "") or "")
        if content and self._builder_partial_text_is_salvageable("", content):
            return True
        if (
            path.lower().endswith((".html", ".htm", ".css", ".js", ".mjs"))
            and (content or '"content"' in lower)
        ):
            return True

        return bool(
            any(marker in lower for marker in ('"action":"write"', '"action": "write"'))
            and any(marker in lower for marker in ('"content"', '"path"', ".html"))
        )

    def _prewrite_activity_grace_seconds(self, stall_timeout: float) -> float:
        try:
            stall = float(stall_timeout or 0.0)
        except Exception:
            stall = 0.0
        return max(12.0, min(stall / 4.0, 45.0))

    def _builder_pending_write_stream_cap_seconds(
        self,
        prewrite_timeout: float,
        hard_timeout: float,
    ) -> float:
        try:
            prewrite = max(0.0, float(prewrite_timeout or 0.0))
        except Exception:
            prewrite = 0.0
        try:
            hard = max(0.0, float(hard_timeout or 0.0))
        except Exception:
            hard = 0.0
        cap = min(
            max(prewrite * 1.5, 0.5),
            max(0.5, hard / 8.0) if hard > 0 else 180.0,
            180.0,
        )
        return max(0.5, cap)

    def _forcing_text_progress_payload(self, node_type: str, reason: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stage": "forcing_text_output",
            "reason": reason,
        }
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type == "builder":
            payload["builder_delivery_mode"] = "direct_text"
            payload["builder_direct_text"] = True
        return payload

    def _extract_html_from_truncated_tool_args(self, args_str: str) -> Optional[str]:
        """Extract HTML content from a truncated file_ops tool call JSON string.

        When the model calls file_ops with action=write and content=<html>... but
        the JSON is truncated mid-stream (finish=length), the standard json.loads()
        fails. This method uses regex to locate the "content" field in the raw JSON
        string and un-escapes the value to recover usable HTML.

        Returns the extracted HTML or None if extraction failed.
        """
        raw = str(args_str or "")
        if len(raw) < 200:
            return None

        # Look for "content": "..." pattern in the truncated JSON
        # The JSON string has escaped quotes, newlines, etc.
        match = re.search(r'"content"\s*:\s*"', raw)
        if not match:
            return None

        html_start = match.end()
        # Everything after "content": " is the HTML (still JSON-escaped)
        json_escaped_html = raw[html_start:]

        # Remove trailing incomplete JSON (may end with \", unfinished tag, etc.)
        # Strip any trailing unescaped quote + JSON structure
        if json_escaped_html.endswith('"'):
            json_escaped_html = json_escaped_html[:-1]

        # Un-escape JSON string encoding
        try:
            # Wrap in quotes and parse as a JSON string to properly un-escape
            # Add closing quote since the original was truncated
            unescaped = json.loads('"' + json_escaped_html + '"')
        except (json.JSONDecodeError, ValueError):
            # Manual fallback for common escape sequences
            unescaped = json_escaped_html
            unescaped = unescaped.replace('\\"', '"')
            unescaped = unescaped.replace('\\n', '\n')
            unescaped = unescaped.replace('\\t', '\t')
            unescaped = unescaped.replace('\\\\', '\\')

        # Validate it looks like HTML
        lower = unescaped.lower()
        if '<html' not in lower and '<!doctype' not in lower and '<head' not in lower:
            return None

        return unescaped

    def _script_tag_balance_issues(self, text: str) -> tuple[int, int]:
        blob = str(text or "")
        if not blob:
            return 0, 0
        depth = 0
        stray_closers = 0
        token_re = re.compile(r"<script\b[^>]*>|</script\s*>", re.IGNORECASE)
        for match in token_re.finditer(blob):
            token = str(match.group(0) or "").lower()
            if token.startswith("</script"):
                if depth <= 0:
                    stray_closers += 1
                else:
                    depth -= 1
            else:
                depth += 1
        return stray_closers, depth

    def _normalize_builder_text_html(
        self,
        html_content: str,
        *,
        filename: str = "index.html",
        input_data: str = "",
    ) -> str:
        normalized = str(html_content or "").strip()
        if not normalized:
            return ""

        lower = normalized.lower()
        starts = [idx for idx in (lower.find("<!doctype"), lower.find("<html")) if idx >= 0]
        if starts:
            start = min(starts)
            if start > 0:
                normalized = normalized[start:].lstrip()

        try:
            task_type = task_classifier.classify(input_data).task_type if str(input_data or "").strip() else "website"
        except Exception:
            task_type = "website"

        try:
            normalized = postprocess_generated_text(
                normalized,
                filename=filename,
                task_type=task_type,
            )
        except Exception as exc:
            logger.warning("Failed to normalize extracted builder HTML %s: %s", filename, str(exc)[:200])
        return str(normalized or "").strip()

    def _builder_game_text_continuation_reason(self, output_text: str, input_data: str = "") -> str:
        text = str(output_text or "")
        lower = text.lower()
        if len(text) < 1000:
            return ""
        try:
            task_type = task_classifier.classify(input_data).task_type
        except Exception:
            task_type = ""
        if task_type not in ("game", "creative"):
            return ""

        has_runtime_surface = bool(re.search(
            r"<canvas\b|new\s+three\.|three\.webglrenderer|phaser\.game|pixi\.application|gamecanvas",
            lower,
            re.IGNORECASE,
        ))

        has_runtime_loop = bool(re.search(
            r"requestanimationframe|setanimationloop|ticker\.add|runrenderloop|gameloop|renderloop|updateloop|animate\s*\(",
            lower,
            re.IGNORECASE,
        ))

        body_match = re.search(r"<body\b[^>]*>(.*?)</body>", text, re.IGNORECASE | re.DOTALL)
        body_markup = body_match.group(1) if body_match else ""
        body_lower = body_markup.lower()
        has_body_tag = "<body" in lower
        body_signal_markup = bool(re.search(
            r"<canvas\b|<button\b|<main\b|<section\b|<div\b|id\s*=\s*['\"][^'\"]*(?:game|canvas|hud|menu|overlay|screen|crosshair|weapon|ammo|health|wave|score)",
            body_lower,
            re.IGNORECASE,
        ))
        if (
            not has_body_tag
            or len(body_markup.strip()) < 80
            or (not body_signal_markup and not has_runtime_loop)
        ):
            return "missing_game_shell"
        if not has_runtime_surface and bool(re.search(
            r"startgame|startbtn|playbtn|startoverlay|start-screen|startscreen|hud|weapon|ammo|health|wave|score|enemy|monster|boss|第三人称|射击|shooter|tps|fps",
            lower,
            re.IGNORECASE,
        )):
            return "missing_runtime_surface"

        stray_script_closers, missing_script_closers = self._script_tag_balance_issues(text)
        if stray_script_closers > 0 or missing_script_closers > 0:
            return "unfinished_script_block"

        inline_start_handler = bool(re.search(
            r"onclick\s*=\s*['\"][^'\"]*startgame\s*\(",
            lower,
            re.IGNORECASE,
        ))
        start_handler_defined = bool(re.search(
            r"(?:function\s+startgame\b|window\.startgame\s*=|globalthis\.startgame\s*=|self\.startgame\s*=|(?:const|let|var)\s+startgame\s*=)",
            lower,
            re.IGNORECASE,
        ))
        if inline_start_handler and not start_handler_defined:
            return "missing_start_handler"

        has_start_flow = bool(re.search(
            r"startgame|startbtn|playbtn|startoverlay|start-screen|startscreen",
            lower,
            re.IGNORECASE,
        ))
        if has_start_flow and not has_runtime_loop:
            return "missing_game_loop"

        return ""

    def _builder_game_text_continuation_prompt(self, reason: str) -> str:
        if reason == "missing_game_shell":
            return (
                "The game page shell started but the playable body markup is still incomplete. "
                "Reconstruct and output the COMPLETE merged HTML document from <!DOCTYPE html> to </html>. "
                "Reuse the existing shell as needed instead of returning only a tail fragment. "
                "The finished file must include a visible start screen, gameplay viewport/canvas, HUD, control hints, "
                "the missing runtime JavaScript, and valid closing tags. "
                "Output ONLY the HTML document, no markdown fences, no explanation."
            )
        if reason == "missing_runtime_surface":
            return (
                "The page currently has menu/style scaffolding, but the playable runtime surface is still missing. "
                "Reconstruct and output the COMPLETE merged HTML document from <!DOCTYPE html> to </html>. "
                "Keep the strongest existing menu/HUD shell, then add the gameplay viewport/canvas, runtime JavaScript, "
                "start handler, controls, requestAnimationFrame loop, and valid closing tags. "
                "Do NOT return only a CSS block or JS tail fragment. "
                "Output ONLY the HTML document, no markdown fences, no explanation."
            )
        if reason == "missing_start_handler":
            return (
                "Continue from EXACTLY where you stopped. Do NOT repeat any previous content. "
                "The HTML already references startGame()/restartGame(), but the handler code is still missing. "
                "Write the remaining JavaScript runtime, define the missing gameplay entry handlers, and finish the game loop/input logic. "
                "Output ONLY the continuation code, no markdown fences, no explanation."
            )
        if reason == "missing_game_loop":
            return (
                "Continue from EXACTLY where you stopped. Do NOT repeat any previous content. "
                "The game UI/runtime shell is present, but the gameplay loop/runtime code is still incomplete. "
                "Write the remaining JavaScript loop, state updates, and input-driven gameplay code, then finish any missing closing tags if needed. "
                "Output ONLY the continuation code, no markdown fences, no explanation."
            )
        return (
            "Continue from EXACTLY where you stopped. Do NOT repeat any previous content. "
            "You were mid-JavaScript inside a game HTML document. Continue writing the remaining "
            "runtime code and properly close any open </script>, </body>, and </html> tags. "
            "Output ONLY the continuation code, no markdown fences, no explanation."
        )

    async def _attempt_builder_game_text_continuation(
        self,
        *,
        output_text: str,
        input_data: str,
        system_prompt: str,
        continuation_count: int,
        max_continuations: int,
        request_continuation: Callable[[List[Dict[str, str]]], Any],
        on_progress,
        log_prefix: str = "Builder text-mode",
    ) -> tuple[str, int, Any]:
        text = str(output_text or "")
        if len(text) <= 1000 or continuation_count >= max_continuations:
            return text, continuation_count, None

        continuation_reason = self._builder_game_text_continuation_reason(text, input_data)
        if not continuation_reason:
            return text, continuation_count, None

        continuation_count += 1
        lower = text.lower()
        logger.info(
            "%s incomplete game runtime detected (%s): %d <script> opens, %d </script> closes — attempting continuation (attempt %d)",
            log_prefix,
            continuation_reason,
            lower.count("<script"),
            lower.count("</script>"),
            continuation_count,
        )

        cont_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._builder_game_text_continuation_prompt(continuation_reason)},
            {"role": "assistant", "content": text[-3000:]},
        ]
        try:
            cont_resp = await request_continuation(cont_messages)
        except Exception as cont_exc:
            logger.warning("%s continuation failed: %s", log_prefix, str(cont_exc)[:200])
            return text, continuation_count, None

        cont_content = str(getattr(cont_resp.choices[0].message, "content", "") or "").strip()
        if not cont_content or len(cont_content) <= 200:
            return text, continuation_count, cont_resp

        cont_lower = cont_content.lower()
        if "```html" in cont_lower or "<!doctype" in cont_lower or "<html" in cont_lower:
            text = cont_content
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += cont_content
        logger.info(
            "%s continuation succeeded: +%d chars (total: %d)",
            log_prefix,
            len(cont_content),
            len(text),
        )
        await self._publish_partial_output(on_progress, text, phase="finalizing")
        return text, continuation_count, cont_resp

    def _extract_html_files_from_text_output(
        self, output_text: str, input_data: str = ""
    ) -> Dict[str, str]:
        """Extract HTML files from builder text output (markdown code blocks).

        When Kimi K2.5 outputs HTML as markdown instead of calling file_ops,
        the output_text contains code blocks like:

            ```html index.html
            <!DOCTYPE html>
            <html>...
            ```

        This method parses all such blocks and returns {filename: html_content}.
        """
        text = str(output_text or "")
        if len(text) < 120:
            return {}

        files: Dict[str, str] = {}
        assigned_targets = self._builder_assigned_html_targets(input_data)
        default_filename = assigned_targets[0] if assigned_targets else ""
        input_lower = str(input_data or "").lower()
        forbid_implicit_root_html = any(
            marker in input_lower
            for marker in (
                "do not overwrite /tmp/evermind_output/index.html",
                "do not write /tmp/evermind_output/index.html",
                "do not emit index.html",
                "if index.html is not in your assigned list, do not touch it",
                "your lane owns support js/css/json modules first, not a root index.html rewrite",
            )
        )

        # Pattern 1: ```html filename.html\n...\n```
        # Pattern 2: ```html\n...\n``` (no filename → default to index.html)
        pattern = re.compile(
            r"```(?:html?|htm)\s*([A-Za-z0-9._/-]*\.html?)?\s*\n(.*?)```",
            re.DOTALL | re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            filename = (match.group(1) or "").strip()
            content = match.group(2).strip()
            if not content or len(content) < 120:
                continue
            content_lower = content.lower()
            if '<html' not in content_lower and '<!doctype' not in content_lower:
                continue
            if not filename:
                if forbid_implicit_root_html or not default_filename:
                    continue
                filename = default_filename
            # Sanitize filename
            filename = Path(filename).name
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html?", filename, re.IGNORECASE):
                if forbid_implicit_root_html or not default_filename:
                    continue
                filename = default_filename
            normalized = self._normalize_builder_text_html(
                content,
                filename=filename,
                input_data=input_data,
            )
            if normalized and len(normalized) >= 120:
                files[filename] = normalized

        # Also accept a trailing open named block when the stream was cut off
        # after starting ```html index.html but before the closing fence.
        for block in self._builder_named_code_blocks(text):
            lang = str(block.get("lang") or "").strip().lower()
            if lang not in {"html", "htm"}:
                continue
            filename = Path(str(block.get("filename") or "index.html").strip() or "index.html").name
            content = str(block.get("code") or "").strip()
            if not content or len(content) < 120:
                continue
            content_lower = content.lower()
            if "<html" not in content_lower and "<!doctype" not in content_lower:
                continue
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html?", filename, re.IGNORECASE):
                if forbid_implicit_root_html or not default_filename:
                    continue
                filename = default_filename
            normalized = self._normalize_builder_text_html(
                content,
                filename=filename,
                input_data=input_data,
            )
            if normalized and len(normalized) >= 120:
                existing = files.get(filename, "")
                if len(normalized) > len(existing):
                    files[filename] = normalized

        # Fallback: if no code blocks found but text itself looks like raw HTML
        if not files:
            stripped = text.strip()
            stripped_lower = stripped.lower()
            if (
                ('<html' in stripped_lower or '<!doctype' in stripped_lower)
                and '<body' in stripped_lower
                and len(stripped) > 1000
            ):
                # P1 FIX (Opus): Trim any leading non-HTML text (e.g. system prompt
                # leakage, markdown preamble) before the actual HTML starts.
                html_start = -1
                for marker in ('<!doctype', '<!DOCTYPE', '<html', '<HTML'):
                    pos = stripped.find(marker)
                    if pos >= 0 and (html_start < 0 or pos < html_start):
                        html_start = pos
                if html_start > 0:
                    stripped = stripped[html_start:]
                if forbid_implicit_root_html or not default_filename:
                    return files
                normalized = self._normalize_builder_text_html(
                    stripped,
                    filename=default_filename,
                    input_data=input_data,
                )
                if normalized and len(normalized) >= 120:
                    files[default_filename] = normalized

        if files:
            logger.info(
                "Extracted %s HTML files from builder text output: %s",
                len(files),
                {k: len(v) for k, v in files.items()},
            )
        return files

    def _builder_tool_result_salvage_text(
        self,
        tool_results: List[Dict[str, Any]],
        input_data: str = "",
    ) -> str:
        """Recover HTML from file_ops read results when the builder never lands a real write."""
        if not tool_results:
            return ""

        assigned_targets = self._builder_assigned_html_targets(input_data)
        html_by_name: Dict[str, str] = {}

        for result in tool_results[-12:]:
            if not isinstance(result, dict):
                continue
            data = result.get("data")
            if not isinstance(data, dict):
                continue
            path = str(data.get("path") or "").strip()
            content = str(data.get("content") or "").strip()
            if not path or not content or len(content) < 80:
                continue
            normalized_path = path.replace("\\", "/")
            if "evermind_output" not in normalized_path:
                continue
            filename = Path(path).name
            if Path(filename).suffix.lower() not in (".html", ".htm"):
                continue
            lower = content.lower()
            if "<!doctype" not in lower and "<html" not in lower:
                continue
            existing = html_by_name.get(filename, "")
            if len(content) > len(existing):
                html_by_name[filename] = content

        if not html_by_name:
            return ""

        ordered_names: List[str] = []
        for name in assigned_targets:
            if name in html_by_name and name not in ordered_names:
                ordered_names.append(name)
        for name in sorted(html_by_name):
            if name not in ordered_names:
                ordered_names.append(name)

        return "\n\n".join(
            f"```html {name}\n{html_by_name[name].strip()}\n```"
            for name in ordered_names
            if str(html_by_name.get(name) or "").strip()
        )

    def _builder_stream_tool_call_salvage_text(
        self,
        tool_calls_map: Dict[Any, Dict[str, Any]],
        input_data: str = "",
    ) -> str:
        if not tool_calls_map:
            return ""

        assigned_targets = self._builder_assigned_html_targets(input_data)
        html_by_name: Dict[str, str] = {}

        for key in sorted(tool_calls_map.keys(), key=lambda item: str(item)):
            tc_data = tool_calls_map.get(key) or {}
            fn = tc_data.get("function") if isinstance(tc_data, dict) else {}
            fn_name = str((fn or {}).get("name") or "").strip().lower()
            if fn_name not in {"file_ops", "write_file", "write"}:
                continue
            raw_args = str((fn or {}).get("arguments") or "")
            if len(raw_args) < 24:
                continue

            parsed = self._safe_json_object(raw_args)
            path = str(parsed.get("path") or "").strip()
            content = str(parsed.get("content") or "").strip()
            html = ""
            if path.lower().endswith((".html", ".htm")) and self._builder_partial_text_is_salvageable(input_data, content):
                html = content
            else:
                extracted = self._extract_html_from_truncated_tool_args(raw_args)
                if extracted and self._builder_partial_text_is_salvageable(input_data, extracted):
                    html = extracted
            if not html:
                continue

            filename = Path(path).name if path else ""
            if not filename.lower().endswith((".html", ".htm")):
                filename = assigned_targets[0] if len(assigned_targets) == 1 else "index.html"
            existing = html_by_name.get(filename, "")
            if len(html) > len(existing):
                html_by_name[filename] = html.strip()

        if not html_by_name:
            return ""

        ordered_names: List[str] = []
        for name in assigned_targets:
            if name in html_by_name and name not in ordered_names:
                ordered_names.append(name)
        for name in sorted(html_by_name):
            if name not in ordered_names:
                ordered_names.append(name)

        return "\n\n".join(
            f"```html {name}\n{html_by_name[name].strip()}\n```"
            for name in ordered_names
            if str(html_by_name.get(name) or "").strip()
        )

    def _builder_fragment_looks_incremental(self, output_text: str) -> bool:
        text = str(output_text or "").strip()
        if len(text) < 120:
            return False
        lower = text.lower()
        if "```html" in lower or "<!doctype" in lower or "<html" in lower:
            return False
        return bool(re.search(
            r"<(?:section|div|main|canvas|button|script|style)\b|"
            r"(?:background|color|font-size|font-weight|letter-spacing|position|display|padding|margin|transition|text-transform)\s*:"
            r"|requestanimationframe|function\s+\w+\s*\(|const\s+\w+\s*=",
            text,
            re.IGNORECASE,
        ))

    def _merge_builder_fragment_with_base_html(self, base_html: str, fragment: str) -> str:
        base = str(base_html or "").strip()
        tail = str(fragment or "").strip()
        if len(base) < 200 or not self._builder_fragment_looks_incremental(tail):
            return ""
        base_lower = base.lower()
        if "<!doctype" not in base_lower and "<html" not in base_lower:
            return ""

        merged = base
        if re.search(r"</style>\s*</head>\s*<body\b[^>]*>\s*</body>\s*</html>\s*$", merged, re.IGNORECASE | re.DOTALL):
            merged = re.sub(
                r"</style>\s*</head>\s*<body\b[^>]*>\s*</body>\s*</html>\s*$",
                "",
                merged,
                flags=re.IGNORECASE | re.DOTALL,
            )
        elif re.search(r"</script>\s*</body>\s*</html>\s*$", merged, re.IGNORECASE | re.DOTALL):
            merged = re.sub(
                r"</script>\s*</body>\s*</html>\s*$",
                "",
                merged,
                flags=re.IGNORECASE | re.DOTALL,
            )
        elif (
            re.search(r"<body\b[^>]*>\s*</body>\s*</html>\s*$", merged, re.IGNORECASE | re.DOTALL)
            and re.search(r"<(?:section|div|main|canvas|button|script)\b", tail, re.IGNORECASE)
        ):
            merged = re.sub(r"</body>\s*</html>\s*$", "", merged, flags=re.IGNORECASE | re.DOTALL)
        elif re.search(r"</html>\s*$", merged, re.IGNORECASE):
            merged = re.sub(r"</html>\s*$", "", merged, flags=re.IGNORECASE)

        if merged and not merged.endswith("\n"):
            merged += "\n"
        return merged + tail

    def _builder_disk_salvage_text(
        self,
        input_data: str = "",
        *,
        incremental_text: str = "",
    ) -> str:
        if not self._builder_fragment_looks_incremental(incremental_text):
            return ""

        output_dir = Path(self._current_output_dir())
        if not output_dir.exists():
            return ""

        assigned_targets = self._builder_assigned_html_targets(input_data)
        candidate_names: List[str] = []
        for name in assigned_targets:
            if name not in candidate_names:
                candidate_names.append(name)
        if not candidate_names:
            for path in sorted(output_dir.glob("*.htm*")):
                if path.is_file() and not path.name.startswith("_") and path.name not in candidate_names:
                    candidate_names.append(path.name)

        if not candidate_names:
            return ""

        html_by_name: Dict[str, str] = {}
        for name in candidate_names[:4]:
            path = output_dir / name
            if not path.exists() or not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                continue
            if len(content.strip()) < 80:
                continue
            merged = self._merge_builder_fragment_with_base_html(content, incremental_text)
            candidate = merged
            if not candidate:
                continue
            lower = candidate.lower()
            if "<!doctype" not in lower and "<html" not in lower:
                continue
            existing = html_by_name.get(name, "")
            if len(candidate) > len(existing):
                html_by_name[name] = candidate

        if not html_by_name:
            return ""

        ordered_names = [name for name in candidate_names if name in html_by_name]
        return "\n\n".join(
            f"```html {name}\n{html_by_name[name].strip()}\n```"
            for name in ordered_names
            if str(html_by_name.get(name) or "").strip()
        )

    def _select_builder_salvage_text(
        self,
        latest_stream_text: str,
        output_text: str,
        recovered_html: Optional[str] = None,
        tool_result_text: str = "",
    ) -> str:
        """Prefer the fullest salvage candidate, especially recovered HTML from truncated tool calls."""
        latest = str(latest_stream_text or "").strip()
        output = str(output_text or "").strip()
        recovered = str(recovered_html or "").strip()
        tool_result = str(tool_result_text or "").strip()

        def _looks_like_html(text: str) -> bool:
            lower = text.lower()
            return "<!doctype" in lower or "<html" in lower or "<body" in lower or "```html" in lower

        def _body_signal_score(text: str) -> int:
            lower = text.lower()
            score = 0
            for token in ("<body", "<main", "<canvas", "<section", "<button", "<h1", "hud", "start", "play"):
                if token in lower:
                    score += 1
            return score

        best = ""
        best_rank = (-1, -1, -1, -1)
        for candidate in (latest, output, recovered, tool_result):
            if not candidate:
                continue
            lower = candidate.lower()
            rank = (
                1 if _looks_like_html(candidate) else 0,
                _body_signal_score(candidate),
                1 if "</html>" in lower else 0,
                len(candidate),
            )
            if rank > best_rank:
                best = candidate
                best_rank = rank
        return best

    async def _builder_partial_text_salvage_result(
        self,
        *,
        node: Optional[Dict[str, Any]] = None,
        input_data: str,
        output_text: str,
        reason: str,
        on_progress,
        tool_results: List[Dict[str, Any]],
        model_name: str,
        mode: str,
        usage: Any = None,
        cost: Optional[float] = None,
        tool_call_stats: Optional[Dict[str, int]] = None,
    ) -> Optional[Dict[str, Any]]:
        tool_result_text = self._builder_tool_result_salvage_text(tool_results, input_data)
        enriched_output = self._select_builder_salvage_text(
            "",
            output_text,
            tool_result_text=tool_result_text,
        )
        if not self._builder_partial_text_is_salvageable(input_data, enriched_output):
            return None
        if not self._builder_text_output_has_persistable_html(enriched_output, input_data):
            logger.info(
                "Skipping builder partial text salvage promotion: recovered HTML is still too thin/incomplete",
            )
            return None
        if on_progress:
            await self._emit_noncritical_progress(on_progress, {
                "stage": "builder_timeout_salvage",
                "reason": str(reason or "partial_text_timeout"),
            })
        await self._publish_partial_output(on_progress, enriched_output, phase="finalizing")
        logger.info(
            "Salvaging builder partial text after timeout/fallback failure (%s chars, reason=%s)",
            len(str(enriched_output or "")),
            _sanitize_error(str(reason or ""))[:160],
        )
        await self._auto_save_builder_text_output(
            output_text=enriched_output,
            input_data=input_data,
            node=node,
            tool_results=tool_results,
            tool_call_stats=tool_call_stats,
            on_progress=on_progress,
        )
        payload: Dict[str, Any] = {
            "success": True,
            "output": enriched_output,
            "tool_results": tool_results,
            "model": model_name,
            "mode": mode,
            "tool_call_stats": dict(tool_call_stats or {}),
        }
        if usage is not None:
            payload["usage"] = usage
        if cost is not None:
            payload["cost"] = cost
        return payload

    def _builder_tool_repair_prompt(self, input_data: str, tool_action: str, result: Any) -> Optional[str]:
        if not isinstance(result, dict) or bool(result.get("success")):
            return None
        error = str(result.get("error") or "").strip()
        if not error:
            return None

        output_dir = self._current_output_dir().rstrip("/")
        assigned_targets = self._builder_assigned_html_targets(input_data)
        support_lane = self._builder_is_support_lane(input_data) and not assigned_targets
        assigned_line = (
            "Assigned HTML filenames: " + ", ".join(assigned_targets) + ". "
            if assigned_targets else
            ""
        )
        output_path_line = ""
        if assigned_targets:
            output_path_line = (
                "Exact runtime output paths: "
                + ", ".join(f"{output_dir}/{name}" for name in assigned_targets[:8])
                + ". "
            )

        if support_lane:
            if "html target not assigned" in error.lower():
                return self._builder_support_lane_write_prompt(
                    input_data,
                    opener="Your previous write targeted an HTML file that is not assigned to this lane.",
                    note="Stay on support files only.",
                )
            if (
                "shipped html write rejected" in error.lower()
                or "lacks a usable game slice" in error.lower()
                or "incomplete for a game deliverable" in error.lower()
                or "too short to trust as a final artifact" in error.lower()
                or "body lacks meaningful visible content" in error.lower()
            ):
                return self._builder_support_lane_write_prompt(
                    input_data,
                    opener="Stop attempting a playable root HTML rewrite in this lane.",
                    note="Repair the assigned subsystem through support files instead.",
                )
            if "Path not allowed by security policy" in error or "blank path is not allowed" in error.lower():
                return self._builder_support_lane_write_prompt(
                    input_data,
                    opener="Your previous file_ops call failed because the path was blank or invalid.",
                    note="Retry immediately with explicit absolute support-file paths.",
                )
            if tool_action == "read" and error.startswith("File not found:"):
                return self._builder_support_lane_write_prompt(
                    input_data,
                    opener="The support file you tried to read does not exist yet.",
                    note="Create it now instead of probing missing files.",
                )

        if "html target not assigned" in error.lower():
            return (
                "Your previous file_ops write targeted an HTML file that is not assigned to you. "
                f"{assigned_line}"
                f"{output_path_line}"
                f"Write ONLY those exact HTML filenames under {output_dir}/. "
                "If index.html is not in your assigned list, do not touch it. "
                "Retry immediately with file_ops write for the missing assigned pages only."
            )

        if (
            "shipped html write rejected" in error.lower()
            or "lacks a usable game slice" in error.lower()
            or "incomplete for a game deliverable" in error.lower()
            or "too short to trust as a final artifact" in error.lower()
            or "body lacks meaningful visible content" in error.lower()
        ):
            primary_target = assigned_targets[0] if assigned_targets else "index.html"
            try:
                task_type = task_classifier.classify(input_data).task_type
            except Exception:
                task_type = ""
            delivery_note = (
                "Write a complete playable game slice now: include a visible start/play flow, a gameplay surface "
                "(canvas/scene/viewport), live input binding, and a running requestAnimationFrame loop or equivalent runtime hook. "
                if task_type == "game" else
                "Overwrite the same assigned page with a complete standalone final page, not a thin shell or off-topic placeholder. "
            )
            return (
                "Your previous HTML write was rejected by the runtime guard. "
                f"{assigned_line}"
                f"{output_path_line}"
                f"Overwrite {output_dir}/{primary_target} immediately via file_ops write. "
                f"{delivery_note}"
                "Do not inspect again. Do not return prose before the corrected write."
            )

        if "Path not allowed by security policy" in error or "blank path is not allowed" in error.lower():
            # Dynamically discover existing HTML files on disk to give the model concrete targets
            disk_hint = ""
            try:
                from pathlib import Path as _P
                output_path = _P(output_dir)
                if output_path.exists():
                    existing = sorted(
                        f.name for f in output_path.glob("*.htm*")
                        if f.is_file() and not f.name.startswith("_")
                    )
                    if existing:
                        disk_hint = (
                            "Existing HTML files on disk: "
                            + ", ".join(f"{output_dir}/{name}" for name in existing[:10])
                            + ". "
                        )
            except Exception:
                pass
            return (
                "Your previous file_ops call failed because the path was blank or invalid. "
                "STOP reading or listing. You MUST write HTML files NOW. "
                f"Retry IMMEDIATELY with file_ops write using explicit absolute paths under {output_dir}/. "
                f"{assigned_line}"
                f"{output_path_line}"
                f"{disk_hint}"
                f"Example: {{\"action\": \"write\", \"path\": \"{output_dir}/index.html\", \"content\": \"<!DOCTYPE html>...\"}}. "
                "Do not output prose or explanations. Save the real HTML files now."
            )

        if tool_action == "read" and error.startswith("File not found:"):
            create_target = assigned_targets[0] if assigned_targets else "index.html"
            return (
                "The file you tried to read does not exist yet. "
                f"Create it now with file_ops write at {output_dir}/{create_target}. "
                f"{assigned_line}"
                f"{output_path_line}"
                "For multi-page work, also create the remaining assigned HTML pages in the same directory. "
                "Do not keep probing missing files."
            )

        return None

    def _builder_non_write_followup_prompt(
        self,
        input_data: str,
        tool_action: str,
        result: Any,
        non_write_streak: int,
    ) -> Optional[str]:
        if tool_action not in {"list", "read"}:
            return None
        if not isinstance(result, dict) or not bool(result.get("success")):
            return None
        if int(non_write_streak or 0) <= 0:
            return None

        output_dir = self._current_output_dir().rstrip("/")
        assigned_targets = self._builder_assigned_html_targets(input_data)
        merger_like = bool(re.search(r"\b(?:final merger|merger|integrator|integration|assemble|assembly|merge)\b", str(input_data or "").lower()))
        support_lane = self._builder_is_support_lane(input_data) and not assigned_targets and not merger_like
        assigned_line = (
            "Assigned HTML filenames: " + ", ".join(assigned_targets) + ". "
            if assigned_targets else
            ""
        )
        output_path_line = ""
        if assigned_targets:
            output_path_line = (
                "Exact runtime output paths: "
                + ", ".join(f"{output_dir}/{name}" for name in assigned_targets[:8])
                + ". "
            )
        if task_classifier.wants_multi_page(input_data):
            targets_line = ""
            if assigned_targets:
                targets_line = (
                    "Your next response must emit a batch of file_ops write calls that covers EVERY assigned HTML filename, "
                    "not just a single page. A one-page retry is still a failure. "
                )
            return (
                "You have already inspected the workspace. Stop listing or reading files now. "
                f"{assigned_line}"
                f"{output_path_line}"
                f"{targets_line}"
                f"Your VERY NEXT response must be one or more file_ops write calls that overwrite the final HTML pages directly under {output_dir}/. "
                "Do not browse. Do not probe more directories. Do not return prose. "
                "Overwrite the bootstrap draft files with the final routed pages now."
            )
        if support_lane:
            return self._builder_support_lane_write_prompt(
                input_data,
                opener="You have already inspected the workspace. Stop listing or reading now.",
            )
        if merger_like:
            primary_target = assigned_targets[0] if assigned_targets else "index.html"
            return (
                "You already inspected enough merger context. Stop reading now. "
                f"{assigned_line}"
                f"{output_path_line}"
                "Read the live root artifact plus each non-empty local JS/CSS/JSON support file at most once, then merge immediately. "
                f"Your VERY NEXT response must be a file_ops write that patches {output_dir}/{primary_target} and wires the retained subsystems into the shipped root. "
                "If a support subsystem is worth keeping, either wire it with browser-native <script>/<link> tags or inline the needed code into the root artifact now. "
                "Do not leave meaningful support files beside the root artifact unwired, and do not keep Node/CommonJS footers like module.exports or exports.* in shipped browser files. "
                "Do not list more directories. Do not re-read files you already inspected. Do not return prose before the merge write is saved."
            )
        primary_target = assigned_targets[0] if assigned_targets else "index.html"
        return (
            "You have already inspected the workspace. Stop reading and write the deliverable now. "
            f"{assigned_line}"
            f"{output_path_line}"
            f"Your VERY NEXT response must be a file_ops write to {output_dir}/{primary_target}. "
            "Do not return prose before the file is saved."
        )

    def _polisher_non_write_followup_prompt(
        self,
        tool_name: str,
        tool_action: str,
        result: Any,
        non_write_streak: int,
    ) -> Optional[str]:
        tool_name = str(tool_name or "").strip().lower()
        tool_action = str(tool_action or "").strip().lower()
        streak = int(non_write_streak or 0)
        if streak < 1:
            return None
        inspection_note = ""
        if tool_name == "browser":
            inspection_note = "You already have enough visual evidence from the browser. "
        elif tool_name == "file_ops" and tool_action in {"list", "read"}:
            if not isinstance(result, dict) or not bool(result.get("success")):
                return None
            inspection_note = "You have already inspected the artifact on disk. "
        elif tool_name == "file_ops" and tool_action and tool_action != "write" and streak >= 2:
            inspection_note = "Stop spending more tool turns on inspection. "
        else:
            return None

        output_dir = self._current_output_dir().rstrip("/")
        current_pages = self._current_output_html_pages()
        page_hint = (
            "Current HTML pages on disk: " + ", ".join(current_pages[:10]) + ". "
            if current_pages else
            ""
        )
        return (
            f"{inspection_note}Stop inspecting and start polishing now. "
            f"Your VERY NEXT response must contain one or more file_ops write calls to files under {output_dir}/. "
            "Start with shared files such as styles.css or app.js when possible, then patch only the specifically affected routed pages. "
            f"{page_hint}"
            "Patch the current files in place instead of re-reading the whole site. "
            "Replace blank media placeholders, gradient-only image stand-ins, empty map/location blocks, [Collection Image]-style placeholder copy, and thin motion with finished premium visuals and interactions now without rewriting strong sections. "
            "Do not browse again. Do not return prose before writing files."
        )

    def _normalize_review_preview_path(self, raw: str) -> str:
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

    def _current_output_html_pages(self) -> List[str]:
        output_dir = Path(self._current_output_dir())
        if not output_dir.exists():
            return []
        try:
            output_root = output_dir.resolve()
        except Exception:
            output_root = output_dir
        pages: List[str] = []
        seen: set[str] = set()
        for path in sorted(output_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".html", ".htm"}:
                continue
            try:
                rel_path = path.resolve().relative_to(output_root)
            except Exception:
                rel_path = Path(path.name)
            if rel_path.parts and rel_path.parts[0] == "_stable_previews":
                continue
            normalized = self._normalize_review_preview_path(str(rel_path))
            name = Path(normalized).name
            if (
                not normalized
                or normalized in seen
                or name.startswith("_")
                or re.fullmatch(r"index_part\d+\.html?", name, re.IGNORECASE)
            ):
                continue
            seen.add(normalized)
            pages.append(normalized)
        pages.sort(key=lambda item: (item != "index.html", item))
        return pages

    def _classify_task_type(self, prompt_source: str) -> str:
        source = str(prompt_source or "").strip()
        if not source:
            return "website"
        try:
            return task_classifier.classify(source).task_type
        except Exception:
            return "website"

    def _tool_names_from_defs(self, tools: Any) -> set[str]:
        names: set[str] = set()
        for tool in tools or []:
            name = ""
            if isinstance(tool, dict):
                if str(tool.get("type") or "").strip().lower() == "function":
                    fn = tool.get("function")
                    if isinstance(fn, dict):
                        name = str(fn.get("name") or "").strip().lower()
                else:
                    name = str(tool.get("name") or "").strip().lower()
            else:
                fn = getattr(tool, "function", None)
                if fn is not None:
                    name = str(getattr(fn, "name", "") or "").strip().lower()
                if not name:
                    name = str(getattr(tool, "name", "") or "").strip().lower()
            if name:
                names.add(name)
        return names

    def _tool_results_reference_urls(self, tool_results: Any) -> List[str]:
        urls: List[str] = []
        for result in tool_results or []:
            if not isinstance(result, dict):
                continue
            data = result.get("data")
            if not isinstance(data, dict):
                continue
            candidates: List[str] = []
            for key in ("url", "final_url"):
                value = str(data.get(key) or "").strip()
                if value:
                    candidates.append(value)
            for key in ("urls", "visited_urls"):
                raw = data.get(key)
                if isinstance(raw, list):
                    candidates.extend(str(item).strip() for item in raw if str(item).strip())
            for candidate in candidates:
                if self._is_local_preview_or_nonweb_url(candidate):
                    continue
                if candidate and candidate not in urls:
                    urls.append(candidate)
        return urls

    def _node_execution_strategy_prompt_block(self, node: Optional[Dict[str, Any]]) -> str:
        block = str((node or {}).get("model_execution_strategy_block") or "").strip()
        if not block:
            return ""
        return f"\n\nMODEL-CAPABILITY EXECUTION STRATEGY:\n{block}"

    def _is_local_preview_or_nonweb_url(self, candidate: str) -> bool:
        text = str(candidate or "").strip()
        if not text:
            return True
        parsed = urlparse(text)
        scheme = str(parsed.scheme or "").strip().lower()
        host = str(parsed.hostname or "").strip().lower()
        path = str(parsed.path or "").strip().lower()
        if scheme and scheme not in {"http", "https"}:
            return True
        if host in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
            return True
        return path.startswith("/preview") or "/preview/" in path

    def _analyst_browser_followup_reason(
        self,
        node_type: str,
        tool_call_stats: Dict[str, int],
        tool_results: List[Dict[str, Any]],
        available_tool_names: Optional[set[str]] = None,
    ) -> Optional[str]:
        if normalize_node_role(node_type) != "analyst":
            return None
        available_names = {
            str(item or "").strip()
            for item in (available_tool_names or set())
            if str(item or "").strip()
        }
        research_tools = {"browser", "source_fetch"} & available_names
        if not research_tools:
            return None
        research_calls = sum(int(tool_call_stats.get(tool_name, 0) or 0) for tool_name in research_tools)
        visited_urls = self._tool_results_reference_urls(tool_results)
        if research_calls < 1 or not visited_urls:
            return "You must use source_fetch or browser on at least 2 different source URLs before final report."
        if len(visited_urls) < 2:
            return (
                "You have only visited 1 source URL. "
                "Use source_fetch or browser on one more distinct GitHub/doc/tutorial/source page before final report."
            )
        return None

    def _analyst_browser_followup_message(self, reason: str) -> str:
        reason_text = str(reason or "").strip()
        lower = reason_text.lower()
        action_hint = (
            'Call source_fetch now on an implementation-grade source such as a GitHub repo README/blob file, docs page, '
            'tutorial, or postmortem; use browser only if the page needs interaction. Visit 2 distinct URLs total before finalizing. '
            'Do NOT open localhost or /preview/ pages for analyst research.'
        )
        if "1 source url" in lower or "one more distinct" in lower:
            action_hint = (
                'Call source_fetch or browser on one more distinct URL now, preferably a GitHub repo, official docs page, '
                'or technical tutorial that materially informs implementation. '
                'Do NOT open localhost or /preview/ pages.'
            )
        return (
            "Your research pass is incomplete.\n"
            f"Missing requirement: {reason_text}\n"
            f"{action_hint}\n"
            "Do not output the final analyst report yet. Browse first, then finalize with the visited URLs listed."
        )

    def _asset_plan_output_complete(self, node_type: str, output_text: str) -> bool:
        normalized = normalize_node_role(node_type)
        text = str(output_text or "").strip()
        if normalized not in {"spritesheet", "assetimport"} or len(text) < 80:
            return False

        candidate_chunks: List[str] = []
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            candidate_chunks.append(text[json_start:json_end])
        candidate_chunks.append(text)

        parsed_obj: Optional[Dict[str, Any]] = None
        for candidate in candidate_chunks:
            try:
                parsed = json.loads(candidate)
            except Exception:
                parsed = None
                if _json_repair_fn:
                    try:
                        repaired = _json_repair_fn(candidate)
                        if isinstance(repaired, str):
                            repaired = json.loads(repaired)
                        parsed = repaired
                    except Exception:
                        parsed = None
            if isinstance(parsed, dict):
                parsed_obj = parsed
                break

        if not isinstance(parsed_obj, dict):
            return False

        non_empty_keys = {
            str(key).strip()
            for key, value in parsed_obj.items()
            if str(key).strip() and value not in (None, "", [], {})
        }
        if len(non_empty_keys) < 4:
            return False

        if normalized == "spritesheet":
            has_targets = bool({"asset_families", "model_targets"} & non_empty_keys)
            has_rules = bool({"builder_replacement_rules", "style_lock_tokens"} & non_empty_keys)
            has_material = bool({"material_constraints", "material_rules"} & non_empty_keys)
            return has_targets and has_rules and has_material

        has_mapping = bool({"runtime_mapping", "manifest_fields"} & non_empty_keys)
        has_replacements = bool({"replacement_keys", "builder_integration_notes"} & non_empty_keys)
        has_fallbacks = bool({"placeholder_fallbacks", "folder_structure", "naming_rules"} & non_empty_keys)
        return has_mapping and has_replacements and has_fallbacks

    def _browser_use_enabled_for_qa(self) -> bool:
        raw = self.config.get("qa_enable_browser_use", os.getenv("EVERMIND_QA_ENABLE_BROWSER_USE", "0"))
        return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}

    def _browser_use_force_for_games(self) -> bool:
        raw = self.config.get(
            "qa_force_browser_use_for_games",
            os.getenv("EVERMIND_QA_FORCE_BROWSER_USE_FOR_GAMES", "0"),
        )
        return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}

    def _qa_browser_use_available(self, node_type: str, available_tool_names: set[str]) -> bool:
        normalized = normalize_node_role(node_type)
        return (
            normalized in {"reviewer", "tester"}
            and is_qa_browser_use_enabled(config=self.config)
            and "browser_use" in (available_tool_names or set())
        )

    def _qa_browser_use_required(
        self,
        node_type: str,
        task_type: str,
        available_tool_names: set[str],
    ) -> bool:
        return (
            self._qa_browser_use_available(node_type, available_tool_names)
            and task_type == "game"
            and self._browser_use_force_for_games()
        )

    def _qa_browser_prefetch_allowed(
        self,
        node_type: str,
        available_tool_names: set[str],
    ) -> bool:
        normalized = normalize_node_role(node_type)
        return normalized in {"reviewer", "tester"} and "browser" in (available_tool_names or set())

    def _browser_action_event_from_result(
        self,
        result: Dict[str, Any],
        *,
        action_name: str,
    ) -> Dict[str, Any]:
        browser_data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
        if not isinstance(browser_data, dict):
            browser_data = {}
        return {
            "action": action_name or browser_data.get("action") or "unknown",
            "subaction": browser_data.get("subaction"),
            "intent": browser_data.get("intent"),
            "ok": bool(result.get("success", False)) if isinstance(result, dict) else False,
            "url": browser_data.get("url"),
            "target": browser_data.get("target"),
            "observation": browser_data.get("observation"),
            "snapshot_refs_preview": browser_data.get("snapshot_refs_preview", []),
            "snapshot_ref_count": browser_data.get("snapshot_ref_count", 0),
            "state_hash": browser_data.get("state_hash"),
            "previous_state_hash": browser_data.get("previous_state_hash"),
            "state_changed": bool(browser_data.get("state_changed", False)),
            "scroll_y": browser_data.get("scroll_y", 0),
            "viewport_height": browser_data.get("viewport_height", 0),
            "page_height": browser_data.get("page_height", 0),
            "at_page_top": bool(browser_data.get("at_page_top", False)),
            "at_page_bottom": bool(browser_data.get("at_page_bottom", False)),
            "keys_count": browser_data.get("keys_count", 0),
            "console_error_count": browser_data.get("console_error_count", 0),
            "page_error_count": browser_data.get("page_error_count", 0),
            "failed_request_count": browser_data.get("failed_request_count", 0),
            "recent_console_errors": browser_data.get("recent_console_errors", []),
            "recent_page_errors": browser_data.get("recent_page_errors", []),
            "browser_mode": browser_data.get("browser_mode"),
            "requested_mode": browser_data.get("requested_mode"),
            "launch_note": browser_data.get("launch_note"),
            "recording_path": browser_data.get("recording_path"),
            "capture_path": browser_data.get("capture_path"),
            "error": (result.get("error") if isinstance(result, dict) else "") or "",
        }

    def _tool_execution_progress_payload(self, fn_name: str, parsed_args: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"stage": "executing_plugin", "plugin": fn_name}
        args = parsed_args if isinstance(parsed_args, dict) else {}
        if fn_name == "file_ops":
            action = str(args.get("action", "")).strip().lower()
            if action:
                payload["tool_action"] = action
            path = (
                args.get("path")
                or args.get("target")
                or args.get("dest")
                or args.get("output_path")
            )
            if str(path or "").strip():
                payload["path"] = str(path).strip()
            else:
                raw_paths = args.get("paths")
                if isinstance(raw_paths, list):
                    payload["paths"] = [str(item).strip() for item in raw_paths[:6] if str(item or "").strip()]
        elif fn_name == "source_fetch":
            mode = str(args.get("mode", "")).strip().lower()
            if mode:
                payload["tool_action"] = mode
            query = str(args.get("query", "")).strip()
            if query:
                payload["query"] = query[:200]
            url = str(args.get("url", "")).strip()
            if url:
                payload["url"] = url
            raw_urls = args.get("urls")
            if isinstance(raw_urls, list):
                payload["urls"] = [str(item).strip() for item in raw_urls[:6] if str(item or "").strip()]
        else:
            action = str(args.get("action", "")).strip().lower()
            if action:
                payload["tool_action"] = action
            url = str(args.get("url", "")).strip()
            if url:
                payload["url"] = url
            target = str(args.get("target", "")).strip()
            if target:
                payload["target"] = target[:200]
        return payload

    def _normalize_browser_use_action_name(self, raw: Any) -> str:
        lowered = str(raw or "").strip().lower().replace("-", "_")
        if not lowered:
            return ""
        mapping = {
            "click_element": "click",
            "click": "click",
            "input_text": "fill",
            "fill": "fill",
            "send_keys": "press_sequence",
            "press_keys": "press_sequence",
            "keyboard": "press_sequence",
            "scroll": "scroll",
            "navigate": "navigate",
            "open_tab": "navigate",
            "go_to_url": "navigate",
            "extract_content": "observe",
            "wait": "wait_for",
            "done": "done",
        }
        return mapping.get(lowered, lowered)

    def _browser_use_state_hash(self, url: str, capture_path: str, step: int) -> str:
        payload = f"{url}|{capture_path}|{step}"
        return hashlib.md5(payload.encode("utf-8", errors="ignore")).hexdigest()

    def _browser_use_action_events(self, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(result, dict):
            return []
        browser_data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
        if not isinstance(browser_data, dict):
            browser_data = {}
        history_items = browser_data.get("history_items")
        if not isinstance(history_items, list):
            history_items = []
        screenshot_paths = [
            str(item).strip()
            for item in (browser_data.get("screenshot_paths") or [])
            if str(item).strip()
        ]
        urls = [
            str(item).strip()
            for item in (browser_data.get("urls") or [])
            if str(item).strip()
        ]
        base_url = str(browser_data.get("final_url") or browser_data.get("url") or "").strip()
        capture_path = str(browser_data.get("capture_path") or "").strip()
        recording_path = str(browser_data.get("recording_path") or "").strip()
        browser_mode = browser_data.get("browser_mode")
        requested_mode = browser_data.get("requested_mode")
        action_names = [
            str(item).strip()
            for item in (browser_data.get("action_names") or [])
            if str(item).strip()
        ]

        events: List[Dict[str, Any]] = []
        first_snapshot_path = (
            str((history_items[0] or {}).get("screenshot_path") or "").strip()
            if history_items and isinstance(history_items[0], dict)
            else ""
        ) or (screenshot_paths[0] if screenshot_paths else capture_path)
        first_url = (
            str((history_items[0] or {}).get("url") or "").strip()
            if history_items and isinstance(history_items[0], dict)
            else ""
        ) or (urls[0] if urls else base_url)
        previous_state_hash = ""
        if first_snapshot_path or first_url:
            previous_state_hash = self._browser_use_state_hash(first_url, first_snapshot_path, 0)
            events.append({
                "action": "snapshot",
                "subaction": "",
                "intent": "observe",
                "ok": True,
                "url": first_url,
                "target": "",
                "observation": "",
                "snapshot_refs_preview": [],
                "snapshot_ref_count": 0,
                "state_hash": previous_state_hash,
                "previous_state_hash": "",
                "state_changed": False,
                "scroll_y": 0,
                "viewport_height": 0,
                "page_height": 0,
                "at_page_top": False,
                "at_page_bottom": False,
                "keys_count": 0,
                "console_error_count": 0,
                "page_error_count": 0,
                "failed_request_count": 0,
                "recent_console_errors": [],
                "recent_page_errors": [],
                "browser_mode": browser_mode,
                "requested_mode": requested_mode,
                "recording_path": recording_path,
                "capture_path": first_snapshot_path,
                "error": "",
                "plugin": "browser_use",
            })

        if not history_items and action_names:
            history_items = [
                {
                    "step": idx + 1,
                    "action_names": [name],
                    "url": urls[idx] if idx < len(urls) else (urls[-1] if urls else base_url),
                    "screenshot_path": screenshot_paths[idx] if idx < len(screenshot_paths) else (screenshot_paths[-1] if screenshot_paths else capture_path),
                    "interacted_element": None,
                    "errors": browser_data.get("errors") or [],
                }
                for idx, name in enumerate(action_names)
            ]

        for idx, item in enumerate(history_items):
            if not isinstance(item, dict):
                continue
            raw_actions = item.get("action_names") if isinstance(item.get("action_names"), list) else []
            normalized_actions = [
                self._normalize_browser_use_action_name(name)
                for name in raw_actions
                if self._normalize_browser_use_action_name(name)
            ]
            if not normalized_actions:
                continue
            item_url = str(item.get("url") or "").strip() or (urls[idx] if idx < len(urls) else "") or base_url
            item_capture = str(item.get("screenshot_path") or "").strip() or (screenshot_paths[idx] if idx < len(screenshot_paths) else "") or capture_path
            state_hash = self._browser_use_state_hash(item_url, item_capture, idx + 1)
            target = item.get("interacted_element")
            if isinstance(target, dict):
                target = json.dumps(target, ensure_ascii=False)[:240]
            elif target is None:
                target = ""
            else:
                target = str(target)[:240]
            item_errors = [
                str(entry).strip()[:240]
                for entry in (item.get("errors") or [])
                if str(entry).strip()
            ]
            state_changed = bool(previous_state_hash and state_hash != previous_state_hash)
            for action_name in normalized_actions:
                if action_name == "done":
                    continue
                effective_state_changed = state_changed or action_name in {
                    "click", "fill", "press", "press_sequence", "scroll", "navigate",
                }
                events.append({
                    "action": action_name,
                    "subaction": "",
                    "intent": action_name,
                    "ok": not bool(item_errors),
                    "url": item_url,
                    "target": target,
                    "observation": "",
                    "snapshot_refs_preview": [],
                    "snapshot_ref_count": 0,
                    "state_hash": state_hash,
                    "previous_state_hash": previous_state_hash,
                    "state_changed": effective_state_changed,
                    "scroll_y": 0,
                    "viewport_height": 0,
                    "page_height": 0,
                    "at_page_top": False,
                    "at_page_bottom": False,
                    "keys_count": 4 if action_name == "press_sequence" else (1 if action_name == "press" else 0),
                    "console_error_count": 0,
                    "page_error_count": 0,
                    "failed_request_count": 0,
                    "recent_console_errors": [],
                    "recent_page_errors": [],
                    "browser_mode": browser_mode,
                    "requested_mode": requested_mode,
                    "recording_path": recording_path,
                    "capture_path": item_capture,
                    "error": "; ".join(item_errors[:2]),
                    "plugin": "browser_use",
                })
            previous_state_hash = state_hash or previous_state_hash

        if not events and (capture_path or base_url):
            events.append({
                "action": "snapshot",
                "subaction": "",
                "intent": "observe",
                "ok": bool(result.get("success", False)),
                "url": base_url,
                "target": "",
                "observation": "",
                "snapshot_refs_preview": [],
                "snapshot_ref_count": 0,
                "state_hash": self._browser_use_state_hash(base_url, capture_path, 0),
                "previous_state_hash": "",
                "state_changed": False,
                "scroll_y": 0,
                "viewport_height": 0,
                "page_height": 0,
                "at_page_top": False,
                "at_page_bottom": False,
                "keys_count": 0,
                "console_error_count": 0,
                "page_error_count": 0,
                "failed_request_count": 0,
                "recent_console_errors": [],
                "recent_page_errors": [],
                "browser_mode": browser_mode,
                "requested_mode": requested_mode,
                "recording_path": recording_path,
                "capture_path": capture_path,
                "error": str(result.get("error") or "").strip(),
                "plugin": "browser_use",
            })
        return events

    def _qa_browser_use_prefetch_task(self, node_type: str, task_type: str, goal: str) -> str:
        normalized = normalize_node_role(node_type)
        if task_type == "game":
            qa_role = "reviewer" if normalized == "reviewer" else "tester"
            return (
                f"You are running an automated {qa_role} gameplay preflight for Evermind. "
                "Open the local preview first, inspect the visible title screen, HUD, and start controls, "
                "click the visible Start / Play / Begin control, then perform a short real gameplay session. "
                "Use Arrow keys or WASD plus Space or Enter when relevant. "
                "Verify that the visible state changes after input by checking the HUD, score, camera, scene, "
                "player position, enemy state, or restart/death state. Keep screenshots and recording evidence. "
                f"Goal context: {str(goal or '')[:220]}"
            )
        return (
            "Open the local preview, interact with the primary controls, and keep screenshot/recording evidence "
            "for downstream QA."
        )

    def _qa_browser_use_prefetch_summary(
        self,
        result: Dict[str, Any],
        browser_use_events: List[Dict[str, Any]],
        task_type: str,
    ) -> str:
        success = bool((result or {}).get("success"))
        data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
        preview_url = self._qa_default_preview_url("tester") or "http://127.0.0.1:8765/preview/"
        action_names: List[str] = []
        for event in browser_use_events or []:
            action = str(event.get("action") or "").strip()
            if action and action not in action_names:
                action_names.append(action)
        if not action_names:
            action_names = [
                str(item).strip()
                for item in (data.get("action_names") or [])
                if str(item).strip()
            ][:8]
        final_url = str(data.get("final_url") or data.get("url") or "").strip() or preview_url
        recording_path = str(data.get("recording_path") or "").strip()
        capture_path = str(data.get("capture_path") or "").strip()
        if success:
            lines = [
                "System note: a deterministic browser_use QA preflight already ran before your verdict.",
                f"- task_type: {task_type}",
                f"- final_url: {final_url}",
            ]
            if action_names:
                lines.append(f"- captured_actions: {', '.join(action_names[:8])}")
            if recording_path:
                lines.append(f"- recording_path: {recording_path}")
            if capture_path:
                lines.append(f"- capture_path: {capture_path}")
            lines.append(
                "Treat this as real interaction evidence. Use it in your scoring, and only add extra browser checks where the evidence is still incomplete."
            )
            return "\n".join(lines)

        error_text = str((result or {}).get("error") or "").strip()[:240]
        return (
            "System note: the deterministic browser_use QA preflight was attempted but did not complete cleanly.\n"
            f"- task_type: {task_type}\n"
            f"- error: {error_text or 'unknown browser_use preflight failure'}\n"
            "Continue with browser-based QA, but do not assume gameplay quality without visible evidence."
        )

    def _qa_browser_prefetch_summary(
        self,
        results: List[Dict[str, Any]],
        browser_events: List[Dict[str, Any]],
        task_type: str,
    ) -> str:
        successful_events = [item for item in (browser_events or []) if bool(item.get("ok"))]
        preview_url = self._qa_default_preview_url("tester") or "http://127.0.0.1:8765/preview/"
        final_url = ""
        capture_path = ""
        for event in successful_events:
            final_url = final_url or str(event.get("url") or "").strip()
            capture_path = capture_path or str(event.get("capture_path") or "").strip()
        if successful_events:
            action_names: List[str] = []
            for event in successful_events:
                action = str(event.get("action") or "").strip()
                if action and action not in action_names:
                    action_names.append(action)
            lines = [
                "System note: a deterministic browser QA preflight already ran before your verdict.",
                f"- task_type: {task_type}",
                f"- final_url: {final_url or preview_url}",
            ]
            if action_names:
                lines.append(f"- captured_actions: {', '.join(action_names[:8])}")
            if capture_path:
                lines.append(f"- capture_path: {capture_path}")
            lines.append(
                "Treat this as real visual evidence. Use it in your scoring, and only add extra browser steps where interaction proof is still incomplete."
            )
            return "\n".join(lines)

        first_error = ""
        for result in results or []:
            first_error = str((result or {}).get("error") or "").strip()
            if first_error:
                break
        return (
            "System note: the deterministic browser QA preflight was attempted but did not complete cleanly.\n"
            f"- task_type: {task_type}\n"
            f"- error: {(first_error or 'unknown browser preflight failure')[:240]}\n"
            "Continue with manual browser-based QA if the tool is still available."
        )

    async def _maybe_seed_qa_browser(
        self,
        *,
        node: Dict[str, Any],
        node_type: str,
        task_type: str,
        input_data: str,
        plugins: Optional[List[Any]],
        available_tool_names: set[str],
        tool_results: List[Dict[str, Any]],
        tool_call_stats: Dict[str, int],
        browser_action_events: List[Dict[str, Any]],
        messages: Optional[List[Dict[str, Any]]] = None,
        on_progress=None,
    ) -> Optional[List[Dict[str, Any]]]:
        if not self._qa_browser_prefetch_allowed(node_type, available_tool_names):
            normalized_role = normalize_node_role(node_type) or node_type
            reason = "node_type_not_qa_role"
            if normalized_role in {"reviewer", "tester"} and "browser" not in (available_tool_names or set()):
                reason = "browser_tool_unavailable"
            logger.info(
                "qa_browser_prefetch skipped: node=%s reason=%s tools=%s",
                normalized_role,
                reason,
                sorted(available_tool_names or set()),
            )
            return None
        if (
            task_type == "game"
            and self._qa_browser_use_required(node_type, task_type, available_tool_names)
            and "browser_use" in available_tool_names
        ):
            logger.info(
                "qa_browser_prefetch skipped: node=%s reason=browser_use_forced_for_game",
                normalize_node_role(node_type) or node_type,
            )
            return None
        if int(tool_call_stats.get("browser", 0) or 0) >= 1:
            logger.info(
                "qa_browser_prefetch skipped: node=%s reason=browser_already_used count=%s",
                normalize_node_role(node_type) or node_type,
                int(tool_call_stats.get("browser", 0) or 0),
            )
            return None
        if not plugins or "browser" not in available_tool_names:
            logger.info(
                "qa_browser_prefetch skipped: node=%s reason=browser_plugin_missing",
                normalize_node_role(node_type) or node_type,
            )
            return None
        if self._desktop_qa_browser_suppressed(node_type, input_data):
            logger.info(
                "qa_browser_prefetch skipped: node=%s reason=desktop_qa_evidence_present",
                normalize_node_role(node_type) or node_type,
            )
            return None

        preflight_results: List[Dict[str, Any]] = []
        preflight_events: List[Dict[str, Any]] = []
        logger.info(
            "deterministic browser QA preflight: node=%s task_type=%s tools=%s",
            normalize_node_role(node_type) or node_type,
            task_type,
            sorted(available_tool_names or set()),
        )
        preview_url = self._qa_default_preview_url(node_type) or "http://127.0.0.1:8765/preview/"
        preflight_steps = [
            (
                "observe",
                {
                    "action": "observe",
                    "url": preview_url,
                    "goal": (
                        "Inspect the loaded preview, visible title, primary CTA, key content blocks, and obvious interaction affordances."
                        if task_type != "game" else
                        "Inspect the loaded game preview, title screen, HUD, and visible start/play controls."
                    ),
                    "screenshot": True,
                    "limit": 36,
                },
            ),
            (
                "record_scroll" if task_type != "game" else "snapshot",
                {
                    "action": "record_scroll" if task_type != "game" else "snapshot",
                    "amount": 420,
                    "max_steps": 4,
                    "full_page": False,
                    "screenshot": True,
                } if task_type != "game" else {
                    "action": "snapshot",
                    "screenshot": True,
                    "limit": 36,
                },
            ),
        ]
        if on_progress:
            await self._emit_noncritical_progress(on_progress, {
                "stage": "qa_browser_prefetch",
                "node_type": normalize_node_role(node_type) or node_type,
                "task_type": task_type,
                "url": preview_url,
            })

        for action_name, params in preflight_steps:
            try:
                result = await asyncio.wait_for(
                    self._run_plugin(
                        "browser",
                        params,
                        plugins,
                        node_type=node_type,
                        node=node,
                    ),
                    timeout=25,
                )
            except asyncio.TimeoutError:
                result = {
                    "success": False,
                    "error": f"browser QA preflight timed out during {action_name}",
                    "data": {},
                }
            preflight_results.append(result)
            tool_results.append(result)
            tool_call_stats["browser"] = tool_call_stats.get("browser", 0) + 1
            event = self._browser_action_event_from_result(result, action_name=action_name)
            preflight_events.append(event)
            browser_action_events.append(event)
            logger.info(
                "qa_browser_prefetch step: node=%s action=%s success=%s url=%s capture=%s error=%s",
                normalize_node_role(node_type) or node_type,
                action_name,
                bool(result.get("success")),
                str(event.get("url") or "").strip(),
                str(event.get("capture_path") or "").strip(),
                str(result.get("error") or "").strip()[:160],
            )
            if on_progress:
                await self._emit_noncritical_progress(on_progress, {
                    "stage": "browser_action",
                    "plugin": "browser",
                    **event,
                })
            if not result.get("success"):
                break

        if messages is not None:
            messages.append({
                "role": "user",
                "content": self._qa_browser_prefetch_summary(preflight_results, preflight_events, task_type),
            })
        return preflight_results

    async def _maybe_seed_qa_browser_use(
        self,
        *,
        node: Dict[str, Any],
        node_type: str,
        task_type: str,
        input_data: str,
        plugins: Optional[List[Any]],
        available_tool_names: set[str],
        tool_results: List[Dict[str, Any]],
        tool_call_stats: Dict[str, int],
        browser_action_events: List[Dict[str, Any]],
        messages: Optional[List[Dict[str, Any]]] = None,
        on_progress=None,
    ) -> Optional[Dict[str, Any]]:
        if not self._qa_browser_use_required(node_type, task_type, available_tool_names):
            logger.info(
                "qa_browser_use_prefetch skipped: node=%s reason=not_required task_type=%s tools=%s",
                normalize_node_role(node_type) or node_type,
                task_type,
                sorted(available_tool_names or set()),
            )
            return None
        if int(tool_call_stats.get("browser_use", 0) or 0) >= 1:
            logger.info(
                "qa_browser_use_prefetch skipped: node=%s reason=browser_use_already_used count=%s",
                normalize_node_role(node_type) or node_type,
                int(tool_call_stats.get("browser_use", 0) or 0),
            )
            return None
        if not plugins or "browser_use" not in available_tool_names:
            logger.info(
                "qa_browser_use_prefetch skipped: node=%s reason=browser_use_plugin_missing",
                normalize_node_role(node_type) or node_type,
            )
            return None

        preview_url = self._qa_default_preview_url(node_type) or "http://127.0.0.1:8765/preview/"
        params = {
            "url": preview_url,
            "task": self._qa_browser_use_prefetch_task(node_type, task_type, input_data),
            "max_steps": 10 if task_type == "game" else 8,
            "timeout_sec": 150 if task_type == "game" else 120,
            "use_vision": True,
            "model": str(
                self.config.get("browser_use_model")
                or self.config.get("default_model")
                or "gpt-5.4"
            ).strip() or "gpt-5.4",
        }
        if on_progress:
            await self._emit_noncritical_progress(on_progress, {
                "stage": "qa_browser_use_prefetch",
                "node_type": normalize_node_role(node_type) or node_type,
                "task_type": task_type,
                "url": params["url"],
            })

        # P0 FIX (Opus): Wrap prefetch with independent timeout to avoid consuming
        # the reviewer/tester node's full timeout budget if browser_use hangs.
        _PREFETCH_TIMEOUT = 60
        try:
            result = await asyncio.wait_for(
                self._run_plugin(
                    "browser_use",
                    params,
                    plugins,
                    node_type=node_type,
                    node=node,
                ),
                timeout=_PREFETCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "browser_use QA prefetch timed out after %ss — continuing without preflight evidence",
                _PREFETCH_TIMEOUT,
            )
            result = {
                "success": False,
                "error": f"browser_use QA prefetch timed out after {_PREFETCH_TIMEOUT}s",
                "data": {},
            }
        tool_results.append(result)
        tool_call_stats["browser_use"] = tool_call_stats.get("browser_use", 0) + 1

        preflight_events = self._browser_use_action_events(result)
        for browser_event in preflight_events:
            browser_action_events.append(browser_event)
            if on_progress:
                await self._emit_noncritical_progress(on_progress, {
                    "stage": "browser_action",
                    "plugin": "browser_use",
                    **browser_event,
                })

        if messages is not None:
            messages.append({
                "role": "user",
                "content": self._qa_browser_use_prefetch_summary(result, preflight_events, task_type),
            })
        return result

    def _review_browser_followup_reason(
        self,
        node_type: str,
        task_type: str,
        browser_actions: List[Dict[str, Any]],
        goal: str = "",
        tool_call_stats: Optional[Dict[str, int]] = None,
        available_tool_names: Optional[set[str]] = None,
    ) -> Optional[str]:
        if node_type not in {"reviewer", "tester"}:
            return None

        tool_stats = tool_call_stats or {}
        available_names = available_tool_names or set()
        if self._qa_browser_use_required(node_type, task_type, available_names):
            if int(tool_stats.get("browser_use", 0) or 0) < 1:
                return "You must use browser_use for a real gameplay session before final verdict."

        successful = [
            item for item in (browser_actions or [])
            if item.get("ok")
        ]
        if not successful:
            return "You have not used the browser tool yet."

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

        def _is_verification_action(item: Dict[str, Any]) -> bool:
            action = str(item.get("action") or "").strip().lower()
            normalized = _normalized_action(item)
            return normalized in {"snapshot", "wait_for"} or action == "record_scroll"

        actions = [_normalized_action(item) for item in successful]
        if "snapshot" not in actions:
            return "You must inspect the page with browser.observe or browser.snapshot before final verdict."

        scroll_actions = [item for item in successful if _normalized_action(item) == "scroll"]
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

        seen_interaction = False
        has_post_verify = False
        seen_gameplay_input = False
        has_post_gameplay_verify = False
        for item in successful:
            action = _normalized_action(item)
            if action in {"click", "fill", "press", "press_sequence"}:
                seen_interaction = True
                if action in {"press", "press_sequence"}:
                    seen_gameplay_input = True
                continue
            if seen_interaction and _is_verification_action(item):
                has_post_verify = True
            if seen_gameplay_input and _is_verification_action(item):
                has_post_gameplay_verify = True

        if task_type == "game":
            if "click" not in actions:
                return "You must click the start/play control before final verdict."
            if not any(action in {"press", "press_sequence"} for action in actions):
                return "You must test gameplay controls with press_sequence or press before final verdict."
            if not has_post_gameplay_verify:
                return (
                    "You must verify gameplay controls with browser.observe, browser.snapshot, "
                    "wait_for, or record_scroll after gameplay input before final verdict."
                )
            return None

        if task_type == "dashboard":
            if "click" not in actions:
                return "You must click at least one dashboard control before final verdict."
            if not has_post_verify:
                return (
                    "After clicking a dashboard control, you must call browser.observe, "
                    "browser.snapshot, wait_for, or record_scroll to verify the changed state."
                )
            return None

        if task_type == "website":
            if node_type == "reviewer" and "scroll" not in actions:
                return "Website reviews must scroll the page before final verdict."
            if node_type == "reviewer" and scroll_actions and scroll_boundary_known and not reached_scroll_boundary:
                return "You must keep scrolling until you reach the bottom of the page or confirm the page is non-scrollable."
            if node_type == "tester" and "scroll" not in actions:
                return "Website tests must scroll the page before final verdict."
            if node_type == "tester" and scroll_actions and scroll_boundary_known and not reached_scroll_boundary:
                return "You must keep scrolling until you reach the bottom of the page or confirm the page is non-scrollable."
            if task_classifier.wants_multi_page(goal):
                requested_pages = max(task_classifier.requested_page_count(goal), 2)
                artifact_pages = self._current_output_html_pages()
                use_artifact_pages = 2 <= len(artifact_pages) <= requested_pages
                expected_pages = len(artifact_pages) if use_artifact_pages else requested_pages
                visited_pages = {
                    self._normalize_review_preview_path(item.get("url", ""))
                    for item in successful
                    if self._normalize_review_preview_path(item.get("url", ""))
                }
                missing_pages = [
                    page for page in artifact_pages
                    if page and page not in visited_pages
                ] if use_artifact_pages else []
                if len(visited_pages) < expected_pages:
                    missing_suffix = ""
                    if missing_pages:
                        missing_suffix = " Remaining missing pages: " + ", ".join(missing_pages[:8]) + "."
                    return (
                        "You must cover every requested page/route before final verdict. "
                        "Validate at least one internal navigation action first; after that, direct visits to the remaining known preview paths are acceptable. "
                        f"Current distinct pages visited: {len(visited_pages)}/{expected_pages}."
                        f"{missing_suffix}"
                    )
            if not any(action in {"click", "fill"} for action in actions):
                return "You must click or fill at least one interactive element before final verdict."
            if not has_post_verify:
                return (
                    "After interacting with the website, you must call browser.observe, "
                    "browser.snapshot, wait_for, or record_scroll to verify the changed state."
                )
            return None

        if not any(action in {"click", "fill", "press", "press_sequence"} for action in actions):
            return "You must test at least one interactive control before final verdict."
        if not has_post_verify:
            return (
                "After interaction, you must call browser.observe, browser.snapshot, "
                "wait_for, or record_scroll to verify the changed state."
            )
        return None

    def _review_browser_followup_message(self, reason: str, task_type: str) -> str:
        reason_text = str(reason or "").strip()
        lower = reason_text.lower()
        missing_pages: List[str] = []
        missing_match = re.search(r"Remaining missing pages:\s*([^.]*)", reason_text, re.IGNORECASE)
        if missing_match:
            missing_pages = [
                item.strip().strip(",")
                for item in missing_match.group(1).split(",")
                if item.strip()
            ]
        action_hint = 'Call browser with {"action":"observe"} now.'
        if "scroll" in lower:
            action_hint = (
                'Prefer calling browser with {"action":"record_scroll","amount":500} now. '
                'If that is unavailable, call browser with {"action":"scroll","direction":"down","amount":500} repeatedly '
                'until the browser reports the bottom of the page or the page is clearly non-scrollable.'
            )
        elif "bottom of the page" in lower or "non-scrollable" in lower:
            action_hint = (
                'Prefer calling browser with {"action":"record_scroll","amount":500} now. '
                'If that is unavailable, call browser with {"action":"scroll","direction":"down","amount":500} repeatedly '
                'until the browser reports at_bottom=true or the page is clearly non-scrollable.'
            )
        elif missing_pages or "every requested page" in lower or "page/route" in lower:
            if missing_pages:
                first_missing = missing_pages[0]
                remaining = ", ".join(missing_pages[:6])
                action_hint = (
                    "After proving one real internal navigation path, directly open the remaining preview routes. "
                    f'Call browser with {{"action":"navigate","url":"http://127.0.0.1:8765/preview/{first_missing}"}} '
                    'and then {"action":"observe"}; repeat for the remaining missing pages: '
                    f"{remaining}."
                )
            else:
                action_hint = (
                    'Use the real navigation links/buttons to open each requested page, then inspect every page before '
                    'finalizing your verdict.'
                )
        elif "start/play" in lower:
            action_hint = 'Call browser with {"action":"act","intent":"click","target":"Start Game"} or the visible play control now.'
        elif "browser_use" in lower and "gameplay session" in lower:
            action_hint = (
                'Call browser_use with {"url":"http://127.0.0.1:8765/preview/","task":"Open the preview, click the visible '
                'Start/Play control, play for several moves with Arrow keys or WASD plus Space/Enter, and verify the HUD, '
                'camera, or scene changes while keeping recorded evidence."} now.'
            )
        elif "gameplay controls" in lower:
            action_hint = (
                'Call browser with {"action":"act","intent":"press_sequence","keys":["ArrowRight","ArrowRight","Space","ArrowLeft"],'
                '"repeat":2,"interval_ms":150} now.'
            )
        elif (
            "wait_for or snapshot" in lower
            or "browser.observe" in lower
            or "record_scroll" in lower
            or "changed state" in lower
        ):
            if task_type == "game":
                action_hint = 'Call browser with {"action":"observe"} now to verify the post-gameplay state changed.'
            else:
                action_hint = 'Call browser with {"action":"observe"} now to verify the post-action state changed.'
        elif "browser.snapshot" in lower or "snapshot" in lower or "browser.observe" in lower:
            action_hint = 'Call browser with {"action":"observe"} now before final verdict.'
        return (
            "Your browser-based review/test is incomplete.\n"
            f"Missing requirement: {reason_text}\n"
            f"{action_hint}\n"
            "Do not output a final verdict yet. Use the browser tool immediately and only finalize after the missing step is complete."
        )

    def _review_output_format_followup_reason(self, node_type: str, output_text: str) -> Optional[str]:
        if normalize_node_role(node_type) != "reviewer":
            return None
        text = str(output_text or "").strip()
        if not text:
            return "Your final reviewer answer is empty. Return one strict JSON verdict object only."
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            try:
                parsed = json.loads(text[json_start:json_end])
                verdict = str(parsed.get("verdict") or "").strip().upper()
                if verdict in {"APPROVED", "REJECTED"}:
                    return None
            except Exception:
                pass
        return (
            "Return the final reviewer result as ONE strict JSON object only with "
            'verdict set to "APPROVED" or "REJECTED".'
        )

    def _review_output_format_followup_message(self, reason: str) -> str:
        reason_text = str(reason or "").strip()
        return (
            "Your reviewer output format is incomplete.\n"
            f"Missing requirement: {reason_text}\n"
            "Return ONLY one JSON object. No markdown fences. No bullet lists. No commentary before or after.\n"
            "Required shape:\n"
            '{"verdict":"APPROVED" or "REJECTED","scores":{"layout":N,"color":N,"typography":N,"animation":N,"responsive":N,"functionality":N,"completeness":N,"originality":N},"ship_readiness":N,"average":N.N,"issues":[],"blocking_issues":[],"missing_deliverables":[],"required_changes":[],"acceptance_criteria":[],"strengths":[]}'
        )

    def _message_char_count(self, msg: Dict[str, Any]) -> int:
        total = 0
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str):
            total += len(reasoning)
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if isinstance(fn, dict):
                args = fn.get("arguments")
                if isinstance(args, str):
                    total += len(args)
        return total

    def _messages_char_count(self, messages: List[Dict[str, Any]]) -> int:
        total = 0
        for msg in messages or []:
            if isinstance(msg, dict):
                total += self._message_char_count(msg)
        return total

    def _tool_names_for_log(self, tools: List[Dict[str, Any]]) -> List[str]:
        names: List[str] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function")
            if isinstance(fn, dict):
                name = str(fn.get("name") or "").strip()
                if name:
                    names.append(name)
        return names

    def _format_latency_for_log(self, value: Optional[float]) -> str:
        if value is None or value < 0:
            return "none"
        return f"{value:.2f}s"

    # ── V4.4: Prompt Caching ─────────────────────────────────
    def _inject_prompt_cache_breakpoints(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Inject cache_control breakpoints for Anthropic prompt caching.

        Places ephemeral cache markers on:
        1. The system message (largest, most stable content)
        2. The first user message (task definition, also stable across tool loops)

        This allows Anthropic to cache the system prompt + initial task
        across consecutive tool-loop API calls within the same node execution,
        saving ~80-95% of input tokens on subsequent calls.

        For OpenAI-compatible gateways that don't support cache_control,
        the extra field is silently ignored by most implementations.
        """
        result = []
        system_tagged = False
        first_user_tagged = False

        for msg in messages:
            if not isinstance(msg, dict):
                result.append(msg)
                continue

            msg_copy = dict(msg)
            role = str(msg_copy.get("role") or "")
            content = msg_copy.get("content")

            # Tag system message
            if role == "system" and isinstance(content, str) and not system_tagged:
                msg_copy["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                system_tagged = True
            # Tag first user message
            elif role == "user" and isinstance(content, str) and not first_user_tagged:
                msg_copy["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                first_user_tagged = True

            result.append(msg_copy)

        if system_tagged:
            logger.debug("Prompt cache breakpoints injected: system=%s user=%s", system_tagged, first_user_tagged)
        return result

    # ── V4.4: Tool Call Dedup ────────────────────────────────
    # Inspired by Opencode-DCP: when the same tool is called with identical
    # name+args multiple times, older results are redundant.  Replace them
    # with a tiny placeholder to free token budget for fresh context.
    TOOL_DEDUP_PLACEHOLDER = "[DEDUP: identical call repeated later — see latest result]"

    def _dedup_tool_calls_in_history(self, messages: List[Dict[str, Any]]) -> int:
        """Deduplicate identical tool calls in message history (in-place).

        Scans backwards: the *last* occurrence of a (name, args) pair keeps
        its full result; earlier occurrences get their tool-result content
        replaced with a short placeholder.

        Returns the number of results deduplicated.
        """
        # Build a map:  (tool_name, args_hash) → list of (tool_call_id)
        # ordered by position (earliest first).
        from hashlib import md5 as _md5

        # Phase 1: collect all tool_call signatures
        sig_to_tc_ids: Dict[str, List[str]] = {}   # sig → [tc_id, ...]
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    continue
                tc_id = str(tc.get("id") or "")
                fn_name = str(fn.get("name") or "")
                fn_args = str(fn.get("arguments") or "")
                if not tc_id or not fn_name:
                    continue
                # Skip write/edit — those are side-effecting, not idempotent
                if fn_name == "file_ops":
                    try:
                        _parsed = json.loads(fn_args)
                        _action = str(_parsed.get("action", "")).lower()
                        if _action in ("write", "edit", "delete"):
                            continue
                    except Exception:
                        continue
                sig = _md5(f"{fn_name}:{fn_args}".encode()).hexdigest()
                sig_to_tc_ids.setdefault(sig, []).append(tc_id)

        # Phase 2: for sigs with ≥2 calls, mark all-but-last for dedup
        dedup_tc_ids: set = set()
        for sig, tc_ids in sig_to_tc_ids.items():
            if len(tc_ids) >= 2:
                # Keep only the last occurrence
                for tc_id in tc_ids[:-1]:
                    dedup_tc_ids.add(tc_id)

        if not dedup_tc_ids:
            return 0

        # Phase 3: replace tool-result content for deduped tc_ids
        deduped = 0
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            tc_id = msg.get("tool_call_id")
            if tc_id and tc_id in dedup_tc_ids:
                msg["content"] = self.TOOL_DEDUP_PLACEHOLDER
                deduped += 1

        if deduped:
            logger.info("Tool call dedup: replaced %d redundant tool results", deduped)
        return deduped

    def _prepare_messages_for_request(self, messages: List[Dict[str, Any]], model_name: str) -> List[Dict[str, Any]]:
        """
        V4.5: 5-layer compaction pipeline (Claude Code + SWE-agent style).

        Layer 0 — OBSERVATION MASK: Replace old tool results (>3 turns back) with
                  short summaries. Based on SWE-agent research showing tool results
                  are ~84% of context; masking reduces cost >50% with no perf loss.
        Layer 1 — SNIP: Per-message truncation (role-based char limits)
        Layer 2 — MICROCOMPACT: Tool call dedup + read-result summarization
        Layer 3 — CONTEXT COLLAPSE: Drop older context, keep recent turns
        Layer 4 — AUTOCOMPACT: Semantic sentence-level compression

        Each layer only activates when the previous layer leaves the total
        above MAX_REQUEST_TOTAL_CHARS.  This avoids over-compacting short
        conversations while aggressively shrinking long tool-loop sessions.
        """
        # ── Layer 0: OBSERVATION MASK — mask old tool results ──────
        # SWE-agent research: tool results are ~84% of context.
        # Replace tool results older than last 3 turns with short placeholders.
        _MASK_KEEP_RECENT = 2  # V4.5.1: 3→2 — only keep last 2 tool results intact
        _MASK_PLACEHOLDER = "[TOOL_RESULT_MASKED: see recent results for current state]"
        _tool_msg_indices = [
            i for i, m in enumerate(messages)
            if isinstance(m, dict) and str(m.get("role") or "") == "tool"
        ]
        _mask_cutoff = len(_tool_msg_indices) - _MASK_KEEP_RECENT
        if _mask_cutoff > 0:
            _indices_to_mask = set(_tool_msg_indices[:_mask_cutoff])
            for idx in _indices_to_mask:
                msg = messages[idx]
                content = msg.get("content")
                if isinstance(content, str) and len(content) > 200:
                    messages[idx] = dict(msg)
                    messages[idx]["content"] = _MASK_PLACEHOLDER

        # ── Layer 1: SNIP — per-message truncation ──────────────────
        prepared: List[Dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                try:
                    prepared.append(self._serialize_assistant_message(msg))
                except Exception:
                    continue
                continue

            normalized = dict(msg)
            role = str(normalized.get("role") or "")
            content = normalized.get("content")
            if isinstance(content, str):
                if role == "tool":
                    normalized["content"] = self._truncate_text(content, MAX_TOOL_RESULT_CHARS)
                elif role == "assistant":
                    normalized["content"] = self._truncate_text(content, MAX_ASSISTANT_REPLAY_CHARS)
                else:
                    normalized["content"] = self._truncate_text(content, MAX_MESSAGE_CONTENT_CHARS)

            reasoning = normalized.get("reasoning_content")
            if isinstance(reasoning, str):
                normalized["reasoning_content"] = self._truncate_text(reasoning, MAX_REASONING_REPLAY_CHARS)

            tool_calls = normalized.get("tool_calls")
            if isinstance(tool_calls, list):
                compact_calls = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        compact_calls.append(tc)
                        continue
                    tc_copy = dict(tc)
                    fn = tc_copy.get("function")
                    if isinstance(fn, dict):
                        fn_copy = dict(fn)
                        function_name = str(fn_copy.get("name") or "")
                        args = fn_copy.get("arguments")
                        if isinstance(args, str):
                            fn_copy["arguments"] = self._compact_tool_arguments_for_replay(function_name, args)
                        tc_copy["function"] = fn_copy
                    compact_calls.append(tc_copy)
                normalized["tool_calls"] = compact_calls

            prepared.append(normalized)

        original_total = sum(self._message_char_count(m) for m in prepared)
        if original_total <= MAX_REQUEST_TOTAL_CHARS:
            return self._fix_orphaned_tool_call_ids(prepared)

        # ── Layer 2: MICROCOMPACT — tool call dedup + read summarization ─
        dedup_count = self._dedup_tool_calls_in_history(prepared)
        # Summarize verbose read-only tool results (list/read actions)
        self._summarize_read_tool_results(prepared)

        l2_total = sum(self._message_char_count(m) for m in prepared)
        if l2_total <= MAX_REQUEST_TOTAL_CHARS:
            logger.info(
                "Microcompact sufficient: model=%s chars=%s->%s dedup=%s",
                model_name, original_total, l2_total, dedup_count,
            )
            return self._fix_orphaned_tool_call_ids(prepared)

        # ── Layer 3: CONTEXT COLLAPSE — drop older message content ───
        keep_last = max(4, MAX_CONTEXT_KEEP_LAST_MESSAGES)
        compact_upto = max(2, len(prepared) - keep_last)
        for idx, msg in enumerate(prepared):
            if idx >= compact_upto:
                continue
            role = str(msg.get("role") or "")
            if role in ("assistant", "tool", "user"):
                if isinstance(msg.get("content"), str) and msg.get("content"):
                    msg["content"] = CONTEXT_OMITTED_MARKER
                if isinstance(msg.get("reasoning_content"), str):
                    msg["reasoning_content"] = ""
                # Keep tool_call structure but drop large historical args.
                if role == "assistant" and isinstance(msg.get("tool_calls"), list):
                    trimmed_calls = []
                    for tc in msg["tool_calls"]:
                        if not isinstance(tc, dict):
                            trimmed_calls.append(tc)
                            continue
                        tc_copy = dict(tc)
                        fn = tc_copy.get("function")
                        if isinstance(fn, dict):
                            fn_copy = dict(fn)
                            if isinstance(fn_copy.get("arguments"), str):
                                fn_copy["arguments"] = "{}"
                            tc_copy["function"] = fn_copy
                        trimmed_calls.append(tc_copy)
                    msg["tool_calls"] = trimmed_calls

        l3_total = sum(self._message_char_count(m) for m in prepared)
        if l3_total > MAX_REQUEST_TOTAL_CHARS:
            # Safety clamp: shrink non-critical messages proportionally.
            per_msg_budget = max(256, MAX_REQUEST_TOTAL_CHARS // max(1, len(prepared)))
            for idx, msg in enumerate(prepared):
                if idx < 2:
                    continue
                if isinstance(msg.get("content"), str):
                    msg["content"] = self._truncate_text(msg["content"], per_msg_budget)
                if isinstance(msg.get("reasoning_content"), str):
                    msg["reasoning_content"] = self._truncate_text(msg["reasoning_content"], min(512, per_msg_budget // 2))
            l3_total = sum(self._message_char_count(m) for m in prepared)

        logger.warning(
            "Context compacted L1→L3: model=%s chars=%s->%s->%s messages=%s dedup=%s",
            model_name,
            original_total,
            l2_total,
            l3_total,
            len(prepared),
            dedup_count,
        )

        # ── Layer 4: AUTOCOMPACT — semantic compression ──────────────
        # After structural compaction, apply sentence-level compression
        # to remaining verbose messages to further reduce token count.
        try:
            _pre_compress_chars = sum(len(str(m.get("content") or "")) for m in prepared)
            # V4.4: More aggressive compression ratio for long tool-loops
            _target_ratio = 0.50 if l3_total > MAX_REQUEST_TOTAL_CHARS * 0.8 else 0.65
            prepared = _compress_messages(
                prepared,
                target_ratio=_target_ratio,
                preserve_last_n=max(4, MAX_CONTEXT_KEEP_LAST_MESSAGES),
            )
            _post_compress_chars = sum(len(str(m.get("content") or "")) for m in prepared)
            _saved_pct = round((1 - _post_compress_chars / max(_pre_compress_chars, 1)) * 100)
            if _saved_pct > 0:
                logger.info(
                    "Autocompact L4: %d→%d chars (-%d%%, ratio=%.2f)",
                    _pre_compress_chars, _post_compress_chars, _saved_pct, _target_ratio,
                )
        except Exception as _comp_exc:
            logger.info("Autocompact L4 skipped: %s", str(_comp_exc)[:120])

        # ── Fix: Validate tool_call_id sequencing after compaction ──
        prepared = self._fix_orphaned_tool_call_ids(prepared)

        return prepared

    def _summarize_read_tool_results(self, messages: List[Dict[str, Any]]) -> None:
        """Microcompact helper: shrink verbose read/list tool results.

        For file_ops read results over 2000 chars, replace content with
        a byte-count summary.  For list results, cap at 30 entries.
        This runs in-place.
        """
        _READ_SUMMARY_THRESHOLD = 2000
        _LIST_MAX_ENTRIES = 30

        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < _READ_SUMMARY_THRESHOLD:
                continue

            # Try to detect read results with file content
            try:
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    continue
                data = parsed.get("data") or parsed
                if not isinstance(data, dict):
                    continue

                # Summarize read results: keep path + first/last lines
                file_content = data.get("content")
                if isinstance(file_content, str) and len(file_content) > _READ_SUMMARY_THRESHOLD:
                    lines = file_content.split("\n")
                    keep_head = min(10, len(lines))
                    keep_tail = min(5, max(0, len(lines) - keep_head))
                    summary_lines = lines[:keep_head]
                    if keep_tail:
                        summary_lines.append(f"... [{len(lines) - keep_head - keep_tail} lines omitted] ...")
                        summary_lines.extend(lines[-keep_tail:])
                    data["content"] = "\n".join(summary_lines)
                    data["_compacted"] = True
                    msg["content"] = json.dumps(parsed, ensure_ascii=False)
                    continue

                # Summarize list results: cap entries
                entries = data.get("entries") or data.get("files") or data.get("items")
                if isinstance(entries, list) and len(entries) > _LIST_MAX_ENTRIES:
                    key = "entries" if "entries" in data else ("files" if "files" in data else "items")
                    data[key] = entries[:_LIST_MAX_ENTRIES]
                    data["_truncated_from"] = len(entries)
                    msg["content"] = json.dumps(parsed, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                continue

    def _fix_orphaned_tool_call_ids(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Validate tool_call_id sequencing: every assistant message with tool_calls
        must be followed by tool messages responding to each tool_call_id.
        Inject synthetic error responses for orphaned tool_call_ids.
        """
        # Collect all tool_call_ids that have responses
        responded_ids: set = set()
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    responded_ids.add(tc_id)

        # Find orphaned tool_call_ids from assistant messages
        orphaned: List[tuple] = []  # (insert_after_idx, tc_id, fn_name)
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            tool_calls = msg.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id", "")
                fn = tc.get("function") or {}
                fn_name = fn.get("name", "unknown") if isinstance(fn, dict) else "unknown"
                if tc_id and tc_id not in responded_ids:
                    orphaned.append((idx, tc_id, fn_name))

        if not orphaned:
            return messages

        logger.warning(
            "Fixing %s orphaned tool_call_ids in message history: %s",
            len(orphaned),
            [(tc_id, fn_name) for _, tc_id, fn_name in orphaned[:8]],
        )

        # Group orphans by their assistant message index
        orphan_by_idx: Dict[int, List[tuple]] = {}
        for assistant_idx, tc_id, fn_name in orphaned:
            orphan_by_idx.setdefault(assistant_idx, []).append((tc_id, fn_name))

        # Build result with injected synthetic tool responses
        result: List[Dict[str, Any]] = []
        for idx, msg in enumerate(messages):
            result.append(msg)
            if idx in orphan_by_idx:
                # Find the correct insertion point: after the last existing tool
                # response for this assistant message, or immediately after it.
                for tc_id, fn_name in orphan_by_idx[idx]:
                    result.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps({
                            "success": False,
                            "error": f"Tool call '{fn_name}' was truncated or interrupted. Context was compacted.",
                            "data": {},
                            "artifacts": [],
                        }),
                    })

        return result

    def _builder_web_research_enabled(self) -> bool:
        return is_builder_browser_enabled(config=self.config)

    def _compose_system_prompt(
        self,
        node: Dict[str, Any],
        plugins: Optional[List[Plugin]] = None,
        input_data: str = "",
    ) -> str:
        """Compose and compress the system prompt for a node."""
        raw = self._compose_system_prompt_raw(node, plugins=plugins, input_data=input_data)
        return compress_system_prompt(raw)

    def _compose_system_prompt_raw(
        self,
        node: Dict[str, Any],
        plugins: Optional[List[Plugin]] = None,
        input_data: str = "",
    ) -> str:
        node_type = node.get("type", "builder")
        normalized_node_type = normalize_node_role(node_type) or str(node_type or "builder")
        preset = AGENT_PRESETS.get(str(node_type or ""), AGENT_PRESETS.get(normalized_node_type, {}))
        base_prompt = node.get("prompt") or preset.get("instructions", "You are a helpful assistant.")
        prompt_source = str(input_data or node.get("goal") or node.get("task") or "").strip()
        # V4.3: Language-aware reports for ALL node types (was only 5).
        # Without this, builder/merger/tester execution_report sections
        # ignore the user's language setting.
        ui_language = str(self.config.get("ui_language", "") or "").strip().lower()
        if ui_language:
            lang_name = "Chinese (简体中文)" if ui_language == "zh" else "English"
            language_directive = (
                f"\n\nREPORT LANGUAGE: Write your entire report, analysis, and execution_report in {lang_name}. "
                f"Use natural {lang_name} for all headings, descriptions, and analysis content. "
                "Technical terms (function names, file paths, library names, variable names) should keep their English form.\n"
            )
            base_prompt = base_prompt + language_directive
        # v4.0: Inject universal execution protocol for all node types
        base_prompt = base_prompt + BASE_HARNESS_PREAMBLE
        if normalized_node_type == "builder":
            prompt_source = self._builder_goal_hint_source(
                goal_hint=str(node.get("goal") or ""),
                input_data=prompt_source,
            ) or prompt_source
        skill_block = build_skill_context(normalized_node_type, prompt_source)
        skill_names = resolve_skill_names_for_goal(normalized_node_type, prompt_source)
        builder_delivery_mode = ""
        if normalized_node_type == "builder":
            builder_delivery_mode = str(node.get("builder_delivery_mode") or "").strip().lower()
        repo_context = None
        # Direct preview-delivery builders own /tmp output artifacts, not the shared repo.
        # Keep repo-edit guidance out even if upstream retry text mentions repo-ish paths.
        if not (
            normalized_node_type == "builder"
            and builder_delivery_mode in {"direct_text", "direct_multifile"}
        ):
            repo_context = build_repo_context(normalized_node_type, prompt_source, self.config)
        runtime_output_block = self._runtime_output_prompt_block()
        skill_contract = ""
        if skill_names:
            skill_contract = (
                "\n═══ MANDATORY SKILL DIRECTIVES ═══\n"
                "The following skills are loaded as MANDATORY operational directives for this execution.\n"
                "You MUST follow every instruction in each skill section below.\n"
                "Ignoring a skill directive is treated as a task failure by the reviewer.\n\n"
                "ACTIVE SKILLS:\n"
                + "\n".join(f"  ✓ {name}" for name in skill_names)
                + "\n\nSKILL COMPLIANCE CHECKLIST (verify before finishing):\n"
                + "\n".join(f"  - [{name}] Applied its core directives? YES/NO" for name in skill_names[:6])
                + "\n═══ END SKILL DIRECTIVES ═══\n"
            )
        strategy_block = self._node_execution_strategy_prompt_block(node)
        repo_block = ""
        if repo_context:
            repo_root = str(repo_context.get("repo_root") or "").strip()
            repo_map_prompt = str(repo_context.get("prompt_block") or "").strip()
            repo_block = (
                "\n\nEXISTING REPOSITORY EDIT MODE:\n"
                f"- Work inside the existing repository at {repo_root}\n"
                "- This is NOT a greenfield one-file HTML task unless the user explicitly asks for that\n"
                "- Start from the repo map, inspect only the relevant files, then make the smallest coherent edits\n"
                "- Preserve the repo's architecture, naming, build system, and existing conventions\n"
                "- Prefer editing existing files over creating parallel replacements or shadow copies\n"
                "- If shell-based verification is available, run the smallest relevant verification command before finishing\n"
            )
            if repo_map_prompt:
                repo_block += f"\nAIDER-STYLE REPO MAP:\n{repo_map_prompt}"
        if node_type != "builder":
            backend_hint = ""
            if node_type == "imagegen":
                asset_mode = task_classifier.game_asset_pipeline_mode(prompt_source)
                if is_image_generation_available(config=self.config):
                    backend_hint = (
                        "\nIMAGE BACKEND STATUS:\n"
                        "- Configured image backend detected\n"
                        "- Health-check the comfyui plugin first, then generate concrete assets if the run requires final art files\n"
                    )
                else:
                    if asset_mode == "3d":
                        backend_hint = (
                            "\nIMAGE BACKEND STATUS:\n"
                            "- No configured image backend detected in runtime settings\n"
                            "- Do NOT pretend to have raster generation\n"
                            "- For this 3D game goal, switch to modeling-design mode: produce character / monster / weapon / environment briefs, silhouette rules, material notes, rig-or-animation requirements, and builder-ready replacement guidance\n"
                            "- Land the minimum viable replacement pack first: 00_visual_target.md, 01_style_lock.md, manifest.json, character_hero_brief.md, monster_primary_brief.md, weapon_primary_brief.md, and environment_kit_brief.md\n"
                            "- Do NOT create optional orthographic/material/shortlist companion docs until that core pack is already complete and substantive\n"
                            "- If source_fetch or browser is available, use source_fetch first on a small set of permissive asset/model libraries or technical docs when the analyst handoff still lacks a critical reference; otherwise write the core pack immediately\n"
                            "- If you gather external asset references, keep them clearly licensed and limited to the most relevant sources (for example Kenney, Poly Pizza, Quaternius, ambientCG, or clearly licensed OpenGameArt entries)\n"
                            "- After the core pack is stable, you may add asset_sources.md or asset_license_matrix.md with replacement candidates and license notes\n"
                        )
                    else:
                        backend_hint = (
                            "\nIMAGE BACKEND STATUS:\n"
                            "- No configured image backend detected in runtime settings\n"
                            "- Do NOT pretend to have raster generation\n"
                            "- Return production-ready prompt packs, negative prompts, style locks, and replacement guidance instead\n"
                        )
            if skill_block:
                return f"{base_prompt}{strategy_block}{backend_hint}{runtime_output_block}\n\nLOADED NODE SKILLS:\n{skill_block}{skill_contract}{repo_block}" + EXECUTION_REPORT_TEMPLATE
            return base_prompt + strategy_block + backend_hint + runtime_output_block + repo_block + EXECUTION_REPORT_TEMPLATE

        # Builder system prompt is task-adaptive so game/dashboard/tool goals don't get
        # constrained by website-only guidance.
        adaptive_source = prompt_source
        if adaptive_source:
            try:
                base_prompt = task_classifier.builder_system_prompt(adaptive_source)
            except Exception:
                # Keep execution resilient if classifier has an unexpected runtime issue.
                pass

        if repo_block:
            if skill_block:
                return f"{base_prompt}{strategy_block}{runtime_output_block}\n\nLOADED NODE SKILLS:\n{skill_block}{skill_contract}{repo_block}" + EXECUTION_REPORT_TEMPLATE
            return base_prompt + strategy_block + runtime_output_block + repo_block + EXECUTION_REPORT_TEMPLATE

        has_browser_plugin = any(p and getattr(p, "name", "") == "browser" for p in (plugins or []))
        web_research_enabled = self._builder_web_research_enabled() and has_browser_plugin
        if web_research_enabled:
            mode_hint = (
                "\n\nWEB RESEARCH MODE (ENABLED):\n"
                "- Use browser observe / extract for quick style research (max 2 pages)\n"
                "- Use browser act only when a site requires one interaction to reveal important content\n"
                "- Extract structure/tone/color intent only; never copy site code\n"
                "- After research, produce a fresh implementation locally in one HTML file\n"
            )
        else:
            mode_hint = (
                "\n\nWEB RESEARCH MODE (DISABLED):\n"
                "- Do not rely on live web browsing\n"
                "- Achieve premium look using inline SVG icons, gradients, and robust layout system\n"
            )
        if skill_block:
            return f"{base_prompt}{strategy_block}{runtime_output_block}\n\nLOADED NODE SKILLS:\n{skill_block}{skill_contract}{mode_hint}" + EXECUTION_REPORT_TEMPLATE
        return base_prompt + strategy_block + runtime_output_block + mode_hint + EXECUTION_REPORT_TEMPLATE

    def _serialize_assistant_message(self, msg: Any) -> Dict[str, Any]:
        # Keep provider-specific fields (e.g. reasoning_content) if available.
        payload: Dict[str, Any] = {}
        if hasattr(msg, "model_dump"):
            try:
                dumped = msg.model_dump(exclude_none=True)
                if isinstance(dumped, dict):
                    payload.update(dumped)
            except Exception:
                pass
        elif hasattr(msg, "dict"):
            try:
                dumped = msg.dict(exclude_none=True)
                if isinstance(dumped, dict):
                    payload.update(dumped)
            except Exception:
                pass

        payload["role"] = "assistant"
        if "content" not in payload:
            payload["content"] = getattr(msg, "content", "") or ""

        # P0 FIX: Prevent empty assistant messages that trigger API 400 errors.
        # When finish=length strips tool calls, both content and tool_calls can be empty.
        # Inject a safe fallback to avoid "message must not be empty" from providers.
        tool_calls_raw = payload.get("tool_calls") or getattr(msg, "tool_calls", None) or []
        if not payload.get("content") and not tool_calls_raw:
            payload["content"] = "(output truncated by token limit — continuing)"

        reasoning_content = getattr(msg, "reasoning_content", None)
        if reasoning_content is not None and "reasoning_content" not in payload:
            payload["reasoning_content"] = reasoning_content

        tool_calls = payload.get("tool_calls")
        if tool_calls is None:
            tool_calls = getattr(msg, "tool_calls", None) or []
        normalized_tool_calls = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                normalized_tool_calls.append(tc)
                continue
            fn = getattr(tc, "function", None)
            normalized_tool_calls.append({
                "id": getattr(tc, "id", ""),
                "type": getattr(tc, "type", "function"),
                "function": {
                    "name": getattr(fn, "name", ""),
                    "arguments": getattr(fn, "arguments", "{}"),
                },
            })
        if normalized_tool_calls:
            payload["tool_calls"] = normalized_tool_calls
        return payload

    def _debug_log_tool_messages(self, messages: List[Dict[str, Any]], model_name: str):
        if not os.getenv("EVERMIND_DEBUG_KIMI_MESSAGES"):
            return
        try:
            for idx, m in enumerate(messages):
                if not isinstance(m, dict):
                    continue
                if m.get("role") != "assistant":
                    continue
                if not m.get("tool_calls"):
                    continue
                has_reasoning = "reasoning_content" in m and bool(m.get("reasoning_content"))
                logger.warning(
                    "Kimi debug msg[%s] model=%s tool_calls=%s has_reasoning_content=%s keys=%s",
                    idx,
                    model_name,
                    len(m.get("tool_calls") or []),
                    has_reasoning,
                    sorted(list(m.keys())),
                )
                if os.getenv("EVERMIND_DEBUG_KIMI_CALLS"):
                    print(
                        f"KIMI_DEBUG_MSG idx={idx} model={model_name} tool_calls={len(m.get('tool_calls') or [])} "
                        f"has_reasoning={has_reasoning} keys={sorted(list(m.keys()))}",
                        flush=True,
                    )
        except Exception:
            pass

    # ─────────────────────────────────────────
    # Pre-flight: check API key availability
    # ─────────────────────────────────────────
    def _check_api_key(self, model_name: str, model_info: Dict) -> Optional[str]:
        """Check if the required API key for a model is configured. Returns error string or None."""
        provider = model_info.get("provider", "")

        # Relay and ollama don't need API keys from us
        if provider in ("relay", "ollama", ""):
            return None

        recent_auth_failure = self._provider_recent_auth_failure_reason(model_info)
        if recent_auth_failure:
            return recent_auth_failure

        env_key = PROVIDER_ENV_KEY_MAP.get(provider)
        if not env_key:
            return None  # Unknown provider, let it try

        # Check config dict (from WS update_config) and env var
        config_key = env_key.lower()  # e.g. "openai_api_key"
        has_key = bool(self.config.get(config_key)) or bool(os.getenv(env_key))

        if not has_key:
            provider_names = {
                "openai": "OpenAI", "anthropic": "Anthropic/Claude",
                "google": "Google/Gemini", "deepseek": "DeepSeek",
                "kimi": "Kimi/Moonshot", "qwen": "Qwen/通义千问",
            }
            name = provider_names.get(provider, provider)
            return (
                f"未配置 {name} 的 API Key。"
                f"请在「设置 → 连接 → API 密钥」中填入 {name} 的 Key，"
                f"然后点击「保存到后端」。\n"
                f"The {name} API key is not configured. "
                f"Please go to Settings → Connection → API Keys and enter your {name} key."
            )
        return None

    def _should_retry_same_model(
        self,
        error_message: str,
        *,
        node_type: str = "",
        model_name: str = "",
        model_info: Optional[Dict[str, Any]] = None,
    ) -> bool:
        error_lower = str(error_message or "").lower()
        if not error_lower:
            return True
        non_retryable_markers = (
            "api key",
            "api_key",
            "invalid key",
            "not configured",
            "unauthorized",
            "forbidden",
            "401",
            "403",
            "authentication",
            "auth failed",
            "permission",
            "empty or invalid response from llm endpoint",
            "invalid response from llm endpoint",
            "received: '<!doctype html",
            "relay - ai api gateway",
            "your request was blocked",
            "request was blocked",
            "content policy",
            "safety system",
            "model not found",
            "does not exist",
            "unsupported model",
            "invalid request",
            "invalid_request_error",
            "unsupported input",
            "builder pre-write timeout",
            "polisher pre-write timeout",
            "polisher loop guard",
            "initial-activity timeout",
            "compatible gateway circuit open",
            "compatible gateway rejection cooldown",
            "returned empty content for builder node",
            "empty content for builder node",
        )
        if any(marker in error_lower for marker in non_retryable_markers):
            return False
        normalized_node_type = normalize_node_role(node_type)
        relay_fast_fail_markers = (
            "timeout",
            "timed out",
            "bad gateway",
            "gateway timeout",
            "remoteprotocolerror",
            "connection",
            "network",
            "socket",
            "dns",
            "service unavailable",
            "temporarily unavailable",
            "502",
            "503",
            "504",
        )
        if normalized_node_type in self._compatible_gateway_fail_fast_node_types() and any(
            marker in error_lower for marker in relay_fast_fail_markers
        ):
            if self._relay_model_candidates_for(model_name):
                return False
        if self._custom_compatible_gateway_base(model_info):
            gateway_fast_fail_markers = (
                "timeout",
                "timed out",
                "bad gateway",
                "gateway timeout",
                "remoteprotocolerror",
                "connection",
                "network",
                "socket",
                "dns",
                "provider",
                "service unavailable",
                "temporarily unavailable",
                "502",
                "503",
                "504",
            )
            if normalized_node_type in self._compatible_gateway_fail_fast_node_types() and any(
                marker in error_lower for marker in gateway_fast_fail_markers
            ):
                return False
        return True

    def _should_fallback_to_next_model(self, error_message: str, *, node_type: str = "") -> bool:
        error_lower = str(error_message or "").lower()
        if not error_lower:
            return False
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type == "polisher" and any(
            marker in error_lower
            for marker in (
                "polisher pre-write timeout",
                "polisher loop guard",
                "polisher deterministic gap gate failed",
                "polisher regression guard failed",
            )
        ):
            # Polisher is a refinement stage. If it fails before landing a safe write,
            # do not bounce into a slower fallback model and stall the full workflow.
            return False
        fallback_markers = (
            "api key",
            "api_key",
            "invalid key",
            "not configured",
            "unauthorized",
            "forbidden",
            "401",
            "403",
            "authentication",
            "auth failed",
            "permission",
            "rate limit",
            "too many requests",
            "quota",
            "overloaded",
            "temporarily unavailable",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "timeout",
            "timed out",
            "connection",
            "network",
            "socket",
            "dns",
            "remoteprotocolerror",
            "provider",
            "litellm",
            "model not found",
            "does not exist",
            "unsupported model",
            "invalid request",
            "your request was blocked",
            "request was blocked",
            "content policy",
            "safety system",
            "builder first-write timeout",
            "builder pre-write timeout",
            "polisher pre-write timeout",
            "polisher loop guard",
            "initial-activity timeout",
            "stalled",
            "502",
            "503",
            "504",
            "429",
            "compatible gateway circuit open",
            "compatible gateway rejection cooldown",
            "returned empty content for builder node",
            "empty content for builder node",
        )
        return any(marker in error_lower for marker in fallback_markers)

    # ─────────────────────────────────────────
    # Main dispatch
    # ─────────────────────────────────────────
    async def _execute_single_model(
        self,
        node: Dict,
        plugins: List[Plugin],
        masked_input: str,
        *,
        model_name: str,
        model_info: Dict,
        on_progress: Callable = None,
        total_candidate_count: int = 1,
    ) -> Dict:
        key_error = self._check_api_key(model_name, model_info)
        if key_error:
            logger.warning(f"API key missing for model {model_name}: {key_error[:80]}")
            return {
                "success": False,
                "output": "",
                "error": key_error,
                "model": model_name,
                "assigned_model": model_name,
                "assigned_provider": model_info.get("provider", ""),
            }

        node_type = node.get("type", "")
        normalized_node_type = normalize_node_role(node_type)
        assigned_builder_targets = self._builder_assigned_html_targets(masked_input)
        builder_goal_hint = (
            self._builder_goal_hint_source(
                goal_hint=str(node.get("goal") or ""),
                input_data=masked_input,
            )
            if normalized_node_type == "builder"
            else ""
        )
        builder_retry_context = (
            self._builder_has_retry_context(
                node_type,
                input_data=masked_input,
                node=node,
            )
            if normalized_node_type == "builder"
            else False
        )
        builder_retry_patch_context = (
            self._builder_retry_requires_existing_artifact_patch_context(
                node_type,
                input_data=masked_input,
                goal_hint=builder_goal_hint,
                node=node,
            )
            if normalized_node_type == "builder"
            else False
        )
        auto_builder_direct_multifile = self._builder_should_auto_direct_multifile(
            node_type,
            model_name=model_name,
            model_info=model_info,
            input_data=masked_input,
        )
        auto_builder_direct_text = self._builder_should_auto_direct_text(
            node_type,
            model_name=model_name,
            model_info=model_info,
            input_data=masked_input,
            goal_hint=builder_goal_hint,
            node=node,
        )
        requested_builder_direct_text = (
            normalized_node_type == "builder"
            and str(node.get("builder_delivery_mode") or "").strip().lower() == "direct_text"
        )
        if builder_retry_patch_context:
            auto_builder_direct_multifile = False
            auto_builder_direct_text = False
            requested_builder_direct_text = False
        retry_builder_direct_text = (
            self._builder_retry_prefers_direct_text(
                node_type,
                input_data=masked_input,
                goal_hint=builder_goal_hint,
                node=node,
            )
            if normalized_node_type == "builder"
            else False
        )
        safe_builder_direct_text = (
            (
                not builder_retry_patch_context
                and not builder_retry_context
                and (
                    task_classifier.game_direct_text_delivery_mode(builder_goal_hint)
                    or task_classifier.premium_3d_builder_direct_text_first_pass(builder_goal_hint)
                )
            )
            or retry_builder_direct_text
            if normalized_node_type == "builder"
            else False
        )
        force_builder_direct_multifile = (
            self._builder_direct_multifile_requested(node_type, masked_input)
            or (
                normalized_node_type == "builder"
                and str(node.get("builder_delivery_mode") or "").strip().lower() == "direct_multifile"
            )
            or auto_builder_direct_multifile
        )
        if builder_retry_patch_context:
            force_builder_direct_multifile = False
        force_builder_direct_text = (requested_builder_direct_text or auto_builder_direct_text) and safe_builder_direct_text
        effective_node = dict(node or {})
        effective_node["model"] = model_name
        if builder_retry_patch_context:
            effective_node.pop("builder_delivery_mode", None)
        if (
            normalized_node_type == "builder"
            and str(effective_node.get("builder_delivery_mode") or "").strip().lower() == "direct_text"
            and not safe_builder_direct_text
        ):
            effective_node.pop("builder_delivery_mode", None)
        if (
            force_builder_direct_multifile
            and normalized_node_type == "builder"
            and str(effective_node.get("builder_delivery_mode") or "").strip().lower() != "direct_multifile"
        ):
            effective_node["builder_delivery_mode"] = "direct_multifile"
        elif (
            force_builder_direct_text
            and normalized_node_type == "builder"
            and str(effective_node.get("builder_delivery_mode") or "").strip().lower() not in ("direct_text", "direct_multifile")
        ):
            effective_node["builder_delivery_mode"] = "direct_text"
        if force_builder_direct_multifile and on_progress:
            message = (
                "Builder retry switched to direct multi-file delivery mode to patch missing pages without tool loops."
                if not auto_builder_direct_multifile
                else "Builder auto-switched to direct multi-file delivery mode for Kimi multi-page delivery to avoid stalled first-write tool loops."
            )
            await self._emit_noncritical_progress(on_progress, {
                "stage": "system_info",
                "message": message,
                "builder_delivery_mode": "direct_multifile",
                "builder_direct_multifile": True,
                "assignedModel": model_name,
                "assignedProvider": model_info.get("provider", ""),
            })
        elif force_builder_direct_text and on_progress:
            message = (
                "Builder switched to direct single-file HTML delivery mode to avoid Kimi tool-call first-write stalls on game generation."
                if auto_builder_direct_text
                else "Builder switched to direct single-file HTML delivery mode."
            )
            await self._emit_noncritical_progress(on_progress, {
                "stage": "system_info",
                "message": message,
                "builder_delivery_mode": "direct_text",
                "builder_direct_text": True,
                "assignedModel": model_name,
                "assignedProvider": model_info.get("provider", ""),
            })
        elif requested_builder_direct_text and not safe_builder_direct_text and on_progress:
            await self._emit_noncritical_progress(on_progress, {
                "stage": "system_info",
                "message": (
                    "Builder direct single-file delivery was disabled for this engine/asset-heavy game brief. "
                    "Falling back to normal file_ops delivery for stability."
                ),
                "assignedModel": model_name,
                "assignedProvider": model_info.get("provider", ""),
            })

        max_retries = 2 if total_candidate_count > 1 else 3
        last_error = None
        result: Dict[str, Any] = {
            "success": False,
            "output": "",
            "error": "",
            "model": model_name,
            "assigned_model": model_name,
            "assigned_provider": model_info.get("provider", ""),
        }
        logger.info(
            "execute(): node=%s model=%s direct_multifile=%s direct_text=%s auto_direct=%s extra_headers=%s litellm=%s assigned_targets=%s candidate_count=%s",
            node_type,
            model_name,
            force_builder_direct_multifile,
            force_builder_direct_text,
            auto_builder_direct_multifile or auto_builder_direct_text,
            bool(model_info.get("extra_headers")),
            bool(self._litellm),
            len(assigned_builder_targets),
            total_candidate_count,
        )
        for attempt in range(max_retries):
            preflight_error = self._compatible_gateway_preflight_error(model_info)
            if preflight_error:
                # P0 FIX 2026-04-04: Try fallback gateway bases before giving up.
                # If relay.cn blocks gpt-5.4, try EVERMIND_OPENAI_FALLBACK_API_BASE.
                fallback_used = False
                for fb_base in self._fallback_gateway_bases(model_info):
                    fb_info = {**model_info, "api_base": fb_base}
                    if not self._compatible_gateway_preflight_error(fb_info):
                        model_info = fb_info
                        fallback_used = True
                        logger.info(
                            "Gateway fallback activated: %s -> %s for model=%s",
                            self._compatible_gateway_host(model_info) or "primary",
                            fb_base,
                            model_name,
                        )
                        break
                if not fallback_used:
                    result = {
                        "success": False,
                        "output": "",
                        "error": preflight_error,
                        "model": model_name,
                        "assigned_model": model_name,
                        "assigned_provider": model_info.get("provider", ""),
                    }
                    last_error = preflight_error
                    break
            attempt_started_at = time.time()
            try:
                if attempt > 0:
                    # v3.0: Optimized retry using RetryStrategy (500ms first retry, Retry-After, circuit breaker)
                    is_rate_limit = False  # Default; legacy branch below may override
                    if self._retry_strategy and _AGENTIC_RUNTIME_AVAILABLE:
                        provider = model_info.get("provider", "unknown")
                        # Circuit breaker check: skip immediately if provider is down
                        if self._retry_strategy.is_circuit_open(provider):
                            logger.warning("Circuit breaker OPEN for %s, skipping retry", provider)
                            break
                        wait = self._retry_strategy.get_wait_time(attempt, str(last_error or ""))
                        # Detect rate-limit for progress event even in v3.0 path
                        if last_error and any(
                            kw in str(last_error).lower()
                            for kw in ("429", "rate limit", "too many requests", "quota")
                        ):
                            is_rate_limit = True
                    else:
                        # Legacy fallback — v3.0: reduced wait (was 2^attempt, now 1+attempt)
                        base_wait = 1 + attempt  # 2s, 3s, 4s instead of 2s, 4s, 8s
                        jitter = random.uniform(0, 0.5)
                        is_rate_limit = last_error and any(
                            kw in str(last_error).lower()
                            for kw in ("429", "rate limit", "too many requests", "quota")
                        )
                        if is_rate_limit and attempt == 1:
                            base_wait = 5  # Was 8s — still respectful but faster
                        wait = base_wait + jitter
                    logger.info(f"Retry {attempt}/{max_retries} for {model_name}, waiting {wait:.1f}s...")
                    if on_progress:
                        await self._emit_noncritical_progress(on_progress, {
                            "stage": "retrying",
                            "attempt": attempt,
                            "wait": round(wait, 1),
                            "assignedModel": model_name,
                            "assignedProvider": model_info.get("provider", ""),
                            "is_rate_limited": bool(is_rate_limit),
                        })
                    await asyncio.sleep(wait)

                custom_gateway = bool(self._custom_compatible_gateway_base(model_info))

                # v3.0: AgenticLoop — Think-Act-Observe cycle for autonomous tool use
                # Only activated when EVERMIND_AGENTIC_LOOP=1 feature flag is set
                _agentic_eligible = (
                    self._agentic_loop_enabled()
                    and normalized_node_type in {"builder", "merger", "debugger", "polisher"}
                    and not force_builder_direct_text
                    and not force_builder_direct_multifile
                    and model_info.get("supports_tools")
                    and self._litellm
                )
                if _agentic_eligible:
                    try:
                        result = await self._execute_agentic_loop(
                            effective_node, plugins, masked_input, model_info, on_progress,
                        )
                    except Exception as agentic_exc:
                        logger.warning(
                            "AgenticLoop failed for node %s, falling back to standard path: %s",
                            effective_node.get("type", "unknown"), _sanitize_error(str(agentic_exc))[:200],
                        )
                        result = None  # Fall through to standard routing below
                elif (force_builder_direct_multifile or force_builder_direct_text) and (model_info.get("extra_headers") or custom_gateway):
                    result = await self._execute_openai_compatible_chat(effective_node, masked_input, model_info, on_progress)
                elif (force_builder_direct_multifile or force_builder_direct_text) and self._litellm:
                    result = await self._execute_litellm_chat(effective_node, masked_input, model_info, on_progress)
                elif force_builder_direct_multifile or force_builder_direct_text:
                    result = await self._execute_openai_direct(effective_node, [], masked_input, on_progress)
                elif model_info.get("supports_cua") and any(p.name == "computer_use" for p in plugins):
                    result = await self._execute_cua_loop(effective_node, plugins, masked_input, on_progress)
                elif model_info.get("provider") == "relay":
                    result = await self._execute_relay(effective_node, masked_input, model_info, on_progress)
                elif custom_gateway and plugins:
                    result = await self._execute_openai_compatible(
                        effective_node,
                        masked_input,
                        model_info,
                        on_progress,
                        plugins=plugins,
                    )
                elif custom_gateway:
                    result = await self._execute_openai_compatible_chat(
                        effective_node,
                        masked_input,
                        model_info,
                        on_progress,
                    )
                elif model_info.get("extra_headers"):
                    result = await self._execute_openai_compatible(
                        effective_node,
                        masked_input,
                        model_info,
                        on_progress,
                        plugins=plugins,
                    )
                elif self._litellm and model_info.get("supports_tools") and plugins:
                    result = await self._execute_litellm_tools(effective_node, plugins, masked_input, model_info, on_progress)
                elif self._litellm:
                    result = await self._execute_litellm_chat(effective_node, masked_input, model_info, on_progress)
                else:
                    result = await self._execute_openai_direct(effective_node, plugins, masked_input, on_progress)

                # AgenticLoop fallback: if it set result=None, fall through to standard path
                if result is None:
                    if self._litellm and model_info.get("supports_tools") and plugins:
                        result = await self._execute_litellm_tools(effective_node, plugins, masked_input, model_info, on_progress)
                    elif self._litellm:
                        result = await self._execute_litellm_chat(effective_node, masked_input, model_info, on_progress)
                    else:
                        result = await self._execute_openai_direct(effective_node, plugins, masked_input, on_progress)

                if not isinstance(result, dict):
                    result = {"success": False, "output": "", "error": "Invalid model response payload"}

                result["model"] = model_name
                result["assigned_model"] = model_name
                result["assigned_provider"] = model_info.get("provider", "")

                if result.get("success"):
                    try:
                        from settings import get_usage_tracker
                        tracker = get_usage_tracker()
                        usage = self._normalize_usage(result.get("usage", {}))
                        tracker.record(
                            model=model_name,
                            prompt_tokens=usage.get("prompt_tokens", 0),
                            completion_tokens=usage.get("completion_tokens", 0),
                            cost=float(result.get("cost", 0) or 0),
                            provider=model_info.get("provider", "unknown"),
                            mode=result.get("mode", "unknown"),
                        )
                    except Exception:
                        pass
                    _call_latency_ms = (time.time() - attempt_started_at) * 1000.0
                    self._record_compatible_gateway_success(
                        model_info,
                        latency_ms=_call_latency_ms,
                    )
                    result["latency_ms"] = int(round(_call_latency_ms))
                    self._record_provider_auth_success(model_info)
                    break

                last_error = result.get("error", "Unknown error")
                _fail_latency_ms = (time.time() - attempt_started_at) * 1000.0
                self._record_compatible_gateway_failure(
                    model_info,
                    last_error,
                    latency_ms=_fail_latency_ms,
                )
                # V4.3 FIX: Record timeout in gateway health so orchestrator
                # retries skip this model+gateway combo instead of retrying
                # from the same slow provider.
                if "timeout" in str(last_error).lower():
                    self._record_gateway_timeout(model_info, last_error, node_type=node_type)
                result["latency_ms"] = int(round(_fail_latency_ms))
                if not self._should_retry_same_model(
                    last_error,
                    node_type=node_type,
                    model_name=model_name,
                    model_info=model_info,
                ):
                    break

            except Exception as e:
                last_error = str(e)
                error_lower = last_error.lower()
                _exc_latency_ms = (time.time() - attempt_started_at) * 1000.0
                self._record_compatible_gateway_failure(
                    model_info,
                    last_error,
                    latency_ms=_exc_latency_ms,
                )
                # V4.3 FIX: Also mark timeout exceptions in gateway health
                if "timeout" in error_lower:
                    self._record_gateway_timeout(model_info, last_error, node_type=node_type)
                logger.warning(f"Execute attempt {attempt+1} failed: {_sanitize_error(last_error[:200])}")

                is_auth_error = any(kw in error_lower for kw in [
                    "auth", "api key", "api_key", "invalid key", "permission",
                    "unauthorized", "forbidden", "401", "403",
                ])
                if is_auth_error:
                    self._record_provider_auth_failure(model_info, last_error)
                    friendly = (
                        f"API 密钥无效或已过期，请在「设置」中重新填入正确的密钥。\n"
                        f"API key invalid or expired. Please update it in Settings.\n"
                        f"({_sanitize_error(last_error[:100])})"
                    )
                    result = {
                        "success": False,
                        "output": "",
                        "error": friendly,
                        "model": model_name,
                        "assigned_model": model_name,
                        "assigned_provider": model_info.get("provider", ""),
                        "latency_ms": int(round(_exc_latency_ms)),
                    }
                    break

                result = {
                    "success": False,
                    "output": "",
                    "error": _sanitize_error(last_error),
                    "model": model_name,
                    "assigned_model": model_name,
                    "assigned_provider": model_info.get("provider", ""),
                    "latency_ms": int(round(_exc_latency_ms)),
                }

        return result

    async def execute(self, node: Dict, plugins: List[Plugin], input_data: str,
                      model: str = "kimi-coding", on_progress: Callable = None,
                      privacy_settings: Dict = None) -> Dict:
        node_type = node.get("type", "")

        # ── Privacy: mask PII before sending to AI ──
        masker = get_masker(privacy_settings) if privacy_settings else get_masker()
        masked_input, restore_map = masker.mask(input_data, node_type=node_type)
        masked_input = self._apply_runtime_node_contracts(node, masked_input)
        if restore_map and on_progress:
            await self._emit_noncritical_progress(on_progress, {"stage": "privacy_masked", "pii_count": len(restore_map)})

        # ── CLI Backend Route ──
        # When cli_mode is enabled in settings, route execution to local CLI tools
        # instead of going through API relay endpoints.
        try:
            from cli_backend import is_cli_mode_enabled, get_executor
            _cli_settings = (getattr(self, "config", {}) or {}).get("cli_mode") or {}
            if not isinstance(_cli_settings, dict):
                _cli_settings = {}
            _cli_enabled = _cli_settings.get("enabled", False) or is_cli_mode_enabled()
            if _cli_enabled:
                _cli_executor = get_executor(self.config)
                _preferred_cli = _cli_settings.get("preferred_cli", "")
                _preferred_model = _cli_settings.get("preferred_model", "")
                _node_overrides = _cli_settings.get("node_cli_overrides", {})
                # Parse override: string (legacy) or {cli, model} (v2 format)
                _override = _node_overrides.get(node_type, "")
                if isinstance(_override, dict):
                    _cli_choice = _override.get("cli", "") or _preferred_cli
                    _model_choice = _override.get("model", "") or _preferred_model
                else:
                    _cli_choice = _override or _preferred_cli
                    _model_choice = _preferred_model
                _workspace = (self.config or {}).get("workspace", "")
                _cli_timeout = 600
                if node_type in ("analyst", "reviewer"):
                    _cli_timeout = 300
                elif node_type == "merger":
                    _cli_timeout = 720
                _cli_result = await _cli_executor.execute(
                    task=masked_input,
                    node_type=node_type,
                    workspace=_workspace,
                    timeout=_cli_timeout,
                    preferred_cli=_cli_choice or None,
                    preferred_model=_model_choice or None,
                    on_progress=on_progress,
                    node=node,
                )
                # Unmask PII in CLI output
                if restore_map and _cli_result.get("output"):
                    _cli_result["output"] = masker.unmask(_cli_result["output"], restore_map)
                    _cli_result["privacy_masked"] = len(restore_map)
                return _cli_result
        except ImportError:
            logger.debug("cli_backend module not available, using API route")
        except Exception as _cli_err:
            logger.warning("CLI backend error, falling back to API: %s", str(_cli_err)[:200])

        candidate_models = self.resolve_node_model_candidates(node, model)
        result: Dict[str, Any] = {"success": False, "output": "", "error": "No model candidates available"}
        attempted_models: List[str] = []
        if on_progress and candidate_models:
            first_info = self._resolve_model(candidate_models[0])
            await self._emit_noncritical_progress(on_progress, {
                "stage": "model_chain_resolved",
                "assignedModel": candidate_models[0],
                "assignedProvider": first_info.get("provider", ""),
                "candidateModels": candidate_models[:6],
            })

        for index, candidate_model in enumerate(candidate_models):
            model_info = self._resolve_model(candidate_model)
            provider = model_info.get("provider", "")
            attempted_models.append(candidate_model)
            if on_progress:
                await self._emit_noncritical_progress(on_progress, {
                    "stage": "model_selected",
                    "assignedModel": candidate_model,
                    "assignedProvider": provider,
                    "modelIndex": index + 1,
                    "modelCount": len(candidate_models),
                })

            result = await self._execute_single_model(
                node,
                plugins,
                masked_input,
                model_name=candidate_model,
                model_info=model_info,
                on_progress=on_progress,
                total_candidate_count=len(candidate_models),
            )
            result["model_candidates"] = candidate_models[:]
            result["attempted_models"] = attempted_models[:]
            result["model_chain_applied"] = len(candidate_models) > 1

            if result.get("success"):
                break

            error_message = str(result.get("error") or "").strip()
            next_model = ""
            next_index = -1
            if index + 1 < len(candidate_models):
                next_model, next_index = self._next_fallback_candidate(node_type, candidate_models, index)
            if next_model and self._should_fallback_to_next_model(error_message, node_type=node_type):
                next_info = self._resolve_model(next_model)
                logger.warning(
                    "Model fallback: node=%s from=%s to=%s error=%s",
                    node_type,
                    candidate_model,
                    next_model,
                    _sanitize_error(error_message[:200]),
                )
                if on_progress:
                    await self._emit_noncritical_progress(on_progress, {
                        "stage": "model_fallback",
                        "message": f"🔄 自动切换模型: {candidate_model} -> {next_model}",
                        "from_model": candidate_model,
                        "to_model": next_model,
                        "assignedModel": next_model,
                        "assignedProvider": next_info.get("provider", ""),
                    })
                continue
            if index + 1 < len(candidate_models) and self._should_fallback_to_next_model(error_message, node_type=node_type):
                logger.info(
                    "Suppressing model fallback: node=%s from=%s error=%s",
                    node_type,
                    candidate_model,
                    _sanitize_error(error_message[:200]),
                )
            break

        # ── Privacy: unmask PII in AI response ──
        if restore_map and result.get("output"):
            result["output"] = masker.unmask(result["output"], restore_map)
            result["privacy_masked"] = len(restore_map)

        report_lang = str((getattr(self, "config", {}) or {}).get("ui_language", "zh") or "zh").strip().lower()
        if report_lang not in {"zh", "en"}:
            report_lang = "zh"

        # v4.0: Extract AI-written execution report from output
        # V4.5: Search full output (was: last 3000 chars only — missed reports in long outputs)
        _full_raw = str(result.get("output") or "")
        _ai_report_match = re.search(
            r'<execution_report>(.*?)</execution_report>',
            _full_raw,
            re.DOTALL,
        )
        if _ai_report_match:
            _extracted_report = _ai_report_match.group(1).strip()
            # V4.5: Reject JSON-formatted reports — force natural language
            if _extracted_report.startswith('{') or _extracted_report.startswith('['):
                logger.debug("Rejected JSON-formatted execution report, will use fallback")
                _extracted_report = ""
            # V4.5: Strip code fences that wrap the report content
            if _extracted_report.startswith('```'):
                _extracted_report = re.sub(r'^```\w*\n?', '', _extracted_report)
                _extracted_report = re.sub(r'\n?```$', '', _extracted_report).strip()
            if _extracted_report:
                result["ai_execution_report"] = _extracted_report
            # Strip the report from main output to prevent downstream confusion
            _full_output = str(result.get("output") or "")
            _report_start = _full_output.rfind('<execution_report>')
            if _report_start >= 0:
                result["output"] = _full_output[:_report_start].rstrip()

        # ── v3.0: Generate node walkthrough report ──
        # Use the full ReportGenerator.from_agentic_result builder when the result
        # contains agentic-loop data (tool_results, files_created, etc.).
        # Falls back to manual construction for non-agentic results.
        if _REPORT_GENERATOR_AVAILABLE and result.get("success"):
            try:
                if result.get("tool_results") or result.get("files_created"):
                    # Rich agentic result — use the full builder (populates tool timeline,
                    # search queries, reference URLs, file change details)
                    node_report = ReportGenerator.from_agentic_result(
                        result,
                        node_label=node.get("label", node_type),
                        node_type=node_type,
                        node_key=node.get("key", ""),
                        model_used=result.get("assigned_model", model),
                        task_brief=masked_input[:500] if masked_input else "",
                        lang=report_lang,
                    )
                else:
                    # Standard LLM result — manual construction with available fields
                    # V4.5: Use larger limit (6000) to preserve meaningful report content
                    fallback_summary = self._compact_partial_output(str(result.get("output", "")))
                    node_report = NodeReport(
                        node_label=node.get("label", node_type),
                        node_type=node_type,
                        node_key=node.get("key", ""),
                        model_used=result.get("assigned_model", model),
                        task_brief=masked_input[:500] if masked_input else "",
                        outcome_summary=fallback_summary,
                        success=True,
                        tool_call_stats=result.get("tool_call_stats", {}),
                        total_tokens=result.get("usage", {}).get("total_tokens", 0),
                        prompt_tokens=result.get("usage", {}).get("prompt_tokens", 0),
                        completion_tokens=result.get("usage", {}).get("completion_tokens", 0),
                        cost=float(result.get("cost", 0) or 0),
                        iterations=result.get("iterations", 0),
                    )
                report_md = ReportGenerator.generate(node_report, lang=report_lang)
                result["walkthrough_report"] = report_md
                result["node_report"] = node_report.__dict__
            except Exception as report_err:
                logger.debug("Report generation failed (non-critical): %s", str(report_err)[:200])

        # ── v3.0: Attach handoff packet for downstream nodes ──
        # Use HandoffBuilder.from_agentic_result for richer handoff data when available.
        if _TASK_HANDOFF_AVAILABLE and result.get("success"):
            try:
                report_summary = ""
                if isinstance(result.get("node_report"), dict):
                    report_summary = str(result.get("node_report", {}).get("outcome_summary") or "").strip()
                if result.get("tool_results") or result.get("files_created"):
                    packet = HandoffBuilder.from_agentic_result(
                        result,
                        source_node=node.get("key", ""),
                        source_node_type=node_type,
                        output_summary=report_summary,
                        lang=report_lang,
                    )
                else:
                    packet = HandoffPacket(
                        source_node=node.get("key", ""),
                        source_node_type=node_type,
                        context_summary=report_summary or self._compact_partial_output(str(result.get("output", "")), limit=1800),
                        token_usage=result.get("usage", {}),
                        tool_calls_count=sum(result.get("tool_call_stats", {}).values()),
                    )
                result["handoff_packet"] = packet.to_dict()
                result["handoff_context_message"] = packet.to_context_message(lang=report_lang)
            except Exception:
                pass

        return result

    # ─────────────────────────────────────────
    # Path: Relay Endpoint
    # ─────────────────────────────────────────
    async def _execute_relay(self, node, input_data, model_info, on_progress) -> Dict:
        """Execute through a proxy/relay endpoint."""
        relay_mgr = get_relay_manager()
        relay_id = model_info.get("relay_id")
        model_name = str(
            model_info.get("relay_model_name")
            or model_info.get("relay_pool_model")
            or model_info.get("relay_target_model")
            or model_info.get("model_name")
            or ""
        ).strip()
        if not model_name:
            litellm_id = str(model_info.get("litellm_id") or "").strip()
            model_name = litellm_id.split("/", 1)[1].strip() if litellm_id.lower().startswith("openai/") else litellm_id
        relay_strategy = str(model_info.get("relay_strategy") or "").strip().lower()
        relay_label = model_info.get("relay_name", "?")
        if not model_name:
            return {"success": False, "output": "", "error": "Relay model name missing"}

        system_prompt = self._compose_system_prompt(node, input_data=input_data)

        if on_progress:
            await self._emit_noncritical_progress(
                on_progress,
                {"stage": "calling_relay", "relay": relay_label, "model": model_name},
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": input_data},
        ]
        if relay_strategy == "pool" or model_info.get("relay_pool_model"):
            result = await relay_mgr.call_best(
                model=model_name,
                messages=messages,
            )
        else:
            result = await relay_mgr.call(
                endpoint_id=relay_id,
                model=model_name,
                messages=messages,
            )

        if result.get("success"):
            return {
                "success": True,
                "output": result.get("content", ""),
                "model": result.get("model", model_name),
                "tool_results": [],
                "mode": "relay",
                "relay": result.get("relay", ""),
                "usage": self._normalize_usage(result.get("usage", {})),
                "cost": float(result.get("cost", 0) or 0),
            }
        else:
            return {"success": False, "output": "", "error": _sanitize_error(result.get("error", "Relay call failed"))}

    # ─────────────────────────────────────────
    # Path 1: CUA Responses Loop
    # ─────────────────────────────────────────
    async def _execute_cua_loop(self, node, plugins, input_data, on_progress) -> Dict:
        client = await self._get_openai()
        if not client:
            return {"success": False, "output": "", "error": "OpenAI API key not configured"}

        if on_progress:
            await self._emit_noncritical_progress(
                on_progress,
                {"stage": "cua_start", "instruction": input_data[:100]},
            )

        system_prompt = self._compose_system_prompt(node, plugins=plugins, input_data=input_data)

        tools = [{"type": "computer_use_preview", "display_width": 1920, "display_height": 1080, "environment": "mac"}]
        for p in plugins:
            if p.name != "computer_use":
                tools.append(p.get_tool_definition())

        input_messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": input_data}]
        all_artifacts, output_text, tool_results = [], "", []
        iteration, max_iterations = 0, 15
        usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        try:
            response = await client.responses.create(
                model="computer-use-preview", tools=tools, input=input_messages, truncation="auto"
            )
            usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
            while iteration < max_iterations:
                iteration += 1
                if on_progress:
                    await self._emit_noncritical_progress(
                        on_progress,
                        {"stage": "cua_iteration", "iteration": iteration, "max": max_iterations},
                    )
                has_action, new_input = False, []
                for item in response.output:
                    item_type = getattr(item, "type", None)
                    if item_type == "message":
                        for block in getattr(item, "content", []):
                            if hasattr(block, "text"):
                                output_text += block.text
                    elif item_type == "computer_call":
                        has_action = True
                        action = item.action
                        if on_progress:
                            await self._emit_noncritical_progress(
                                on_progress,
                                {"stage": "cua_action", "action": getattr(action, "type", "unknown"), "iteration": iteration},
                            )
                        await self._execute_cua_action(action, plugins)
                        ss_plugin = PluginRegistry.get("screenshot")
                        ss_b64 = ""
                        if ss_plugin:
                            ss_result = await ss_plugin.execute({}, context=self.config)
                            if ss_result.success and ss_result.artifacts:
                                ss_b64 = ss_result.artifacts[0].get("base64", "")
                                all_artifacts.append(ss_result.artifacts[0])
                        new_input.append({"type": "computer_call_output", "call_id": item.call_id,
                                          "output": {"type": "computer_screenshot", "image_url": f"data:image/png;base64,{ss_b64}"}})
                    elif item_type == "function_call":
                        has_action = True
                        result = await self._run_plugin(
                            item.name,
                            item.arguments,
                            plugins,
                            node_type=node.get("type", "builder"),
                            node=node,
                        )
                        tool_results.append(result)
                        new_input.append({"type": "function_call_output", "call_id": item.call_id, "output": json.dumps(result)})
                if not has_action:
                    break
                response = await client.responses.create(model="computer-use-preview", tools=tools, input=new_input, truncation="auto")
                usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
            return {"success": True, "output": output_text, "tool_results": tool_results, "artifacts": all_artifacts,
                    "model": "computer-use-preview", "iterations": iteration, "mode": "cua_loop", "usage": usage_totals,
                    "cost": self._estimate_response_cost("computer-use-preview", usage_totals)}
        except Exception as e:
            logger.error(f"CUA loop error: {_sanitize_error(str(e))}")
            return {"success": False, "output": output_text, "error": _sanitize_error(str(e))}

    async def _execute_cua_action(self, action, plugins) -> Dict:
        action_type = getattr(action, "type", "unknown")
        ui = PluginRegistry.get("ui_control")
        try:
            if action_type == "click" and ui:
                return (await ui.execute({"action": "click", "x": getattr(action, "x", 0), "y": getattr(action, "y", 0)}, context=self.config)).to_dict()
            elif action_type == "type" and ui:
                return (await ui.execute({"action": "type", "text": getattr(action, "text", "")}, context=self.config)).to_dict()
            elif action_type == "scroll" and ui:
                return (await ui.execute({"action": "scroll", "amount": getattr(action, "amount", -3)}, context=self.config)).to_dict()
            elif action_type == "key" and ui:
                return (await ui.execute({"action": "hotkey", "keys": getattr(action, "keys", [])}, context=self.config)).to_dict()
            elif action_type == "screenshot":
                ss = PluginRegistry.get("screenshot")
                if ss: return (await ss.execute({}, context=self.config)).to_dict()
            elif action_type == "wait":
                await asyncio.sleep(getattr(action, "duration", 1))
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _compact_partial_output(self, text: Any, limit: int = 6000) -> str:
        """Compact output for report summaries.

        V4.5: Increased default limit 1800→6000 to preserve report quality.
        Uses 60/40 head/tail split to keep conclusions (usually at end).
        """
        preview = str(text or "").strip()
        if not preview:
            return ""
        if len(preview) <= limit:
            return preview
        head = int(limit * 0.6)
        tail = int(limit * 0.35)
        return f"{preview[:head]}\n\n[...truncated {len(preview) - head - tail} chars...]\n\n{preview[-tail:]}"

    @staticmethod
    def _count_code_metrics(text: str) -> Dict[str, Any]:
        """Count code output metrics for real-time progress display.

        Returns line counts, byte size, and detected language breakdown
        so the frontend can show e.g. '已输出 342 行代码 / 15.2KB (HTML+JS+CSS)'.
        """
        if not text:
            return {"total_lines": 0, "code_bytes": 0, "languages": []}
        lines = text.splitlines()
        total_lines = len(lines)
        code_bytes = len(text.encode("utf-8", errors="replace"))

        # Detect language breakdown from content markers
        languages: List[str] = []
        lower = text[:8000].lower()
        if "<html" in lower or "<!doctype" in lower or "<div" in lower:
            languages.append("HTML")
        if "<script" in lower or "function " in lower or "const " in lower or "let " in lower:
            languages.append("JS")
        # v3.0 fix: CSS detection requires <style> tag as anchor. No longer triggers
        # on bare `{` which exists in all JS code (was causing false positives).
        if "<style" in lower or ("color:" in lower and "margin:" in lower and "display:" in lower):
            languages.append("CSS")
        if "import three" in lower or "three.js" in lower or "scene.add" in lower:
            languages.append("Three.js")
        if "canvas" in lower and ("getcontext" in lower or "drawimage" in lower):
            languages.append("Canvas")

        # Count non-empty, non-comment lines as "code lines"
        code_lines = 0
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith(("//", "#", "/*", "*/", "<!--", "-->")):
                code_lines += 1

        return {
            "total_lines": total_lines,
            "code_lines": code_lines,
            "code_bytes": code_bytes,
            "code_kb": round(code_bytes / 1024, 1),
            "languages": languages[:5],
        }

    def _written_file_code_metrics(self, path: Any) -> Dict[str, Any]:
        """Read a just-written artifact so code metrics also update on file writes."""
        raw_path = str(path or "").strip()
        if not raw_path:
            return {}
        try:
            candidate = Path(raw_path)
            if not candidate.is_file():
                return {}
            if candidate.suffix.lower() not in {".html", ".htm", ".js", ".mjs", ".css", ".json"}:
                return {}
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return {}
        metrics = self._count_code_metrics(text)
        return {
            "code_lines": metrics.get("code_lines", 0),
            "total_lines": metrics.get("total_lines", 0),
            "code_kb": metrics.get("code_kb", 0),
            "languages": metrics.get("languages", []),
        }

    def _build_partial_output_event(self, text: Any, phase: str = "drafting") -> Optional[Dict[str, Any]]:
        partial_output = str(text or "").strip()
        preview = self._compact_partial_output(partial_output)
        if not preview:
            return None
        # v3.0: Include code metrics for real-time progress visibility
        metrics = self._count_code_metrics(partial_output)
        return {
            "stage": "partial_output",
            "phase": phase,
            "preview": preview,
            # Preserve the raw stream text so the orchestrator can salvage
            # builder timeouts from the real HTML instead of from a compacted UI preview.
            "partial_output": partial_output[:240000],
            "source": "model",
            # v3.0: Code output metrics for UI display
            "code_lines": metrics.get("code_lines", 0),
            "total_lines": metrics.get("total_lines", 0),
            "code_kb": metrics.get("code_kb", 0),
            "languages": metrics.get("languages", []),
        }

    async def _publish_partial_output(self, on_progress, text: Any, phase: str = "drafting") -> None:
        if not on_progress:
            return
        event = self._build_partial_output_event(text, phase=phase)
        if event:
            await self._emit_noncritical_progress(on_progress, event)

    def _attach_browser_action_events(
        self,
        payload: Dict[str, Any],
        browser_action_events: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        if browser_action_events:
            payload["browser_action_events"] = [
                dict(item)
                for item in browser_action_events
                if isinstance(item, dict)
            ]
        return payload

    # ─────────────────────────────────────────
    # Path 2.5: Direct OpenAI-compatible SDK (for APIs needing custom headers, e.g. Kimi Coding)
    #           Now supports tool calling for file_ops, shell, etc.
    # ─────────────────────────────────────────
    async def _execute_openai_compatible(self, node, input_data, model_info, on_progress, plugins=None) -> Dict:
        """Execute via OpenAI SDK directly with custom default_headers (bypasses LiteLLM).
        Now supports tool calling so AI can use file_ops/shell plugins."""
        from openai import OpenAI

        node_type = node.get("type", "builder")
        normalized_node_type = normalize_node_role(node_type)
        system_prompt = self._compose_system_prompt(node, plugins=plugins, input_data=input_data)
        model_name = model_info["litellm_id"].replace("openai/", "")
        max_tokens = self._max_tokens_for_node(
            node_type,
            retry_attempt=int(node.get("retry_attempt", 0)),
            node=node,
        )
        timeout_sec = self._effective_timeout_for_node(node_type, input_data, node=node)
        max_continuations = self._read_int_env("EVERMIND_MAX_CONTINUATIONS", 2, 0, 5)
        output_text = ""
        latest_stream_text = ""
        latest_stream_activity_at = 0.0
        meaningful_stream_activity_at = 0.0
        stream_activity_events = 0
        stream_has_initial_activity = False
        stream_has_meaningful_activity = False
        builder_stream_pending_write = False
        builder_stream_pending_write_at = 0.0
        builder_stream_pending_write_started_at = 0.0
        tool_results: List[Dict[str, Any]] = []
        builder_support_snapshots: Dict[str, Dict[str, Any]] = {}
        tool_call_stats: Dict[str, int] = {}
        stream_tool_calls_map: Dict[int, Dict[str, Any]] = {}
        builder_has_written_file = False
        polisher_has_written_file = False
        builder_non_write_streak = 0
        polisher_non_write_streak = 0
        custom_gateway = bool(self._custom_compatible_gateway_base(model_info))

        # Get API key
        api_key = self._resolved_api_key_for_model_info(model_info)

        if not api_key:
            return {"success": False, "output": "", "error": f"API key not configured for {model_info.get('provider')}"}

        logger.info(
            "_execute_openai_compatible: model=%s timeout=%ss stall=%ss tools=%s",
            model_name, timeout_sec, self._effective_stream_stall_timeout(node_type, input_data, node=node),
            len(plugins) if plugins else 0,
        )
        if on_progress:
            await self._emit_noncritical_progress(
                on_progress,
                {"stage": "calling_ai", "model": model_name, "mode": "openai_compatible"},
            )

        # Build tools from plugins
        # V4.5: Sort deterministically by function name for stable prefix caching.
        # OpenAI auto-caches prefixes >1024 tokens; shuffled tool order breaks cache.
        tools = []
        if plugins:
            for p in plugins:
                if p.name != "computer_use":
                    defn = p.get_tool_definition()
                    tools.append({"type": "function", "function": defn} if "function" not in defn else defn)
            tools.sort(key=lambda t: str((t.get("function") or {}).get("name") or ""))
        available_tool_names = self._tool_names_from_defs(tools)
        qa_browser_use_available = self._qa_browser_use_available(node_type, available_tool_names)
        logger.info(
            "openai_compatible request profile: node=%s model=%s system_chars=%s user_chars=%s msg_chars=%s tool_names=%s assigned_targets=%s",
            normalized_node_type or node_type,
            model_name,
            len(system_prompt or ""),
            len(str(input_data or "")),
            self._messages_char_count([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_data},
            ]),
            self._tool_names_for_log(tools),
            len(self._builder_assigned_html_targets(input_data)) if normalized_node_type == "builder" else 0,
        )

        try:
            client = self._get_or_create_openai_client(
                api_key=api_key,
                base_url=self._resolved_api_base_for_model_info(model_info),
                extra_headers=model_info.get("extra_headers", {}),
                timeout=timeout_sec,
            )
            loop = asyncio.get_running_loop()

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_data},
            ]
            qa_task_type = self._classify_task_type(input_data)
            browser_action_events: List[Dict[str, Any]] = []
            await self._maybe_seed_qa_browser_use(
                node=node,
                node_type=node_type,
                task_type=qa_task_type,
                input_data=input_data,
                plugins=plugins,
                available_tool_names=available_tool_names,
                tool_results=tool_results,
                tool_call_stats=tool_call_stats,
                browser_action_events=browser_action_events,
                messages=messages,
                on_progress=on_progress,
            )
            await self._maybe_seed_qa_browser(
                node=node,
                node_type=node_type,
                task_type=qa_task_type,
                input_data=input_data,
                plugins=plugins,
                available_tool_names=available_tool_names,
                tool_results=tool_results,
                tool_call_stats=tool_call_stats,
                browser_action_events=browser_action_events,
                messages=messages,
                on_progress=on_progress,
            )

            # Streaming stall timeout: cancel when the stream stops producing chunks.
            stall_timeout = self._effective_stream_stall_timeout(node_type, input_data, node=node)

            def _call_streaming(msgs, tls, cancel_event: Optional[threading.Event] = None):
                """Make API call with streaming to detect stalls early."""
                nonlocal latest_stream_text, latest_stream_activity_at, meaningful_stream_activity_at
                nonlocal stream_has_initial_activity, stream_has_meaningful_activity
                nonlocal builder_stream_pending_write, builder_stream_pending_write_at
                nonlocal builder_stream_pending_write_started_at
                nonlocal stream_tool_calls_map
                nonlocal stream_activity_events
                stream_has_initial_activity = False
                stream_has_meaningful_activity = False
                meaningful_stream_activity_at = 0.0
                latest_stream_activity_at = 0.0
                stream_activity_events = 0
                builder_stream_pending_write = False
                builder_stream_pending_write_at = 0.0
                builder_stream_pending_write_started_at = 0.0
                stream_tool_calls_map = {}
                prepared_msgs = self._prepare_messages_for_request(msgs, model_name)
                # V4.4: Prompt caching — inject cache_control breakpoints
                # Anthropic: cache_control on system message content blocks
                # OpenAI: automatic for >1024 tokens, no action needed
                # Kimi/DeepSeek: cache via provider-side KV cache, no markers needed
                _provider = str(model_info.get("provider") or "").lower()
                if _provider in ("anthropic", "claude"):
                    prepared_msgs = self._inject_prompt_cache_breakpoints(prepared_msgs)
                kwargs = {
                    "model": model_name,
                    "messages": prepared_msgs,
                    "max_tokens": max_tokens,
                    "stream": True,
                    # F4-1: Request usage data in streaming mode for accurate token monitoring
                    "stream_options": {"include_usage": True},
                    # V4.3.1: Transport timeout uses 3x stall for first-chunk grace.
                    # relay can have high cold-start latency; 2x was too tight.
                    "timeout": stall_timeout * 3,
                }
                if tls:
                    kwargs["tools"] = tls
                    kwargs["tool_choice"] = "auto"
                if model_info.get("provider") == "kimi":
                    if os.getenv("EVERMIND_KIMI_THINKING", "disabled").lower() != "enabled":
                        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                # V4.3: thinking_depth → reasoning_effort for compatible models
                _thinking_depth = self._configured_thinking_depth()
                if _thinking_depth == "fast" and "reasoning_effort" not in kwargs:
                    if model_info.get("supports_reasoning_effort", False):
                        kwargs["reasoning_effort"] = "low"
                if os.getenv("EVERMIND_DEBUG_KIMI_CALLS"):
                    print(
                        f"KIMI_DEBUG_CALL model={model_name} msg_count={len(prepared_msgs)} "
                        f"tools={bool(tls)} stream=True",
                        flush=True,
                    )
                self._debug_log_tool_messages(prepared_msgs, model_name)
                stream = None

                # Collect streamed chunks with stall detection
                content_parts = []
                tool_calls_map = stream_tool_calls_map
                finish_reason = None
                usage_data = None
                last_chunk_time = time.time()
                last_preview_emit = 0.0
                last_pending_write_emit = 0.0
                stream_started_at = last_chunk_time
                first_chunk_at: Optional[float] = None
                first_content_at: Optional[float] = None
                first_tool_call_at: Optional[float] = None
                chunk_count = 0

                try:
                    stream = client.chat.completions.create(**kwargs)
                    for chunk in stream:
                        if cancel_event is not None and cancel_event.is_set():
                            raise asyncio.CancelledError()
                        now = time.time()
                        latest_stream_activity_at = now
                        # V4.3.1: First-chunk grace — 3x stall window before first content
                        # for relay cold-start / queue latency, then normal after.
                        _effective_stall = stall_timeout * 3 if first_content_at is None else stall_timeout
                        if now - last_chunk_time > _effective_stall:
                            raise TimeoutError(f"Stream stalled: no chunk for {_effective_stall:.0f}s")
                        if first_chunk_at is None:
                            first_chunk_at = now
                        chunk_count += 1
                        stream_activity_events += 1
                        last_chunk_time = now

                        if not chunk.choices:
                            # Usage-only final chunk
                            if hasattr(chunk, "usage") and chunk.usage:
                                usage_data = chunk.usage
                            continue

                        delta = chunk.choices[0].delta
                        if delta.content:
                            if first_content_at is None:
                                first_content_at = now
                            stream_has_initial_activity = True
                            content_parts.append(delta.content)
                            latest_stream_text = "".join(content_parts)
                            latest_trimmed = latest_stream_text.strip()
                            if (
                                latest_trimmed
                                and (
                                    len(latest_trimmed) >= 256
                                    or "<!doctype" in latest_trimmed.lower()
                                    or "<html" in latest_trimmed.lower()
                                    or "<body" in latest_trimmed.lower()
                                )
                            ):
                                stream_has_meaningful_activity = True
                                meaningful_stream_activity_at = now
                            if on_progress and (now - last_preview_emit >= 0.75):
                                event = self._build_partial_output_event(latest_stream_text, phase="streaming")
                                if event:
                                    try:
                                        asyncio.run_coroutine_threadsafe(on_progress(event), loop)
                                    except RuntimeError:
                                        pass
                                last_preview_emit = now
                        if delta.tool_calls:
                            for tc in delta.tool_calls:
                                idx = tc.index
                                if idx not in tool_calls_map:
                                    tool_calls_map[idx] = {
                                        "id": tc.id or "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                entry = tool_calls_map[idx]
                                if tc.id:
                                    entry["id"] = tc.id
                                if tc.function:
                                    if first_tool_call_at is None:
                                        first_tool_call_at = now
                                    stream_has_initial_activity = True
                                    if tc.function.name:
                                        entry["function"]["name"] = tc.function.name
                                    if tc.function.arguments:
                                        entry["function"]["arguments"] += tc.function.arguments
                                    if normalized_node_type == "builder":
                                        if self._builder_streaming_tool_call_looks_like_write(
                                            entry["function"].get("name", ""),
                                            entry["function"].get("arguments", ""),
                                        ):
                                            stream_has_meaningful_activity = True
                                            meaningful_stream_activity_at = now
                                            builder_stream_pending_write = True
                                            if builder_stream_pending_write_started_at <= 0:
                                                builder_stream_pending_write_started_at = now
                                            builder_stream_pending_write_at = now
                                            if on_progress and (now - last_pending_write_emit >= 5.0):
                                                # V4.3: Estimate code_lines from streaming tool_call args
                                                # so tool-path builders show real-time code progress like
                                                # direct_text builders do.
                                                _pw_args = entry["function"].get("arguments", "")
                                                _pw_metrics = self._count_code_metrics(_pw_args) if len(_pw_args) > 100 else {}
                                                try:
                                                    asyncio.run_coroutine_threadsafe(
                                                        on_progress({
                                                            "stage": "builder_pending_write",
                                                            "message": "Builder is still streaming a write-like file_ops payload.",
                                                            "code_lines": _pw_metrics.get("code_lines", 0),
                                                            "code_kb": _pw_metrics.get("code_kb", 0),
                                                            "languages": _pw_metrics.get("languages", []),
                                                        }),
                                                        loop,
                                                    )
                                                except RuntimeError:
                                                    pass
                                                last_pending_write_emit = now
                                    else:
                                        stream_has_meaningful_activity = True
                                        meaningful_stream_activity_at = now
                        if chunk.choices[0].finish_reason:
                            finish_reason = chunk.choices[0].finish_reason
                    if cancel_event is not None and cancel_event.is_set():
                        raise asyncio.CancelledError()
                finally:
                    _close_stream_quietly(stream)

                logger.info(
                    "openai_compatible stream stats: node=%s model=%s chunks=%s first_chunk=%s first_content=%s first_tool_call=%s finish=%s content_chars=%s tool_calls=%s total_stream=%s",
                    normalized_node_type or node_type,
                    model_name,
                    chunk_count,
                    self._format_latency_for_log(
                        None if first_chunk_at is None else first_chunk_at - stream_started_at
                    ),
                    self._format_latency_for_log(
                        None if first_content_at is None else first_content_at - stream_started_at
                    ),
                    self._format_latency_for_log(
                        None if first_tool_call_at is None else first_tool_call_at - stream_started_at
                    ),
                    finish_reason or "stop",
                    len("".join(content_parts)),
                    len(tool_calls_map),
                    self._format_latency_for_log(time.time() - stream_started_at),
                )

                # Broadcast stream stats to frontend via on_progress
                if on_progress:
                    _total_stream_sec = time.time() - stream_started_at
                    _first_content_sec = (first_content_at - stream_started_at) if first_content_at else None
                    _chars_per_sec = round(len("".join(content_parts)) / max(_total_stream_sec, 0.1), 1)
                    try:
                        asyncio.run_coroutine_threadsafe(
                            on_progress({
                                "stage": "stream_stats",
                                "model": model_name,
                                "chunks": chunk_count,
                                "first_content_sec": round(_first_content_sec, 2) if _first_content_sec else None,
                                "total_stream_sec": round(_total_stream_sec, 2),
                                "content_chars": len("".join(content_parts)),
                                "chars_per_sec": _chars_per_sec,
                                "tool_calls": len(tool_calls_map),
                            }),
                            loop,
                        )
                    except Exception:
                        pass

                # ── Fix: Validate tool call JSON when finish=length (Kimi K2.5 truncation) ──
                # When Kimi hits max_tokens mid-tool-call, the arguments JSON is incomplete.
                # Instead of discarding, try to repair JSON or extract HTML from the truncated args.
                if finish_reason == "length" and tool_calls_map:
                    valid_tc_map = {}
                    stripped_count = 0
                    recovered_html = None
                    for _idx, tc_data in tool_calls_map.items():
                        fn_name = tc_data["function"].get("name", "")
                        fn_args = tc_data["function"].get("arguments", "")
                        if not fn_name:
                            stripped_count += 1
                            continue
                        try:
                            json.loads(fn_args)
                            valid_tc_map[_idx] = tc_data
                        except (json.JSONDecodeError, ValueError):
                            # ── P0 FIX: Don't just discard — try to recover HTML from truncated args ──
                            repaired = False

                            # Strategy 0: Claude Code-style suffix guessing (openaiShim.ts pattern)
                            # Try cheap bracket/brace combinations to close truncated JSON.
                            # This handles the most common case where only closing delimiters are missing.
                            _suffix_combos = (
                                "}", '"}', "]}", '"]}', "}}", '"}}',
                                '"]}}', "]}}", '"}]}', '"]}}',
                                '"}]', '"]', '"}]}]}',
                            )
                            trimmed = fn_args.rstrip()
                            for suffix in _suffix_combos:
                                try:
                                    json.loads(trimmed + suffix)
                                    tc_data["function"]["arguments"] = trimmed + suffix
                                    valid_tc_map[_idx] = tc_data
                                    repaired = True
                                    logger.info(
                                        "Suffix-guessing repaired truncated tool call: node=%s fn=%s suffix=%r args_len=%s",
                                        normalized_node_type or node_type,
                                        fn_name,
                                        suffix,
                                        len(fn_args),
                                    )
                                    break
                                except (json.JSONDecodeError, ValueError):
                                    continue

                            # Strategy 1: json_repair library (best-effort structural repair)
                            if not repaired and _json_repair_fn and fn_name in ("file_ops", "write_file", "write"):
                                try:
                                    repaired_str = _json_repair_fn(fn_args)
                                    repaired_obj = json.loads(repaired_str) if isinstance(repaired_str, str) else repaired_str
                                    if isinstance(repaired_obj, dict) and repaired_obj.get("content"):
                                        tc_data["function"]["arguments"] = json.dumps(repaired_obj, ensure_ascii=False)
                                        valid_tc_map[_idx] = tc_data
                                        repaired = True
                                        logger.info(
                                            "json_repair recovered truncated tool call: node=%s fn=%s args_len=%s→%s",
                                            normalized_node_type or node_type,
                                            fn_name,
                                            len(fn_args),
                                            len(tc_data["function"]["arguments"]),
                                        )
                                except Exception as repair_exc:
                                    logger.debug("json_repair failed for %s: %s", fn_name, str(repair_exc)[:100])

                            # Strategy 2: Regex extract HTML content from truncated args
                            if not repaired and fn_name in ("file_ops", "write_file", "write"):
                                extracted = self._extract_html_from_truncated_tool_args(fn_args)
                                if extracted and len(extracted) > 500:
                                    recovered_html = extracted
                                    logger.info(
                                        "Regex-extracted HTML from truncated tool call: node=%s fn=%s "
                                        "raw_args_len=%s extracted_html_len=%s",
                                        normalized_node_type or node_type,
                                        fn_name,
                                        len(fn_args),
                                        len(extracted),
                                    )

                            if not repaired:
                                stripped_count += 1
                                logger.warning(
                                    "Stripped truncated tool call: node=%s model=%s fn=%s args_len=%s finish=length",
                                    normalized_node_type or node_type,
                                    model_name,
                                    fn_name,
                                    len(fn_args),
                                )
                    if stripped_count > 0:
                        logger.info(
                            "finish=length tool call sanitization: node=%s stripped=%s valid=%s recovered_html=%s",
                            normalized_node_type or node_type,
                            stripped_count,
                            len(valid_tc_map),
                            bool(recovered_html),
                        )
                    # If we recovered HTML from a truncated tool call, inject it into
                    # latest_stream_text so the salvage path can use it.
                    preferred_salvage = self._select_builder_salvage_text(
                        latest_stream_text,
                        output_text,
                        recovered_html=recovered_html,
                    )
                    if preferred_salvage and preferred_salvage != latest_stream_text:
                        latest_stream_text = preferred_salvage
                        logger.info(
                            "Injected preferred salvage HTML/text into latest_stream_text (%s chars, recovered_html=%s)",
                            len(preferred_salvage),
                            bool(recovered_html),
                        )
                    tool_calls_map = valid_tc_map
                    stream_tool_calls_map = valid_tc_map

                # Reassemble into a response-like object
                from types import SimpleNamespace
                tool_calls_list = None
                if tool_calls_map:
                    tool_calls_list = []
                    for _idx in sorted(tool_calls_map.keys()):
                        tc_data = tool_calls_map[_idx]
                        tool_calls_list.append(SimpleNamespace(
                            id=tc_data["id"],
                            type="function",
                            function=SimpleNamespace(
                                name=tc_data["function"]["name"],
                                arguments=tc_data["function"]["arguments"],
                            ),
                        ))

                message = SimpleNamespace(
                    content="".join(content_parts) if content_parts else None,
                    tool_calls=tool_calls_list,
                    role="assistant",
                )
                choice = SimpleNamespace(
                    message=message,
                    finish_reason=finish_reason or "stop",
                )
                response_obj = SimpleNamespace(
                    choices=[choice],
                    usage=usage_data,
                )
                return response_obj

            async def _force_builder_text_timeout_fallback(reason: str) -> Optional[Dict[str, Any]]:
                nonlocal builder_has_written_file, tool_results, tool_call_stats
                if normalized_node_type != "builder" or builder_has_written_file:
                    return None
                # P0 FIX: Prioritize recovered HTML from truncated tool args over
                # often-empty output_text (Kimi sends content_chars=0, all HTML in tool args)
                tool_result_text = self._builder_tool_result_salvage_text(tool_results, input_data)
                stream_tool_call_text = self._builder_stream_tool_call_salvage_text(stream_tool_calls_map, input_data)
                disk_result_text = self._builder_disk_salvage_text(
                    input_data,
                    incremental_text=output_text or latest_stream_text,
                )
                combined_salvage_text = "\n\n".join(
                    part
                    for part in (stream_tool_call_text, tool_result_text, disk_result_text)
                    if str(part or "").strip()
                )
                salvage_text = self._select_builder_salvage_text(
                    latest_stream_text,
                    output_text,
                    tool_result_text=combined_salvage_text,
                )
                async def _request_builder_continuation(cont_messages):
                    return await _await_stream_call(cont_messages, [])

                salvage_text, _, _ = await self._attempt_builder_game_text_continuation(
                    output_text=salvage_text,
                    input_data=input_data,
                    system_prompt=system_prompt,
                    continuation_count=0,
                    max_continuations=1,
                    request_continuation=_request_builder_continuation,
                    on_progress=on_progress,
                    log_prefix="Builder timeout salvage",
                )
                if not salvage_text or len(salvage_text) < 200:
                    # latest_stream_text may be empty when Kimi uses tool-call-only mode.
                    # The truncation handler above may have injected recovered HTML.
                    logger.debug(
                        "Salvage text thin (%s chars), latest_stream_text may contain recovered HTML",
                        len(salvage_text or ""),
                    )

                # ── P0 FIX C: If salvage_text has HTML, auto-save it to disk first ──
                # This handles the case where the prewrite timeout killed a text-mode
                # stream that had significant HTML content (e.g. 37k chars with full body).
                if salvage_text and len(salvage_text) > 2000:
                    saved_paths = await self._auto_save_builder_text_output(
                        output_text=salvage_text,
                        input_data=input_data,
                        node=node,
                        tool_results=tool_results,
                        tool_call_stats=tool_call_stats,
                        on_progress=on_progress,
                    )
                    if saved_paths:
                        builder_has_written_file = True
                        return {
                            "success": True,
                            "output": salvage_text,
                            "model": model_name,
                            "tool_results": tool_results,
                            "mode": "openai_compatible_text_mode_auto_save",
                            "tool_call_stats": dict(tool_call_stats),
                        }

                salvage = await self._builder_partial_text_salvage_result(
                    node=node,
                    input_data=input_data,
                    output_text=salvage_text,
                    reason=reason or "prewrite_call_timeout",
                    on_progress=on_progress,
                    tool_results=tool_results,
                    model_name=model_name,
                    mode="openai_compatible_partial_timeout_salvage",
                    tool_call_stats=tool_call_stats,
                )
                if salvage is not None:
                    return salvage
                if on_progress:
                    await self._emit_noncritical_progress(
                        on_progress,
                        self._forcing_text_progress_payload(node_type, reason or "prewrite_call_timeout"),
                    )
                forced_messages = self._builder_forced_text_messages(
                    system_prompt,
                    input_data,
                    tool_results,
                    output_text,
                    reason or "prewrite_call_timeout",
                )
                forced_timeout = self._read_int_env(
                    "EVERMIND_BUILDER_FORCED_TEXT_TIMEOUT_SEC",
                    45,
                    15,
                    120,
                )
                try:
                    final_resp = await asyncio.wait_for(
                        asyncio.to_thread(_call_streaming, forced_messages, []),
                        timeout=forced_timeout,
                    )
                except Exception as fallback_exc:
                    salvage = await self._builder_partial_text_salvage_result(
                        node=node,
                        input_data=input_data,
                        output_text=latest_stream_text or output_text,
                        reason=fallback_exc,
                        on_progress=on_progress,
                        tool_results=tool_results,
                        model_name=model_name,
                        mode="openai_compatible_partial_timeout_salvage",
                        tool_call_stats=tool_call_stats,
                    )
                    if salvage is not None:
                        return salvage
                    logger.warning(
                        "Builder forced text fallback after timeout failed: %s",
                        _sanitize_error(str(fallback_exc)),
                    )
                    return None

                final_msg = final_resp.choices[0].message
                final_content = getattr(final_msg, "content", "") or ""
                if not final_content.strip():
                    return await self._builder_partial_text_salvage_result(
                        node=node,
                        input_data=input_data,
                        output_text=latest_stream_text or output_text,
                        reason=reason or "forced_text_empty",
                        on_progress=on_progress,
                        tool_results=tool_results,
                        model_name=model_name,
                        mode="openai_compatible_partial_timeout_salvage",
                        usage=self._normalize_usage(getattr(final_resp, "usage", None)),
                        tool_call_stats=tool_call_stats,
                    )
                await self._publish_partial_output(on_progress, final_content, phase="finalizing")
                return {
                    "success": True,
                    "output": final_content,
                    "model": model_name,
                    "tool_results": tool_results,
                    "mode": "openai_compatible_forced_text_timeout",
                    "usage": self._normalize_usage(getattr(final_resp, "usage", None)),
                    "tool_call_stats": dict(tool_call_stats),
                }

            async def _await_stream_call(msgs, tls, timeout_override: Optional[float] = None):
                nonlocal latest_stream_text
                cancel_event = threading.Event()
                call_task = asyncio.create_task(asyncio.to_thread(_call_streaming, msgs, tls, cancel_event))

                def _request_stream_stop() -> None:
                    cancel_event.set()
                    call_task.cancel()

                timeout_phase = "hard"
                timeout_value = 0
                hard_timeout = 0
                prewrite_timeout = 0
                builder_tool_only_timeout = 0
                initial_activity_timeout = 0
                if normalized_node_type == "builder" and not builder_has_written_file:
                    prewrite_timeout = self._builder_prewrite_call_timeout(node_type, input_data, node=node)
                    builder_tool_only_timeout = self._builder_tool_only_call_timeout(node_type, input_data, node=node)
                    if builder_non_write_streak > 0:
                        repair_timeout = self._builder_repair_write_timeout(node_type, input_data, node=node)
                        prewrite_timeout = min(prewrite_timeout, repair_timeout)
                elif normalized_node_type == "polisher" and not polisher_has_written_file:
                    prewrite_timeout = self._polisher_prewrite_call_timeout(node_type, input_data)
                if custom_gateway:
                    initial_activity_timeout = self._gateway_initial_activity_timeout_for_node(node_type, input_data)
                # Always enforce a hard ceiling timeout — even after the first file write.
                if timeout_override is None:
                    hard_timeout = timeout_sec
                else:
                    try:
                        hard_timeout = max(1.0, min(float(timeout_override), float(timeout_sec)))
                    except Exception:
                        hard_timeout = timeout_sec
                pending_write_cap = (
                    self._builder_pending_write_stream_cap_seconds(prewrite_timeout, hard_timeout)
                    if normalized_node_type == "builder"
                    else 0.0
                )
                effective = prewrite_timeout if prewrite_timeout > 0 else hard_timeout
                logger.info(
                    "_await_stream_call: prewrite_timeout=%s tool_only_timeout=%s initial_activity_timeout=%s hard_timeout=%s pending_write_cap=%s effective=%s written=%s",
                    prewrite_timeout,
                    builder_tool_only_timeout,
                    initial_activity_timeout,
                    hard_timeout,
                    pending_write_cap,
                    effective,
                    builder_has_written_file if normalized_node_type == "builder" else polisher_has_written_file,
                )
                try:
                    started_at = time.time()
                    hard_deadline = started_at + hard_timeout
                    prewrite_deadline = started_at + prewrite_timeout if prewrite_timeout > 0 else None
                    builder_tool_only_deadline = (
                        started_at + builder_tool_only_timeout
                        if builder_tool_only_timeout > 0
                        else None
                    )
                    initial_activity_deadline = (
                        started_at + initial_activity_timeout if initial_activity_timeout > 0 else None
                    )
                    activity_grace = self._prewrite_activity_grace_seconds(stall_timeout)

                    while True:
                        now = time.time()
                        remaining_hard = hard_deadline - now
                        if remaining_hard <= 0:
                            latest_text_is_salvageable = len(latest_stream_text or "") > 0
                            if normalized_node_type == "builder":
                                latest_text_is_salvageable = self._builder_partial_text_is_salvageable(
                                    input_data,
                                    latest_stream_text,
                                )
                            meaningful_age = (
                                now - meaningful_stream_activity_at
                                if meaningful_stream_activity_at > 0
                                else None
                            )
                            pending_write_age = (
                                now - builder_stream_pending_write_at
                                if builder_stream_pending_write_at > 0
                                else None
                            )
                            pending_write_total_age = (
                                now - builder_stream_pending_write_started_at
                                if builder_stream_pending_write_started_at > 0
                                else None
                            )
                            active_hard_grace = max(
                                0.0,
                                min(
                                    float(self._read_int_env("EVERMIND_BUILDER_ACTIVE_HARD_TIMEOUT_GRACE_SEC", 120, 30, 300)),
                                    max(0.0, hard_timeout / 4.0),
                                ),
                            )
                            if normalized_node_type == "builder" and (
                                (
                                    stream_activity_events >= 2
                                    and latest_text_is_salvageable
                                    and stream_has_meaningful_activity
                                    and meaningful_age is not None
                                    and meaningful_age <= active_hard_grace
                                )
                                or (
                                    stream_activity_events >= 2
                                    and builder_stream_pending_write
                                    and pending_write_age is not None
                                    and pending_write_total_age is not None
                                    and pending_write_age <= active_hard_grace
                                    and pending_write_total_age <= pending_write_cap
                                )
                            ):
                                hard_deadline = now + active_hard_grace
                                logger.info(
                                    "builder hard timeout reached but stream is still active (meaningful_age=%s pending_write_age=%s pending_write_total_age=%s grace=%ss cap=%ss) — extending deadline",
                                    None if meaningful_age is None else round(meaningful_age, 2),
                                    None if pending_write_age is None else round(pending_write_age, 2),
                                    None if pending_write_total_age is None else round(pending_write_total_age, 2),
                                    int(active_hard_grace),
                                    round(pending_write_cap, 2),
                                )
                                continue
                            _request_stream_stop()
                            timeout_phase = "hard"
                            timeout_value = hard_timeout
                            raise asyncio.TimeoutError()

                        next_poll = min(1.0, remaining_hard)
                        if initial_activity_deadline is not None:
                            next_poll = min(next_poll, max(0.01, initial_activity_deadline - now))
                        if prewrite_deadline is not None:
                            next_poll = min(next_poll, max(0.01, prewrite_deadline - now))
                        done, _ = await asyncio.wait({call_task}, timeout=next_poll)
                        if call_task in done:
                            return call_task.result()

                        now = time.time()
                        if stream_has_initial_activity:
                            initial_activity_deadline = None
                        elif initial_activity_deadline is not None and now >= initial_activity_deadline:
                            _request_stream_stop()
                            timeout_phase = "initial-activity"
                            timeout_value = initial_activity_timeout
                            raise asyncio.TimeoutError()

                        if (
                            normalized_node_type == "builder"
                            and not builder_has_written_file
                            and builder_tool_only_deadline is not None
                            and now >= builder_tool_only_deadline
                            and not builder_stream_pending_write
                            and not self._builder_partial_text_is_salvageable(input_data, latest_stream_text)
                        ):
                            _request_stream_stop()
                            timeout_phase = "tool-only"
                            timeout_value = builder_tool_only_timeout
                            raise asyncio.TimeoutError()

                        if prewrite_deadline is None or now < prewrite_deadline:
                            continue

                        meaningful_age = None
                        pending_write_age = None
                        pending_write_total_age = None
                        if meaningful_stream_activity_at > 0:
                            meaningful_age = now - meaningful_stream_activity_at
                        if builder_stream_pending_write_at > 0:
                            pending_write_age = now - builder_stream_pending_write_at
                        if builder_stream_pending_write_started_at > 0:
                            pending_write_total_age = now - builder_stream_pending_write_started_at
                        latest_len = len(latest_stream_text or "")
                        active_writer = (
                            builder_has_written_file if normalized_node_type == "builder"
                            else polisher_has_written_file
                        )
                        latest_text_is_salvageable = latest_len > 0
                        if normalized_node_type == "builder":
                            latest_text_is_salvageable = self._builder_partial_text_is_salvageable(
                                input_data,
                                latest_stream_text,
                            )
                        if (
                            not active_writer
                            and latest_text_is_salvageable
                            and stream_has_meaningful_activity
                            and meaningful_age is not None
                            and meaningful_age <= activity_grace
                        ):
                            logger.info(
                                "%s prewrite deadline reached but stream is active (latest_chars=%s meaningful_age=%.2fs grace=%.2fs) — extending prewrite deadline",
                                normalized_node_type or node_type,
                                latest_len,
                                meaningful_age,
                                activity_grace,
                            )
                            prewrite_deadline = now + activity_grace
                            continue

                        if (
                            normalized_node_type == "builder"
                            and not active_writer
                            and builder_stream_pending_write
                            and pending_write_age is not None
                            and pending_write_total_age is not None
                            and pending_write_age <= activity_grace
                            and pending_write_total_age <= pending_write_cap
                        ):
                            logger.info(
                                "builder prewrite deadline reached but a write-like file_ops payload is still streaming (pending_write_age=%.2fs total_age=%.2fs grace=%.2fs cap=%.2fs) — extending prewrite deadline",
                                pending_write_age,
                                pending_write_total_age,
                                activity_grace,
                                pending_write_cap,
                            )
                            prewrite_deadline = now + activity_grace
                            continue

                        _request_stream_stop()
                        timeout_phase = "pre-write"
                        timeout_value = prewrite_timeout
                        raise asyncio.TimeoutError()
                except asyncio.TimeoutError as exc:
                    if normalized_node_type == "builder":
                        if timeout_phase == "initial-activity":
                            kind = "initial-activity"
                            detail = "compatible gateway produced no content or tool activity."
                        elif timeout_phase == "tool-only":
                            kind = "tool-only"
                            detail = (
                                "stream stayed in tool-planning mode without any deliverable HTML "
                                "or write-like file_ops payload."
                            )
                        else:
                            kind = timeout_phase if timeout_phase == "pre-write" else "hard-ceiling"
                            detail = (
                                "no file write produced"
                                if not builder_has_written_file
                                else "API call exceeded node timeout"
                            )
                        raise TimeoutError(
                            f"builder {kind} timeout after {timeout_value or effective}s: {detail}."
                        ) from exc
                    if normalized_node_type == "polisher":
                        if timeout_phase == "initial-activity":
                            kind = "initial-activity"
                            detail = "compatible gateway produced no content or tool activity."
                        else:
                            kind = timeout_phase if timeout_phase == "pre-write" else "hard-ceiling"
                            detail = (
                                "no real file write was produced"
                                if not polisher_has_written_file
                                else "API call exceeded node timeout"
                            )
                        raise TimeoutError(
                            f"polisher {kind} timeout after {timeout_value or effective}s: {detail}."
                        ) from exc
                    node_label = normalized_node_type or str(node_type or "node").strip().lower() or "node"
                    if timeout_phase == "initial-activity":
                        raise TimeoutError(
                            f"{node_label} initial-activity timeout after {timeout_value or effective}s: compatible gateway produced no content or tool activity."
                        ) from exc
                    raise TimeoutError(
                        f"{node_label} hard-ceiling timeout after {timeout_value or effective}s: API call exceeded node timeout."
                    ) from exc

            response = await _await_stream_call(messages, tools)

            await self._publish_partial_output(
                on_progress,
                getattr(response.choices[0].message, "content", "") or "",
                phase="sectioning" if node_type == "planner" else "drafting",
            )

            iteration = 0
            max_iterations = self._max_tool_iterations_for_node(node_type, node=node)
            continuation_count = 0
            usage_totals = self._normalize_usage(getattr(response, "usage", None))
            builder_non_write_streak = 0
            builder_force_text_early = False
            builder_force_reason = ""
            builder_force_threshold = self._builder_force_text_threshold(input_data)
            builder_repair_prompts_sent = 0
            polisher_non_write_streak = 0
            polisher_force_threshold = self._polisher_force_write_threshold(input_data)
            polisher_loop_guard_reason = ""
            polisher_repair_prompts_sent = 0
            polisher_force_write_grace_available = False
            polisher_force_write_grace_used = False
            qa_followup_count = 0
            analyst_followup_count = 0
            review_format_followup_count = 0
            plain_text_force_reason = ""
            length_truncation_tool_count = 0  # Track consecutive finish=length with tool calls

            while iteration < max_iterations:
                iteration += 1
                msg = response.choices[0].message
                msg_content = getattr(msg, "content", "") or ""
                if msg.tool_calls and msg_content:
                    output_text += msg.content
                    await self._publish_partial_output(on_progress, output_text, phase="drafting")
                    await self._publish_partial_output(on_progress, output_text, phase="drafting")

                # Check for tool calls
                if not msg.tool_calls:
                    finish_reason = str(getattr(response.choices[0], "finish_reason", "") or "").lower()
                    candidate_plain_text_output = output_text + (msg_content or "")
                    if self._plain_text_node_output_is_materializable(node_type, candidate_plain_text_output):
                        if msg_content:
                            output_text = candidate_plain_text_output
                            await self._publish_partial_output(on_progress, output_text, phase="drafting")
                        break
                    analyst_followup_reason = self._analyst_browser_followup_reason(
                        node_type,
                        tool_call_stats,
                        tool_results,
                        available_tool_names=available_tool_names,
                    )
                    if analyst_followup_reason and analyst_followup_count < 2 and tools:
                        analyst_followup_count += 1
                        if msg_content:
                            messages.append({"role": "assistant", "content": msg_content})
                        messages.append({
                            "role": "user",
                            "content": self._analyst_browser_followup_message(analyst_followup_reason),
                        })
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "analyst_followup",
                                "message": analyst_followup_reason,
                                "continuation": analyst_followup_count,
                            })
                        response = await _await_stream_call(messages, tools)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        continue
                    followup_reason = self._review_browser_followup_reason(
                        node_type,
                        qa_task_type,
                        browser_action_events,
                        input_data,
                        tool_call_stats=tool_call_stats,
                        available_tool_names=available_tool_names,
                    )
                    if followup_reason and qa_followup_count < 2 and tools:
                        qa_followup_count += 1
                        if msg_content:
                            messages.append({"role": "assistant", "content": msg_content})
                        messages.append({
                            "role": "user",
                            "content": self._review_browser_followup_message(followup_reason, qa_task_type),
                        })
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "qa_followup",
                                "message": followup_reason,
                                "continuation": qa_followup_count,
                            })
                        response = await _await_stream_call(messages, tools)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        continue
                    format_followup_reason = self._review_output_format_followup_reason(
                        node_type,
                        msg_content,
                    )
                    if format_followup_reason and review_format_followup_count < 2:
                        review_format_followup_count += 1
                        if msg_content:
                            messages.append({"role": "assistant", "content": msg_content})
                        messages.append({
                            "role": "user",
                            "content": self._review_output_format_followup_message(format_followup_reason),
                        })
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "review_format_followup",
                                "message": format_followup_reason,
                                "continuation": review_format_followup_count,
                            })
                        response = await _await_stream_call(messages, tools)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        continue
                    if msg_content:
                        output_text += msg_content
                        await self._publish_partial_output(on_progress, output_text, phase="drafting")
                    if self._asset_plan_output_complete(node_type, output_text):
                        if finish_reason == "length":
                            logger.info(
                                "Asset plan JSON looks complete despite finish=length — stopping continuation chain for %s",
                                normalize_node_role(node_type) or node_type,
                            )
                        break
                    if finish_reason == "length" and continuation_count < max_continuations:
                        continuation_count += 1
                        messages.append(self._serialize_assistant_message(msg))
                        messages.append({
                            "role": "user",
                            "content": (
                                "Continue from exactly where you stopped. "
                                "Do not repeat previous content. "
                                "Keep the same format and finish the full result."
                            ),
                        })
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "continuing",
                                "reason": "length_truncated",
                                "continuation": continuation_count,
                            })
                        response = await _await_stream_call(messages, tools)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        continue
                    break

                # ── Guard: detect finish=length with tool calls (truncated tool loop) ──
                tc_finish = str(getattr(response.choices[0], "finish_reason", "") or "").lower()
                if tc_finish == "length":
                    length_truncation_tool_count += 1
                    logger.warning(
                        "finish=length with tool_calls detected: node=%s model=%s iteration=%s truncation_count=%s",
                        normalized_node_type or node_type,
                        model_name,
                        iteration,
                        length_truncation_tool_count,
                    )
                    if length_truncation_tool_count >= 2:
                        error_msg = (
                            f"Model hit token limit during tool calls {length_truncation_tool_count} times. "
                            f"Breaking truncated tool loop for node {normalized_node_type or node_type}."
                        )
                        logger.error(error_msg)
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "length_truncation_break",
                                "message": error_msg,
                                "truncation_count": length_truncation_tool_count,
                            })
                        # P0 FIX: Salvage any accumulated output before breaking.
                        # If we have partial HTML from truncated tool calls or stream text,
                        # write it to disk so it's not completely lost.
                        salvage_text = (output_text or "").strip()
                        if not salvage_text and latest_stream_text:
                            salvage_text = latest_stream_text.strip()
                        if salvage_text and len(salvage_text) > 200:
                            logger.info(
                                "Salvaging %s chars of partial output before truncation break for %s",
                                len(salvage_text),
                                normalized_node_type or node_type,
                            )
                            output_text = salvage_text
                        break
                else:
                    length_truncation_tool_count = 0  # Reset on non-truncated response

                # Process tool calls
                messages.append(self._serialize_assistant_message(msg))
                processed_calls = 0
                pending_repair_prompts: List[str] = []
                for tc in msg.tool_calls:
                    if isinstance(tc, dict):
                        tc_id = tc.get("id", "")
                        tc_type = tc.get("type", "function")
                        fn_payload = tc.get("function") or {}
                        fn_name = fn_payload.get("name", "")
                        fn_args = fn_payload.get("arguments", "{}")
                    else:
                        tc_id = getattr(tc, "id", "")
                        tc_type = getattr(tc, "type", "function")
                        fn_payload = getattr(tc, "function", None)
                        fn_name = getattr(fn_payload, "name", "") if fn_payload else ""
                        fn_args = getattr(fn_payload, "arguments", "{}") if fn_payload else "{}"
                    parsed_args = self._safe_json_object(fn_args)

                    if tc_type != "function" or not fn_name:
                        logger.warning("Skipping unsupported tool call payload: type=%s id=%s", tc_type, tc_id)
                        continue

                    _tool_call_start_ts = time.time()

                    if on_progress:
                        await self._emit_noncritical_progress(
                            on_progress,
                            self._tool_execution_progress_payload(fn_name, parsed_args),
                        )
                    if fn_name in {"browser", "browser_use"} and self._desktop_qa_browser_suppressed(node_type, input_data):
                        result = {
                            "success": False,
                            "data": {},
                            "error": (
                                "Desktop QA evidence already exists for this reviewer/tester pass. "
                                "Use the provided Evermind internal QA record instead of opening browser/browser_use."
                            ),
                            "artifacts": [],
                        }
                    elif fn_name == "browser" and self._should_block_browser_call(node_type, tool_call_stats, node=node):
                        limit = (
                            self._polisher_browser_call_limit(node=node)
                            if normalized_node_type == "polisher"
                            else self._analyst_browser_call_limit(node=node)
                        )
                        node_label = "Polisher" if normalized_node_type == "polisher" else "Analyst"
                        guidance = (
                            "Stop browsing and write concrete file improvements now."
                            if normalized_node_type == "polisher"
                            else "Skip additional browsing and summarize from collected insights."
                        )
                        result = {
                            "success": False,
                            "data": {},
                            "error": (
                                f"{node_label} browser call limit reached ({limit}). "
                                f"{guidance}"
                            ),
                            "artifacts": [],
                        }
                    elif fn_name == "file_ops" and normalized_node_type in ("analyst", "scribe", "uidesign"):
                        # Guard: analyst/scribe/uidesign must NOT write code files.
                        # If the model tries file_ops write, return an error forcing it
                        # to produce a text report instead. This prevents the 100s+
                        # token-truncated HTML generation observed in production.
                        parsed_args = self._safe_json_object(fn_args)
                        action = str(parsed_args.get("action", "")).strip().lower()
                        if action in ("write", "edit"):
                            result = {
                                "success": False,
                                "data": {},
                                "error": self._plain_text_node_write_guard_error(normalized_node_type),
                                "artifacts": [],
                            }
                            plain_text_force_reason = plain_text_force_reason or "blocked_file_write"
                            logger.warning(
                                "Blocked file_ops write/edit for %s node — must produce text report only",
                                normalized_node_type,
                            )
                        else:
                            result = await self._run_plugin(
                                fn_name,
                                fn_args,
                                plugins or [],
                                node_type=node_type,
                                node=node,
                            )
                    else:
                        if fn_name == "file_ops":
                            self._builder_capture_prewrite_snapshot(
                                node=node,
                                input_data=input_data,
                                tool_action=str(parsed_args.get("action", "")).strip().lower(),
                                parsed_args=parsed_args,
                                snapshot_cache=builder_support_snapshots,
                            )
                        result = await self._run_plugin(
                            fn_name,
                            fn_args,
                            plugins or [],
                            node_type=node_type,
                            node=node,
                        )
                    tool_action = (
                        self._infer_file_ops_action(fn_args, result)
                        if fn_name == "file_ops"
                        else str(parsed_args.get("action", "")).strip().lower()
                    )
                    if fn_name == "file_ops":
                        result = self._builder_guard_support_lane_write_result(
                            node=node,
                            tool_action=tool_action,
                            result=result,
                            snapshot_cache=builder_support_snapshots,
                        )
                        result = self._builder_guard_html_write_result(
                            node=node,
                            input_data=input_data,
                            tool_action=tool_action,
                            result=result,
                            snapshot_cache=builder_support_snapshots,
                        )
                    # V4.2: Enrich tool result for report trace table
                    if isinstance(result, dict):
                        result.setdefault("tool", fn_name)
                        result.setdefault("started_at", _tool_call_start_ts)
                        result.setdefault("duration_ms", int((time.time() - _tool_call_start_ts) * 1000) if _tool_call_start_ts else 0)
                        # V4.2 FIX (Codex #3): inject args for ALL tools, not just file_ops
                        result.setdefault("args", parsed_args)
                    tool_results.append(result)
                    tool_call_stats[fn_name] = tool_call_stats.get(fn_name, 0) + 1
                    if fn_name == "browser":
                        browser_data = result.get("data", {}) if isinstance(result, dict) else {}
                        if not isinstance(browser_data, dict):
                            browser_data = {}
                        browser_event = {
                            "action": tool_action or parsed_args.get("action") or "unknown",
                            "subaction": browser_data.get("subaction"),
                            "intent": browser_data.get("intent"),
                            "ok": bool(result.get("success", False)) if isinstance(result, dict) else False,
                            "url": browser_data.get("url"),
                            "target": browser_data.get("target"),
                            "observation": browser_data.get("observation"),
                            "snapshot_refs_preview": browser_data.get("snapshot_refs_preview", []),
                            "snapshot_ref_count": browser_data.get("snapshot_ref_count", 0),
                            "state_hash": browser_data.get("state_hash"),
                            "previous_state_hash": browser_data.get("previous_state_hash"),
                            "state_changed": bool(browser_data.get("state_changed", False)),
                            "scroll_y": browser_data.get("scroll_y", 0),
                            "viewport_height": browser_data.get("viewport_height", 0),
                            "page_height": browser_data.get("page_height", 0),
                            "at_page_top": bool(browser_data.get("at_page_top", False)),
                            "at_page_bottom": bool(browser_data.get("at_page_bottom", False)),
                            "keys_count": browser_data.get("keys_count", 0),
                            "console_error_count": browser_data.get("console_error_count", 0),
                            "page_error_count": browser_data.get("page_error_count", 0),
                            "failed_request_count": browser_data.get("failed_request_count", 0),
                            "recent_console_errors": browser_data.get("recent_console_errors", []),
                            "recent_page_errors": browser_data.get("recent_page_errors", []),
                            "browser_mode": browser_data.get("browser_mode"),
                            "requested_mode": browser_data.get("requested_mode"),
                            "launch_note": browser_data.get("launch_note"),
                            "error": (result.get("error") if isinstance(result, dict) else "") or "",
                        }
                        browser_action_events.append(browser_event)
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "browser_action",
                                "plugin": "browser",
                                **browser_event,
                            })
                    elif fn_name == "browser_use":
                        browser_use_events = self._browser_use_action_events(result)
                        for browser_event in browser_use_events:
                            browser_action_events.append(browser_event)
                            if on_progress:
                                await self._emit_noncritical_progress(on_progress, {
                                    "stage": "browser_action",
                                    "plugin": "browser_use",
                                    **browser_event,
                                })
                    repair_prompt = None
                    wrote_file = False
                    if fn_name == "file_ops":
                        wrote_file = self._tool_result_has_write(result)
                    normalized_node_type = normalize_node_role(node_type)
                    if normalized_node_type == "builder":
                        if wrote_file:
                            builder_has_written_file = True
                            builder_non_write_streak = 0
                            if on_progress:
                                write_path = ""
                                if isinstance(result, dict):
                                    data = result.get("data")
                                    if isinstance(data, dict):
                                        write_path = str(data.get("path") or "").strip()
                                metrics = self._written_file_code_metrics(write_path)
                                await self._emit_noncritical_progress(on_progress, {
                                    "stage": "builder_write",
                                    "plugin": fn_name,
                                    "path": write_path,
                                    **metrics,
                                })
                        else:
                            # Count all non-write tool turns so malformed file_ops args
                            # cannot bypass the loop guard.
                            builder_non_write_streak += 1
                    else:
                        if normalized_node_type == "polisher":
                            if wrote_file:
                                # Only count as a real deliverable write if it targets
                                # a .html/.css/.js file inside the output directory.
                                write_path_str = ""
                                if isinstance(result, dict):
                                    data = result.get("data")
                                    if isinstance(data, dict):
                                        write_path_str = str(data.get("path") or "").strip()
                                is_deliverable = False
                                if write_path_str:
                                    try:
                                        from pathlib import Path as _P
                                        wp = _P(write_path_str).resolve()
                                        od = _P(self._current_output_dir()).resolve()
                                        if str(wp).startswith(str(od)):
                                            deliverable_exts = {".html", ".htm", ".css", ".js", ".mjs"}
                                            if wp.suffix.lower() in deliverable_exts:
                                                is_deliverable = True
                                    except Exception:
                                        pass
                                if is_deliverable:
                                    polisher_has_written_file = True
                                    polisher_non_write_streak = 0
                                else:
                                    # Non-deliverable write (e.g. list, read, tmp file) —
                                    # do not disable the prewrite timeout.
                                    polisher_non_write_streak += 1
                            else:
                                polisher_non_write_streak += 1
                        if wrote_file and on_progress:
                            write_path = ""
                            if isinstance(result, dict):
                                data = result.get("data")
                                if isinstance(data, dict):
                                    write_path = str(data.get("path") or "").strip()
                            metrics = self._written_file_code_metrics(write_path)
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "artifact_write",
                                "plugin": fn_name,
                                "path": write_path,
                                "agent": normalized_node_type or node_type,
                                "writer": normalized_node_type or node_type,
                                **metrics,
                            })
                    if fn_name == "file_ops" and normalized_node_type == "builder" and builder_repair_prompts_sent < 2:
                        repair_prompt = self._builder_tool_repair_prompt(input_data, tool_action, result)
                        if not repair_prompt and not wrote_file:
                            repair_prompt = self._builder_non_write_followup_prompt(
                                input_data,
                                tool_action,
                                result,
                                builder_non_write_streak,
                            )
                    elif normalized_node_type == "polisher" and polisher_repair_prompts_sent < 2 and not wrote_file:
                        repair_prompt = self._polisher_non_write_followup_prompt(
                            fn_name,
                            tool_action,
                            result,
                            polisher_non_write_streak,
                        )
                    # Truncate tool output to prevent token overflow (Kimi 262K limit)
                    result_str = json.dumps(result)
                    _tool_char_limit = MAX_ANALYST_TOOL_RESULT_CHARS if normalized_node_type == "analyst" else MAX_TOOL_RESULT_CHARS
                    if len(result_str) > _tool_char_limit:
                        result_str = result_str[:_tool_char_limit] + '... [TRUNCATED]'
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": result_str})
                    if repair_prompt and repair_prompt not in pending_repair_prompts:
                        pending_repair_prompts.append(repair_prompt)
                        if normalized_node_type == "builder":
                            builder_repair_prompts_sent += 1
                        elif normalized_node_type == "polisher":
                            polisher_repair_prompts_sent += 1
                            if fn_name == "file_ops" and not polisher_force_write_grace_used:
                                polisher_force_write_grace_available = True
                    processed_calls += 1

                if msg.tool_calls and processed_calls == 0:
                    raise ValueError("No supported function tool calls were produced by model response")

                if pending_repair_prompts:
                    messages.append({"role": "user", "content": "\n\n".join(pending_repair_prompts)})

                if (
                    normalized_node_type in {"analyst", "scribe", "uidesign"}
                    and plain_text_force_reason == "blocked_file_write"
                ):
                    logger.info(
                        "Plain-text node blocked from file_ops write — ending tool loop and switching to final no-tool synthesis (%s)",
                        normalized_node_type,
                    )
                    break

                if (
                    normalize_node_role(node_type) == "builder"
                    and builder_non_write_streak >= builder_force_threshold
                    and not any(self._tool_result_has_write(tr) for tr in tool_results)
                ):
                    builder_force_text_early = True
                    builder_force_reason = "tool_research_loop"
                    logger.info(
                        "Builder loop guard triggered: non-write tool streak=%s (threshold=%s)",
                        builder_non_write_streak,
                        builder_force_threshold,
                    )
                    if on_progress:
                        await self._emit_noncritical_progress(on_progress, {
                            "stage": "builder_loop_guard",
                            "streak": builder_non_write_streak,
                            "threshold": builder_force_threshold,
                            "reason": builder_force_reason,
                        })
                    break

                if (
                    normalized_node_type == "polisher"
                    and not polisher_has_written_file
                    and polisher_non_write_streak >= polisher_force_threshold
                ):
                    if polisher_force_write_grace_available and not polisher_force_write_grace_used:
                        polisher_force_write_grace_available = False
                        polisher_force_write_grace_used = True
                        logger.info(
                            "Polisher force-write grace granted: non-write tool streak=%s (threshold=%s)",
                            polisher_non_write_streak,
                            polisher_force_threshold,
                        )
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "polisher_force_write_warning",
                                "streak": polisher_non_write_streak,
                                "threshold": polisher_force_threshold,
                                "message": "Polisher inspection limit reached; one final response is allowed to obey the forced-write prompt.",
                            })
                        response = await _await_stream_call(messages, tools)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        continue
                    polisher_loop_guard_reason = (
                        "polisher loop guard triggered after "
                        f"{polisher_non_write_streak} non-write tool iterations without any file write."
                    )
                    logger.warning(polisher_loop_guard_reason)
                    if on_progress:
                        await self._emit_noncritical_progress(on_progress, {
                            "stage": "polisher_loop_guard",
                            "streak": polisher_non_write_streak,
                            "threshold": polisher_force_threshold,
                            "reason": polisher_loop_guard_reason,
                        })
                    break

                if on_progress:
                    await self._emit_noncritical_progress(
                        on_progress,
                        {"stage": "continuing", "iteration": iteration},
                    )
                response = await _await_stream_call(messages, tools)
                usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))

            if polisher_loop_guard_reason:
                return self._attach_browser_action_events({
                    "success": False,
                    "output": output_text,
                    "error": polisher_loop_guard_reason,
                    "model": model_name,
                    "tool_results": tool_results,
                    "mode": "openai_compatible",
                    "usage": usage_totals,
                    "tool_call_stats": dict(tool_call_stats),
                    "qa_browser_use_available": qa_browser_use_available,
                }, browser_action_events)

            # ── Forced final text-only call for builder / plain-text handoff nodes ──
            force_text_reason = ""
            if self._builder_needs_forced_text(node_type, output_text, tool_results):
                if builder_force_text_early:
                    force_text_reason = builder_force_reason or "tool_research_loop"
                elif iteration >= max_iterations:
                    force_text_reason = "tool_iterations_exhausted"
                else:
                    force_text_reason = "no_html_or_file_output"

            if force_text_reason:
                logger.info("Builder missing HTML/file output — forcing final text-only call (%s)", force_text_reason)
                if on_progress:
                    await self._emit_noncritical_progress(
                        on_progress,
                        self._forcing_text_progress_payload(node_type, force_text_reason),
                    )
                forced_messages = self._builder_forced_text_messages(
                    system_prompt,
                    input_data,
                    tool_results,
                    output_text,
                    force_text_reason,
                )
                try:
                    final_resp = await _await_stream_call(forced_messages, [])  # No tools
                    final_msg = final_resp.choices[0].message
                    if final_msg.content:
                        output_text += final_msg.content
                        await self._publish_partial_output(on_progress, output_text, phase="finalizing")
                    usage_totals = self._merge_usage(usage_totals, getattr(final_resp, "usage", None))
                    logger.info(f"Forced text output: {len(final_msg.content or '')} chars")
                except Exception as e:
                    logger.warning(f"Forced text-only call failed: {_sanitize_error(str(e))}")

            plain_text_final_reason = ""
            if self._plain_text_node_needs_forced_output(node_type, output_text, tool_results):
                if plain_text_force_reason:
                    plain_text_final_reason = plain_text_force_reason
                elif iteration >= max_iterations:
                    plain_text_final_reason = "tool_iterations_exhausted"
                else:
                    plain_text_final_reason = "missing_final_handoff"

            if plain_text_final_reason:
                logger.info(
                    "Plain-text handoff node missing final output — forcing no-tool synthesis (%s)",
                    plain_text_final_reason,
                )
                if on_progress:
                    await self._emit_noncritical_progress(
                        on_progress,
                        self._forcing_text_progress_payload(node_type, plain_text_final_reason),
                    )
                forced_messages = self._plain_text_node_forced_messages(
                    system_prompt,
                    input_data,
                    tool_results,
                    output_text,
                    node_type,
                    plain_text_final_reason,
                )
                forced_timeout = self._plain_text_final_timeout_for_node(
                    node_type,
                    plain_text_final_reason,
                )
                try:
                    final_resp = await _await_stream_call(
                        forced_messages,
                        [],
                        timeout_override=forced_timeout or None,
                    )
                    final_msg = final_resp.choices[0].message
                    final_content = str(final_msg.content or "").strip()
                    if final_content:
                        if output_text and not output_text.endswith("\n"):
                            output_text += "\n"
                        output_text += final_content
                        await self._publish_partial_output(on_progress, output_text, phase="finalizing")
                    usage_totals = self._merge_usage(usage_totals, getattr(final_resp, "usage", None))
                except Exception as e:
                    logger.warning("Forced plain-text handoff failed: %s", _sanitize_error(str(e)))

            reviewer_force_reason = ""
            if self._reviewer_needs_forced_verdict(node_type, output_text, tool_results):
                reviewer_force_reason = (
                    self._review_output_format_followup_reason(node_type, output_text)
                    or "missing_reviewer_verdict_json"
                )

            if reviewer_force_reason:
                logger.info(
                    "Reviewer output missing strict verdict JSON — forcing final no-tool verdict synthesis (%s)",
                    reviewer_force_reason,
                )
                if on_progress:
                    await self._emit_noncritical_progress(
                        on_progress,
                        self._forcing_text_progress_payload(node_type, reviewer_force_reason),
                    )
                forced_messages = self._reviewer_forced_verdict_messages(
                    system_prompt,
                    input_data,
                    tool_results,
                    output_text,
                    reviewer_force_reason,
                )
                try:
                    final_resp = await _await_stream_call(forced_messages, [])
                    final_msg = final_resp.choices[0].message
                    final_content = str(final_msg.content or "").strip()
                    if (
                        final_content
                        and self._review_output_format_followup_reason(node_type, final_content) is None
                    ):
                        output_text = final_content
                        await self._publish_partial_output(on_progress, output_text, phase="finalizing")
                    usage_totals = self._merge_usage(usage_totals, getattr(final_resp, "usage", None))
                except Exception as e:
                    logger.warning("Forced reviewer verdict synthesis failed: %s", _sanitize_error(str(e)))

            if normalized_node_type == "builder":
                tool_result_text = self._builder_tool_result_salvage_text(tool_results, input_data)
                disk_result_text = self._builder_disk_salvage_text(
                    input_data,
                    incremental_text=output_text,
                )
                combined_salvage_text = "\n\n".join(
                    part for part in (tool_result_text, disk_result_text) if str(part or "").strip()
                )
                preferred_output = self._select_builder_salvage_text(
                    "",
                    output_text,
                    tool_result_text=combined_salvage_text,
                )
                if (
                    preferred_output
                    and preferred_output != output_text
                    and self._builder_partial_text_is_salvageable(input_data, preferred_output)
                ):
                    output_text = preferred_output
                    await self._publish_partial_output(on_progress, output_text, phase="finalizing")

            # ── P0 FIX B: Auto-save HTML from text output if builder never used file_ops ──
            # Kimi K2.5 sometimes outputs full HTML as markdown code blocks in content text
            # instead of calling file_ops. When this happens, output_text has 37k+ chars of
            # complete HTML that needs to be saved to disk, otherwise quality gate sees empty body.
            if (
                normalized_node_type == "builder"
                and not builder_has_written_file
                and output_text
                and len(output_text) > 120
            ):
                async def _request_builder_continuation(cont_messages):
                    return await _await_stream_call(cont_messages, [])

                output_text, continuation_count, cont_resp = await self._attempt_builder_game_text_continuation(
                    output_text=output_text,
                    input_data=input_data,
                    system_prompt=system_prompt,
                    continuation_count=continuation_count,
                    max_continuations=max_continuations,
                    request_continuation=_request_builder_continuation,
                    on_progress=on_progress,
                )
                if cont_resp is not None:
                    usage_totals = self._merge_usage(usage_totals, getattr(cont_resp, "usage", None))

                saved_paths = await self._auto_save_builder_text_output(
                    output_text=output_text,
                    input_data=input_data,
                    node=node,
                    tool_results=tool_results,
                    tool_call_stats=tool_call_stats,
                    on_progress=on_progress,
                )
                if saved_paths:
                    builder_has_written_file = True
                elif not self._builder_text_output_has_persistable_html(output_text, input_data):
                    failure_msg = self._builder_non_deliverable_output_reason(output_text, input_data)
                    logger.warning(failure_msg)
                    return self._attach_browser_action_events({
                        "success": False,
                        "output": output_text,
                        "error": failure_msg,
                        "model": getattr(response, "model", model_name),
                        "tool_results": tool_results,
                        "mode": "openai_compatible",
                        "usage": usage_totals,
                        "tool_call_stats": dict(tool_call_stats),
                        "qa_browser_use_available": qa_browser_use_available,
                    }, browser_action_events)

            return self._attach_browser_action_events({
                "success": True, "output": output_text,
                "model": getattr(response, "model", model_name),
                "tool_results": tool_results, "mode": "openai_compatible",
                "usage": usage_totals,
                "tool_call_stats": tool_call_stats,
                "qa_browser_use_available": qa_browser_use_available,
            }, browser_action_events)
        except TimeoutError as e:
            fallback = await _force_builder_text_timeout_fallback(str(e))
            if fallback is not None:
                return fallback
            if on_progress:
                await self._emit_noncritical_progress(
                    on_progress,
                    {"stage": "stream_stalled", "reason": str(e)},
                )
            return self._attach_browser_action_events(
                {"success": False, "output": "", "error": _sanitize_error(str(e))},
                locals().get("browser_action_events"),
            )
        except Exception as e:
            err = _sanitize_error(str(e))
            # V4.3 connection self-healing: evict cached client on transport-level
            # failures so the orchestrator's retry creates a fresh TCP+TLS session.
            _err_lower = err.lower()
            _is_connection_error = any(kw in _err_lower for kw in (
                "connectionerror", "connecterror", "connection reset",
                "connection refused", "transport", "reset by peer",
                "broken pipe", "unexpectedeof", "ssl handshake",
                "socket error", "networkerror",
            ))
            if _is_connection_error:
                self._invalidate_openai_client(
                    api_key=api_key,
                    base_url=self._resolved_api_base_for_model_info(model_info),
                    reason=err[:200],
                )
            if normalized_node_type == "builder" and not builder_has_written_file and "timeout" in _err_lower:
                fallback = await _force_builder_text_timeout_fallback(err)
                if fallback is not None:
                    return fallback
            if on_progress and ("timed out" in _err_lower or "timeout" in _err_lower):
                await self._emit_noncritical_progress(
                    on_progress,
                    {"stage": "stream_stalled", "reason": err},
                )
            return self._attach_browser_action_events(
                {"success": False, "output": "", "error": err},
                locals().get("browser_action_events"),
            )

    async def _execute_openai_compatible_chat(self, node, input_data, model_info, on_progress) -> Dict:
        from openai import OpenAI

        node_type = node.get("type", "builder")
        system_prompt = self._compose_system_prompt(node, input_data=input_data)
        model_name = model_info["litellm_id"].replace("openai/", "")
        max_tokens = self._max_tokens_for_node(
            node_type,
            retry_attempt=int(node.get("retry_attempt", 0)),
            node=node,
        )
        timeout_sec = self._effective_timeout_for_node(node_type, input_data, node=node)
        direct_multifile = (
            normalize_node_role(node_type) == "builder"
            and str(node.get("builder_delivery_mode") or "").strip().lower() == "direct_multifile"
        )
        if direct_multifile:
            max_tokens, timeout_sec = self._builder_direct_multifile_budget(
                input_data,
                max_tokens=max_tokens,
                timeout_sec=timeout_sec,
            )

        api_key = self._resolved_api_key_for_model_info(model_info)
        if not api_key:
            return {"success": False, "output": "", "error": f"API key not configured for {model_info.get('provider')}"}

        if on_progress:
            await self._emit_noncritical_progress(
                on_progress,
                {"stage": "calling_ai", "model": model_name, "tools_count": 0, "mode": "openai_compatible_chat"},
            )

        stall_timeout = self._effective_stream_stall_timeout(node_type, input_data, node=node)
        latest_stream_text = ""
        output_parts: List[str] = []
        stream_has_initial_activity = False
        custom_gateway = bool(self._custom_compatible_gateway_base(model_info))
        loop = asyncio.get_running_loop()
        logger.info(
            "openai_compatible_chat request profile: node=%s model=%s system_chars=%s user_chars=%s assigned_targets=%s direct_multifile=%s",
            normalize_node_role(node_type) or node_type,
            model_name,
            len(system_prompt or ""),
            len(str(input_data or "")),
            len(self._builder_assigned_html_targets(input_data)) if normalize_node_role(node_type) == "builder" else 0,
            direct_multifile,
        )

        try:
            client = self._get_or_create_openai_client(
                api_key=api_key,
                base_url=self._resolved_api_base_for_model_info(model_info),
                extra_headers=model_info.get("extra_headers", {}),
                timeout=timeout_sec,
            )
            kwargs_base: Dict[str, Any] = {
                "model": model_name,
                "max_tokens": max_tokens,
            }
            # V4.3 PERF: Node-specific temperature (was fixed 0.7 for all)
            _node_temp = self._temperature_for_node(node_type, model_name)
            if _node_temp is not None:
                kwargs_base["temperature"] = _node_temp
            if model_info.get("provider") == "kimi":
                if os.getenv("EVERMIND_KIMI_THINKING", "disabled").lower() != "enabled":
                    kwargs_base["extra_body"] = {"thinking": {"type": "disabled"}}
            # V4.3: thinking_depth → reasoning_effort for compatible models
            _thinking_depth = self._configured_thinking_depth()
            if _thinking_depth == "fast" and "reasoning_effort" not in kwargs_base:
                if model_info.get("supports_reasoning_effort", False):
                    kwargs_base["reasoning_effort"] = "low"

            def _call_chat_streaming(current_messages, cancel_event: Optional[threading.Event] = None):
                """Make API call with streaming to detect stalls early."""
                nonlocal latest_stream_text, stream_has_initial_activity
                latest_stream_text = ""
                stream_has_initial_activity = False
                kwargs = dict(kwargs_base)
                kwargs["messages"] = current_messages
                kwargs["stream"] = True
                # F4-1: Request usage data in streaming mode for accurate token monitoring
                kwargs["stream_options"] = {"include_usage": True}
                # V4.3.1: Transport timeout uses 3x stall for first-chunk grace
                kwargs["timeout"] = stall_timeout * 3
                stream = client.chat.completions.create(**kwargs)
                content_parts = []
                usage_data = None
                finish_reason = None
                last_chunk_time = time.time()
                last_preview_emit = 0.0
                stream_started_at = last_chunk_time
                first_chunk_at: Optional[float] = None
                first_content_at: Optional[float] = None
                chunk_count = 0
                try:
                    for chunk in stream:
                        if cancel_event is not None and cancel_event.is_set():
                            raise asyncio.CancelledError()
                        now = time.time()
                        # V4.3.1: First-chunk grace for chat streaming path too (3x)
                        _eff_stall = stall_timeout * 3 if first_content_at is None else stall_timeout
                        if now - last_chunk_time > _eff_stall:
                            raise TimeoutError(f"Chat stream stalled: no chunk for {_eff_stall:.0f}s")
                        if first_chunk_at is None:
                            first_chunk_at = now
                        chunk_count += 1
                        last_chunk_time = now
                        if not chunk.choices:
                            if hasattr(chunk, "usage") and chunk.usage:
                                usage_data = chunk.usage
                            continue
                        delta = chunk.choices[0].delta
                        if delta.content:
                            if first_content_at is None:
                                first_content_at = now
                            stream_has_initial_activity = True
                            content_parts.append(delta.content)
                            latest_stream_text = "".join(content_parts)
                            if on_progress and (now - last_preview_emit >= 0.75):
                                event = self._build_partial_output_event(latest_stream_text, phase="streaming")
                                if event:
                                    try:
                                        asyncio.run_coroutine_threadsafe(on_progress(event), loop)
                                    except RuntimeError:
                                        pass
                                last_preview_emit = now
                        if chunk.choices[0].finish_reason:
                            finish_reason = chunk.choices[0].finish_reason
                    if cancel_event is not None and cancel_event.is_set():
                        raise asyncio.CancelledError()
                finally:
                    _close_stream_quietly(stream)
                logger.info(
                    "openai_compatible_chat stream stats: node=%s model=%s chunks=%s first_chunk=%s first_content=%s finish=%s content_chars=%s total_stream=%s",
                    normalize_node_role(node_type) or node_type,
                    model_name,
                    chunk_count,
                    self._format_latency_for_log(
                        None if first_chunk_at is None else first_chunk_at - stream_started_at
                    ),
                    self._format_latency_for_log(
                        None if first_content_at is None else first_content_at - stream_started_at
                    ),
                    finish_reason or "stop",
                    len("".join(content_parts)),
                    self._format_latency_for_log(time.time() - stream_started_at),
                )

                # Broadcast stream stats to frontend
                if on_progress:
                    _total_sec = time.time() - stream_started_at
                    _fc_sec = (first_content_at - stream_started_at) if first_content_at else None
                    _cps = round(len("".join(content_parts)) / max(_total_sec, 0.1), 1)
                    try:
                        asyncio.run_coroutine_threadsafe(
                            on_progress({
                                "stage": "stream_stats",
                                "model": model_name,
                                "chunks": chunk_count,
                                "first_content_sec": round(_fc_sec, 2) if _fc_sec else None,
                                "total_stream_sec": round(_total_sec, 2),
                                "content_chars": len("".join(content_parts)),
                                "chars_per_sec": _cps,
                                "tool_calls": 0,
                            }),
                            loop,
                        )
                    except Exception:
                        pass

                from types import SimpleNamespace
                message = SimpleNamespace(
                    content="".join(content_parts) if content_parts else None,
                    role="assistant",
                )
                choice = SimpleNamespace(
                    message=message,
                    finish_reason=finish_reason or "stop",
                )
                return SimpleNamespace(choices=[choice], usage=usage_data)

            async def _call_chat(current_messages):
                cancel_event = threading.Event()
                call_task = asyncio.create_task(asyncio.to_thread(_call_chat_streaming, current_messages, cancel_event))

                def _request_stream_stop() -> None:
                    cancel_event.set()
                    call_task.cancel()

                initial_activity_timeout = (
                    self._gateway_initial_activity_timeout_for_node(node_type, input_data)
                    if custom_gateway
                    else 0
                )
                started_at = time.time()
                hard_deadline = started_at + timeout_sec
                initial_activity_deadline = (
                    started_at + initial_activity_timeout if initial_activity_timeout > 0 else None
                )
                while True:
                    now = time.time()
                    remaining_hard = hard_deadline - now
                    if remaining_hard <= 0:
                        _request_stream_stop()
                        node_label = normalize_node_role(node_type) or str(node_type or "node").strip().lower() or "node"
                        raise TimeoutError(
                            f"{node_label} hard-ceiling timeout after {timeout_sec}s: API call exceeded node timeout."
                        )
                    next_poll = min(1.0, remaining_hard)
                    if initial_activity_deadline is not None:
                        next_poll = min(next_poll, max(0.01, initial_activity_deadline - now))
                    done, _ = await asyncio.wait({call_task}, timeout=next_poll)
                    if call_task in done:
                        return call_task.result()
                    now = time.time()
                    if stream_has_initial_activity:
                        initial_activity_deadline = None
                        continue
                    if initial_activity_deadline is not None and now >= initial_activity_deadline:
                        _request_stream_stop()
                        node_label = normalize_node_role(node_type) or str(node_type or "node").strip().lower() or "node"
                        raise TimeoutError(
                            f"{node_label} initial-activity timeout after {initial_activity_timeout}s: compatible gateway produced no content."
                        )

            logger.info(
                "openai_compatible_chat: starting API call model=%s timeout=%ss stall=%ss multifile=%s",
                model_name, timeout_sec, stall_timeout, direct_multifile,
            )
            response = await _call_chat(
                self._builder_direct_multifile_initial_messages(system_prompt, input_data)
                if direct_multifile
                else [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": input_data},
                ]
            )
            usage = self._normalize_usage(getattr(response, "usage", None))
            content = response.choices[0].message.content or ""
            if content:
                output_parts.append(content)
                returned_targets = self._builder_returned_html_targets(content)
                completed_targets = self._builder_completed_html_targets(content)
                logger.info(
                    "builder direct_multifile batch ready: node=%s model=%s batch_index=%s returned_targets=%s completed_targets=%s finish=%s chars=%s",
                    normalize_node_role(node_type) or node_type,
                    model_name,
                    len(output_parts),
                    returned_targets[:12],
                    completed_targets[:12],
                    self._response_finish_reason(response) or "stop",
                    len(content),
                )
                if on_progress:
                    await self._emit_noncritical_progress(on_progress, {
                        "stage": "builder_multifile_batch_ready",
                        "batch_index": len(output_parts),
                        "returned_targets": returned_targets[:12],
                        "completed_targets": completed_targets[:12],
                        "finish_reason": self._response_finish_reason(response),
                        "content": content,
                    })
                await self._publish_partial_output(on_progress, content, phase="drafting")

            continuation_limit = self._builder_direct_multifile_continuation_limit(input_data) if direct_multifile else 0
            continuation_count = 0
            while direct_multifile and continuation_count < continuation_limit:
                combined_output = "\n\n".join(part for part in output_parts if str(part).strip())
                remaining_targets = self._builder_missing_html_targets(input_data, combined_output)
                if not remaining_targets:
                    break
                previous_remaining = list(remaining_targets)
                next_batch = remaining_targets[: self._builder_direct_multifile_batch_size(input_data)]
                logger.info(
                    "builder direct_multifile continuation request: node=%s model=%s continuation=%s next_batch=%s remaining=%s prior_finish=%s",
                    normalize_node_role(node_type) or node_type,
                    model_name,
                    continuation_count + 1,
                    next_batch[:12],
                    len(remaining_targets),
                    self._response_finish_reason(response) or "stop",
                )
                if on_progress:
                    await self._emit_noncritical_progress(on_progress, {
                        "stage": "builder_multifile_continue",
                        "continuation": continuation_count + 1,
                        "next_batch": next_batch[:12],
                        "remaining_targets": remaining_targets[:12],
                        "finish_reason": self._response_finish_reason(response),
                    })
                response = await _call_chat(
                    self._builder_direct_multifile_continuation_messages(
                        system_prompt,
                        input_data,
                        combined_output,
                        remaining_targets,
                    )
                )
                usage = self._merge_usage(usage, getattr(response, "usage", None))
                continuation_count += 1
                continuation_content = response.choices[0].message.content or ""
                if not continuation_content.strip():
                    break
                output_parts.append(continuation_content)
                returned_targets = self._builder_returned_html_targets(continuation_content)
                completed_targets = self._builder_completed_html_targets(continuation_content)
                logger.info(
                    "builder direct_multifile batch ready: node=%s model=%s batch_index=%s returned_targets=%s completed_targets=%s finish=%s chars=%s",
                    normalize_node_role(node_type) or node_type,
                    model_name,
                    len(output_parts),
                    returned_targets[:12],
                    completed_targets[:12],
                    self._response_finish_reason(response) or "stop",
                    len(continuation_content),
                )
                if on_progress:
                    await self._emit_noncritical_progress(on_progress, {
                        "stage": "builder_multifile_batch_ready",
                        "batch_index": len(output_parts),
                        "returned_targets": returned_targets[:12],
                        "completed_targets": completed_targets[:12],
                        "finish_reason": self._response_finish_reason(response),
                        "content": continuation_content,
                    })
                await self._publish_partial_output(on_progress, continuation_content, phase="finalizing")
                next_remaining = self._builder_missing_html_targets(
                    input_data,
                    "\n\n".join(output_parts),
                )
                if len(next_remaining) >= len(previous_remaining) and self._response_finish_reason(response).lower() not in {"length", "max_tokens"}:
                    break

            final_output = "\n\n".join(part for part in output_parts if str(part).strip()).strip()
            # §FIX: Detect empty model response for builder nodes.
            # gpt-5.4 sometimes returns chunks=2, content_chars=0 on retry
            # (likely token exhaustion or context overflow). Returning success=True
            # with empty output wastes a retry on a guaranteed quality-gate failure.
            if normalize_node_role(node_type) == "builder" and not final_output.strip():
                empty_err = (
                    f"{model_name} returned empty content for builder node "
                    f"(stream had {len(output_parts)} parts, all empty). "
                    "Possible cause: context overflow or model refusal."
                )
                logger.warning("Builder empty-response detected: %s", empty_err)
                return {
                    "success": False,
                    "output": "",
                    "error": empty_err,
                    "model": model_name,
                    "tool_results": [],
                    "mode": "openai_compatible_chat",
                    "usage": usage,
                    "cost": self._estimate_response_cost(model_name, usage),
                }
            if normalize_node_role(node_type) == "builder":
                failure_msg = self._builder_non_deliverable_output_reason(final_output, input_data)
                if failure_msg:
                    logger.warning("Builder chat returned non-deliverable output: %s", failure_msg)
                    return {
                        "success": False,
                        "output": final_output,
                        "error": failure_msg,
                        "model": model_name,
                        "tool_results": [],
                        "mode": "openai_compatible_chat",
                        "usage": usage,
                        "cost": self._estimate_response_cost(model_name, usage),
                    }
            return {
                "success": True,
                "output": final_output,
                "model": model_name,
                "tool_results": [],
                "mode": "openai_compatible_chat",
                "usage": usage,
                "cost": self._estimate_response_cost(model_name, usage),
            }
        except (TimeoutError, asyncio.TimeoutError) as e:
            err = _sanitize_error(str(e))
            stitched_parts = [part for part in output_parts if str(part).strip()]
            stitched_output = "\n\n".join(stitched_parts).strip()
            latest_trimmed = str(latest_stream_text or "").strip()
            if latest_trimmed:
                if not stitched_output:
                    stitched_output = latest_trimmed
                elif latest_trimmed not in stitched_output:
                    stitched_output = f"{stitched_output}\n\n{latest_trimmed}".strip()
            logger.warning(
                "openai_compatible_chat timeout: %s (partial=%s chars prior_batches=%s)",
                err,
                len(stitched_output or latest_stream_text or ""),
                len(output_parts),
            )
            # v4.1: Record timeout so subsequent nodes auto-skip this model+gateway
            # V4.5: Pass node_type for node-aware cooldown duration
            self._record_gateway_timeout(model_info, f"timeout: {err}", node_type=node_type)
            # Try to salvage whatever the stream produced before the timeout
            if normalize_node_role(node_type) == "builder" and stitched_output:
                salvage = await self._builder_partial_text_salvage_result(
                    node=node,
                    input_data=input_data,
                    output_text=stitched_output,
                    reason=err,
                    on_progress=on_progress,
                    tool_results=[],
                    model_name=model_name,
                    mode="openai_compatible_chat_timeout_salvage",
                    tool_call_stats={},
                )
                if salvage is not None:
                    return salvage
            if on_progress:
                await self._emit_noncritical_progress(
                    on_progress,
                    {"stage": "stream_stalled", "reason": err},
                )
            return {"success": False, "output": "", "error": err}
        except Exception as e:
            err = _sanitize_error(str(e))
            # V4.3 connection self-healing (chat path)
            _err_lower = err.lower()
            if any(kw in _err_lower for kw in (
                "connection", "connect", "transport", "reset by peer",
                "broken pipe", "eof", "ssl", "socket", "network",
            )):
                self._invalidate_openai_client(
                    api_key=api_key,
                    base_url=self._resolved_api_base_for_model_info(model_info),
                    reason=err[:200],
                )
            stitched_parts = [part for part in output_parts if str(part).strip()]
            stitched_output = "\n\n".join(stitched_parts).strip()
            latest_trimmed = str(latest_stream_text or "").strip()
            if latest_trimmed:
                if not stitched_output:
                    stitched_output = latest_trimmed
                elif latest_trimmed not in stitched_output:
                    stitched_output = f"{stitched_output}\n\n{latest_trimmed}".strip()
            if normalize_node_role(node_type) == "builder" and stitched_output:
                salvage = await self._builder_partial_text_salvage_result(
                    node=node,
                    input_data=input_data,
                    output_text=stitched_output,
                    reason=err,
                    on_progress=on_progress,
                    tool_results=[],
                    model_name=model_name,
                    mode="openai_compatible_chat_exception_salvage",
                    tool_call_stats={},
                )
                if salvage is not None:
                    return salvage
            logger.error(f"OpenAI-compatible chat error: {err}")
            return {"success": False, "output": "", "error": err}

    # ─────────────────────────────────────────
    # Path 2b: Agentic Loop (Think-Act-Observe)
    # ─────────────────────────────────────────

    def _agentic_loop_enabled(self) -> bool:
        """Check if the v3.0 AgenticLoop is enabled via feature flag."""
        if not _AGENTIC_RUNTIME_AVAILABLE:
            return False
        cfg = getattr(self, "config", {}) or {}
        if isinstance(cfg, dict) and cfg.get("agentic_loop"):
            return True
        return os.getenv("EVERMIND_AGENTIC_LOOP", "0").strip().lower() in {"1", "true", "yes"}

    async def _execute_agentic_loop(
        self,
        node: Dict,
        plugins: List,
        input_data: str,
        model_info: Dict,
        on_progress: Callable = None,
    ) -> Dict:
        """Execute a node using the AgenticLoop (Think-Act-Observe cycle).

        Full tool capabilities (11 registered):
        - file_read / file_write / file_list: Filesystem operations
        - file_edit: Precise string replacement (à la Claude Code FileEditTool)
        - grep_search: Pattern search via ripgrep
        - glob: Fast file pattern matching (**/*.js, etc.)
        - bash: Shell command execution (sandboxed, timeout 60s)
        - web_fetch: HTTP fetch + HTML→text (TLS verified)
        - web_search: DuckDuckGo-powered web search (no API key)
        - context_compress: Manual context compression trigger (L1/L2/L3)
        - multi_file_read: Batch file reading for efficiency

        Self-correction is limited to within the loop's iteration budget.
        """
        from agentic_runtime import AgenticLoop, LoopConfig, get_tool_registry
        from agentic_runtime import get_tools_for_role as _get_tools_for_role

        node_type = node.get("type", "builder")
        normalized_node_type = normalize_node_role(node_type)
        litellm_model = model_info["litellm_id"]
        max_tokens = self._max_tokens_for_node(
            node_type,
            retry_attempt=int(node.get("retry_attempt", 0)),
            node=node,
        )
        timeout_sec = min(self._effective_timeout_for_node(node_type, input_data, node=node), 600.0)
        system_prompt = self._compose_system_prompt(node, plugins=plugins, input_data=input_data)

        max_iterations, max_tool_calls = self._agentic_loop_limits(node_type, node=node)

        config = LoopConfig(
            max_iterations=max_iterations,
            max_tool_calls=max_tool_calls,
            timeout_seconds=timeout_sec,
            enable_thinking_trace=True,
            enable_sub_agents=False,
            enable_context_compression=True,
            node_type=normalized_node_type,
            node_key=str(node.get("key", node.get("id", ""))),
            model_name=litellm_model,
            allowed_tools=_get_tools_for_role(normalized_node_type),
        )

        # Build LLM call closure
        api_base = model_info.get("api_base", "")
        extra_headers = model_info.get("extra_headers")
        api_key_env = {"kimi": "KIMI_API_KEY", "qwen": "QWEN_API_KEY"}.get(model_info.get("provider"))
        api_key = None
        if api_key_env:
            api_key = (getattr(self, "config", {}) or {}).get(api_key_env.lower()) or os.getenv(api_key_env)

        async def llm_call(messages, tools=None, tool_choice=None):
            kwargs = {
                "model": litellm_model,
                "messages": self._prepare_messages_for_request(messages, litellm_model),
                "max_tokens": max_tokens,
                "timeout": min(timeout_sec, 120),
                "num_retries": 0,
            }
            # V4.3 PERF: Node-specific temperature
            _node_temp = self._temperature_for_node(normalized_node_type, litellm_model)
            if _node_temp is not None:
                kwargs["temperature"] = _node_temp
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = tool_choice or "auto"
            if api_base:
                kwargs["api_base"] = api_base
            if extra_headers:
                kwargs["extra_headers"] = extra_headers
            if api_key:
                kwargs["api_key"] = api_key

            resp = await self._litellm_stream_completion(**kwargs)
            msg = resp.choices[0].message

            # Convert tool_calls to serializable format
            tool_calls_raw = None
            if getattr(msg, "tool_calls", None):
                tool_calls_raw = []
                for tc in msg.tool_calls:
                    tool_calls_raw.append({
                        "id": getattr(tc, "id", f"tc_{id(tc)}"),
                        "function": {
                            "name": getattr(tc.function, "name", ""),
                            "arguments": getattr(tc.function, "arguments", "{}"),
                        },
                    })

            return {
                "message": {
                    "content": getattr(msg, "content", "") or "",
                    "tool_calls": tool_calls_raw,
                },
                "usage": self._normalize_usage(getattr(resp, "usage", None)),
            }

        # Event forwarding to UI
        async def on_event(event):
            if on_progress:
                try:
                    await self._emit_noncritical_progress(on_progress, {
                        "stage": f"agentic_{event.event_type}",
                        "iteration": event.data.get("iteration", 0),
                        "tool": event.data.get("tool", ""),
                        "detail": str(event.data.get("text", event.data.get("result_preview", "")))[:300],
                        "agentic_mode": True,
                    })
                except Exception:
                    pass

        tool_registry = get_tool_registry()
        loop = AgenticLoop(config, tool_registry, llm_call, on_event=on_event)

        logger.info(
            "Starting AgenticLoop for node=%s type=%s model=%s tools=%d max_iter=%d",
            config.node_key,
            normalized_node_type,
            litellm_model,
            len(tool_registry.list_tools(config.allowed_tools)),
            max_iterations,
        )

        if on_progress:
            await self._emit_noncritical_progress(on_progress, {
                "stage": "agentic_loop_start",
                "model": litellm_model,
                "tools_count": len(tool_registry.list_tools(config.allowed_tools)),
                "max_iterations": max_iterations,
                "agentic_mode": True,
            })

        result = await loop.run(system_prompt, input_data)

        logger.info(
            "AgenticLoop completed: node=%s success=%s iterations=%d tool_calls=%d files_created=%d duration=%.1fs",
            config.node_key,
            result.get("success"),
            result.get("iterations", 0),
            sum(result.get("tool_call_stats", {}).values()),
            len(result.get("files_created", [])),
            result.get("duration_seconds", 0),
        )

        if on_progress:
            await self._emit_noncritical_progress(on_progress, {
                "stage": "agentic_loop_complete",
                "iterations": result.get("iterations", 0),
                "tool_calls": sum(result.get("tool_call_stats", {}).values()),
                "files_created": result.get("files_created", [])[:8],
                "files_modified": result.get("files_modified", [])[:8],
                "agentic_mode": True,
            })

        return {
            "success": result.get("success", False),
            "error": (
                f"Agentic loop exhausted: {result.get('exhaustion_reason', 'unknown')}"
                if not result.get("success", False) and result.get("exhausted")
                else ""
            ),
            "output": result.get("output", ""),
            "tool_results": result.get("tool_results", []),
            "model": litellm_model,
            "iterations": result.get("iterations", 0),
            "mode": "agentic_loop",
            "usage": result.get("usage", {}),
            "cost": self._estimate_response_cost(litellm_model, result.get("usage", {})),
            "tool_call_stats": result.get("tool_call_stats", {}),
            "files_created": result.get("files_created", []),
            "files_modified": result.get("files_modified", []),
            "context_compressions": result.get("context_compressions", {}),
            "exhausted": bool(result.get("exhausted", False)),
            "exhaustion_reason": str(result.get("exhaustion_reason", "") or ""),
        }

    # ─────────────────────────────────────────
    # Path 3: LiteLLM with Tool Calling
    # ─────────────────────────────────────────
    async def _execute_litellm_tools(self, node, plugins, input_data, model_info, on_progress) -> Dict:
        node_type = node.get("type", "builder")
        normalized_node_type = normalize_node_role(node_type)
        system_prompt = self._compose_system_prompt(node, plugins=plugins, input_data=input_data)
        litellm_model = model_info["litellm_id"]
        max_tokens = self._max_tokens_for_node(
            node_type,
            retry_attempt=int(node.get("retry_attempt", 0)),
            node=node,
        )
        timeout_sec = self._effective_timeout_for_node(node_type, input_data, node=node)
        builder_has_written_file = False
        polisher_has_written_file = False
        builder_non_write_streak = 0
        polisher_non_write_streak = 0
        output_text = ""
        tool_results: List[Dict[str, Any]] = []
        tool_call_stats: Dict[str, int] = {}
        usage_totals: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        total_cost = 0.0
        builder_support_snapshots: Dict[str, Dict[str, Any]] = {}
        max_continuations = self._read_int_env("EVERMIND_MAX_CONTINUATIONS", 2, 0, 5)

        # Build OpenAI-format tools from plugins
        # V4.5: Sort by name for stable prefix caching (same as openai_compatible path)
        tools = []
        for p in plugins:
            if p.name != "computer_use":
                defn = p.get_tool_definition()
                tools.append({"type": "function", "function": defn} if "function" not in defn else defn)
        tools.sort(key=lambda t: str((t.get("function") or {}).get("name") or ""))
        available_tool_names = self._tool_names_from_defs(tools)
        qa_browser_use_available = self._qa_browser_use_available(node_type, available_tool_names)

        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": input_data}]
        qa_task_type = self._classify_task_type(input_data)
        browser_action_events: List[Dict[str, Any]] = []
        await self._maybe_seed_qa_browser_use(
            node=node,
            node_type=node_type,
            task_type=qa_task_type,
            input_data=input_data,
            plugins=plugins,
            available_tool_names=available_tool_names,
            tool_results=tool_results,
            tool_call_stats=tool_call_stats,
            browser_action_events=browser_action_events,
            messages=messages,
            on_progress=on_progress,
        )
        await self._maybe_seed_qa_browser(
            node=node,
            node_type=node_type,
            task_type=qa_task_type,
            input_data=input_data,
            plugins=plugins,
            available_tool_names=available_tool_names,
            tool_results=tool_results,
            tool_call_stats=tool_call_stats,
            browser_action_events=browser_action_events,
            messages=messages,
            on_progress=on_progress,
        )

        if on_progress:
            await self._emit_noncritical_progress(
                on_progress,
                {"stage": "calling_ai", "model": litellm_model, "tools_count": len(tools)},
            )

        try:
            kwargs = {
                "model": litellm_model,
                "messages": self._prepare_messages_for_request(messages, litellm_model),
                "timeout": timeout_sec,
                "num_retries": 0,
                "max_tokens": max_tokens,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            if model_info.get("api_base"):
                # Allow env-based override for relay/proxy users
                provider = model_info.get("provider", "")
                env_base_key = self._provider_api_base_env_key(provider)
                env_base = os.getenv(env_base_key, "") if env_base_key else ""
                kwargs["api_base"] = env_base if env_base else model_info["api_base"]
            if model_info.get("extra_headers"):
                kwargs["extra_headers"] = model_info["extra_headers"]
            api_key_env = self._provider_api_key_env_key(model_info.get("provider", ""))
            if api_key_env:
                kwargs["api_key"] = self.config.get(api_key_env.lower()) or os.getenv(api_key_env)

            async def _force_builder_text_timeout_fallback(reason: str) -> Optional[Dict[str, Any]]:
                nonlocal output_text, usage_totals, total_cost
                if normalized_node_type != "builder" or builder_has_written_file:
                    return None
                tool_result_text = self._builder_tool_result_salvage_text(tool_results, input_data)
                disk_result_text = self._builder_disk_salvage_text(
                    input_data,
                    incremental_text=output_text,
                )
                combined_salvage_text = "\n\n".join(
                    part for part in (tool_result_text, disk_result_text) if str(part or "").strip()
                )
                output_text = self._select_builder_salvage_text(
                    "",
                    output_text,
                    tool_result_text=combined_salvage_text,
                )

                async def _request_builder_continuation(cont_messages):
                    cont_kwargs = dict(kwargs)
                    cont_kwargs.pop("tools", None)
                    cont_kwargs.pop("tool_choice", None)
                    cont_kwargs["messages"] = self._prepare_messages_for_request(cont_messages, litellm_model)
                    return await self._litellm_stream_completion(**cont_kwargs)

                output_text, _, cont_resp = await self._attempt_builder_game_text_continuation(
                    output_text=output_text,
                    input_data=input_data,
                    system_prompt=system_prompt,
                    continuation_count=0,
                    max_continuations=1,
                    request_continuation=_request_builder_continuation,
                    on_progress=on_progress,
                    log_prefix="Builder timeout salvage (litellm)",
                )
                if cont_resp is not None:
                    usage_totals = self._merge_usage(usage_totals, getattr(cont_resp, "usage", None))
                    total_cost += self._estimate_litellm_cost(cont_resp, litellm_model)

                salvage = await self._builder_partial_text_salvage_result(
                    node=node,
                    input_data=input_data,
                    output_text=output_text,
                    reason=reason or "prewrite_call_timeout",
                    on_progress=on_progress,
                    tool_results=tool_results,
                    model_name=litellm_model,
                    mode="litellm_tools_partial_timeout_salvage",
                    usage=usage_totals,
                    cost=total_cost,
                    tool_call_stats=tool_call_stats,
                )
                if salvage is not None:
                    return salvage
                if on_progress:
                    await self._emit_noncritical_progress(
                        on_progress,
                        self._forcing_text_progress_payload(node_type, reason or "prewrite_call_timeout"),
                    )
                forced_messages = self._builder_forced_text_messages(
                    system_prompt,
                    input_data,
                    tool_results,
                    output_text,
                    reason or "prewrite_call_timeout",
                )
                forced_timeout = self._read_int_env(
                    "EVERMIND_BUILDER_FORCED_TEXT_TIMEOUT_SEC",
                    45,
                    15,
                    120,
                )
                final_kwargs = dict(kwargs)
                final_kwargs.pop("tools", None)
                final_kwargs.pop("tool_choice", None)
                final_kwargs["messages"] = self._prepare_messages_for_request(forced_messages, litellm_model)
                try:
                    final_resp = await asyncio.wait_for(
                        self._litellm_stream_completion(**final_kwargs),
                        timeout=forced_timeout,
                    )
                except Exception as fallback_exc:
                    salvage = await self._builder_partial_text_salvage_result(
                        node=node,
                        input_data=input_data,
                        output_text=output_text,
                        reason=fallback_exc,
                        on_progress=on_progress,
                        tool_results=tool_results,
                        model_name=litellm_model,
                        mode="litellm_tools_partial_timeout_salvage",
                        usage=usage_totals,
                        cost=total_cost,
                        tool_call_stats=tool_call_stats,
                    )
                    if salvage is not None:
                        return salvage
                    logger.warning(
                        "Builder forced text fallback (litellm) after timeout failed: %s",
                        _sanitize_error(str(fallback_exc)),
                    )
                    return None
                final_msg = final_resp.choices[0].message
                final_content = getattr(final_msg, "content", "") or ""
                if not final_content.strip():
                    return await self._builder_partial_text_salvage_result(
                        node=node,
                        input_data=input_data,
                        output_text=output_text,
                        reason=reason or "forced_text_empty",
                        on_progress=on_progress,
                        tool_results=tool_results,
                        model_name=litellm_model,
                        mode="litellm_tools_partial_timeout_salvage",
                        usage=usage_totals,
                        cost=total_cost,
                        tool_call_stats=tool_call_stats,
                    )
                await self._publish_partial_output(on_progress, final_content, phase="finalizing")
                merged_usage = self._merge_usage(usage_totals, getattr(final_resp, "usage", None))
                merged_cost = total_cost + self._estimate_litellm_cost(final_resp, litellm_model)
                return {
                    "success": True,
                    "output": final_content,
                    "tool_results": tool_results,
                    "model": litellm_model,
                    "iterations": 0,
                    "mode": "litellm_tools_forced_text_timeout",
                    "usage": merged_usage,
                    "cost": merged_cost,
                    "tool_call_stats": dict(tool_call_stats),
                }

            async def _await_completion(current_kwargs):
                call = self._litellm_stream_completion(**current_kwargs)
                timeout = 0
                if normalized_node_type == "builder" and not builder_has_written_file:
                    timeout = self._builder_prewrite_call_timeout(node_type, input_data, node=node)
                    if builder_non_write_streak > 0:
                        repair_timeout = self._builder_repair_write_timeout(node_type, input_data, node=node)
                        timeout = min(timeout, repair_timeout)
                elif normalized_node_type == "polisher" and not polisher_has_written_file:
                    timeout = self._polisher_prewrite_call_timeout(node_type, input_data)
                if timeout <= 0:
                    return await call
                try:
                    return await asyncio.wait_for(call, timeout=timeout)
                except asyncio.TimeoutError as exc:
                    if normalized_node_type == "builder":
                        raise TimeoutError(
                            f"builder pre-write timeout after {timeout}s: no real file write or tool progress was produced."
                        ) from exc
                    if normalized_node_type == "polisher":
                        raise TimeoutError(
                            f"polisher pre-write timeout after {timeout}s: no real file write was produced."
                        ) from exc
                    raise

            response = await _await_completion(kwargs)

            iteration = 0
            max_iterations = (
                self._max_tool_iterations_for_node(node_type, node=node)
                if normalized_node_type in {"builder", "polisher"}
                else self._read_int_env("EVERMIND_LITELLM_MAX_TOOL_ITERS", 10, 1, 30)
            )
            usage_totals = self._normalize_usage(getattr(response, "usage", None))
            total_cost = self._estimate_litellm_cost(response, litellm_model)
            continuation_count = 0
            builder_non_write_streak = 0
            builder_force_text_early = False
            builder_force_reason = ""
            builder_force_threshold = self._builder_force_text_threshold(input_data)
            builder_repair_prompts_sent = 0
            polisher_non_write_streak = 0
            polisher_force_threshold = self._polisher_force_write_threshold(input_data)
            polisher_loop_guard_reason = ""
            polisher_repair_prompts_sent = 0
            polisher_force_write_grace_available = False
            polisher_force_write_grace_used = False
            qa_followup_count = 0
            analyst_followup_count = 0
            review_format_followup_count = 0
            plain_text_force_reason = ""

            while iteration < max_iterations:
                iteration += 1
                msg = response.choices[0].message
                msg_content = getattr(msg, "content", "") or ""
                if msg.tool_calls and msg_content:
                    output_text += msg.content

                # Check for tool calls
                if not msg.tool_calls:
                    candidate_plain_text_output = output_text + (msg_content or "")
                    if self._plain_text_node_output_is_materializable(node_type, candidate_plain_text_output):
                        if msg_content:
                            output_text = candidate_plain_text_output
                        break
                    analyst_followup_reason = self._analyst_browser_followup_reason(
                        node_type,
                        tool_call_stats,
                        tool_results,
                        available_tool_names=available_tool_names,
                    )
                    if analyst_followup_reason and analyst_followup_count < 2 and tools:
                        analyst_followup_count += 1
                        if msg_content:
                            messages.append({"role": "assistant", "content": msg_content})
                        messages.append({
                            "role": "user",
                            "content": self._analyst_browser_followup_message(analyst_followup_reason),
                        })
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "analyst_followup",
                                "message": analyst_followup_reason,
                                "continuation": analyst_followup_count,
                            })
                        kwargs["messages"] = self._prepare_messages_for_request(messages, litellm_model)
                        response = await _await_completion(kwargs)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        total_cost += self._estimate_litellm_cost(response, litellm_model)
                        continue
                    followup_reason = self._review_browser_followup_reason(
                        node_type,
                        qa_task_type,
                        browser_action_events,
                        input_data,
                        tool_call_stats=tool_call_stats,
                        available_tool_names=available_tool_names,
                    )
                    if followup_reason and qa_followup_count < 2 and tools:
                        qa_followup_count += 1
                        if msg_content:
                            messages.append({"role": "assistant", "content": msg_content})
                        messages.append({
                            "role": "user",
                            "content": self._review_browser_followup_message(followup_reason, qa_task_type),
                        })
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "qa_followup",
                                "message": followup_reason,
                                "continuation": qa_followup_count,
                            })
                        kwargs["messages"] = self._prepare_messages_for_request(messages, litellm_model)
                        response = await _await_completion(kwargs)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        total_cost += self._estimate_litellm_cost(response, litellm_model)
                        continue
                    format_followup_reason = self._review_output_format_followup_reason(
                        node_type,
                        msg_content,
                    )
                    if format_followup_reason and review_format_followup_count < 2:
                        review_format_followup_count += 1
                        if msg_content:
                            messages.append({"role": "assistant", "content": msg_content})
                        messages.append({
                            "role": "user",
                            "content": self._review_output_format_followup_message(format_followup_reason),
                        })
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "review_format_followup",
                                "message": format_followup_reason,
                                "continuation": review_format_followup_count,
                            })
                        kwargs["messages"] = self._prepare_messages_for_request(messages, litellm_model)
                        response = await _await_completion(kwargs)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        total_cost += self._estimate_litellm_cost(response, litellm_model)
                        continue
                    if msg_content:
                        output_text += msg_content
                    if self._asset_plan_output_complete(node_type, output_text):
                        break
                    break

                messages.append(self._serialize_assistant_message(msg))
                pending_repair_prompts: List[str] = []
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    fn_args = tc.function.arguments
                    parsed_args = self._safe_json_object(fn_args)
                    _tool_call_start_ts = time.time()
                    if on_progress:
                        await self._emit_noncritical_progress(
                            on_progress,
                            self._tool_execution_progress_payload(fn_name, parsed_args),
                        )
                    if fn_name in {"browser", "browser_use"} and self._desktop_qa_browser_suppressed(node_type, input_data):
                        result = {
                            "success": False,
                            "data": {},
                            "error": (
                                "Desktop QA evidence already exists for this reviewer/tester pass. "
                                "Use the provided Evermind internal QA record instead of opening browser/browser_use."
                            ),
                            "artifacts": [],
                        }
                    elif fn_name == "browser" and self._should_block_browser_call(node_type, tool_call_stats, node=node):
                        limit = (
                            self._polisher_browser_call_limit(node=node)
                            if normalized_node_type == "polisher"
                            else self._analyst_browser_call_limit(node=node)
                        )
                        node_label = "Polisher" if normalized_node_type == "polisher" else "Analyst"
                        guidance = (
                            "Stop browsing and write concrete file improvements now."
                            if normalized_node_type == "polisher"
                            else "Skip additional browsing and summarize from collected insights."
                        )
                        result = {
                            "success": False,
                            "data": {},
                            "error": (
                                f"{node_label} browser call limit reached ({limit}). "
                                f"{guidance}"
                            ),
                            "artifacts": [],
                        }
                    elif fn_name == "file_ops" and normalized_node_type in ("analyst", "scribe", "uidesign"):
                        parsed_block_args = self._safe_json_object(fn_args)
                        block_action = str(parsed_block_args.get("action", "")).strip().lower()
                        if block_action in ("write", "edit"):
                            result = {
                                "success": False,
                                "data": {},
                                "error": self._plain_text_node_write_guard_error(normalized_node_type),
                                "artifacts": [],
                            }
                            plain_text_force_reason = plain_text_force_reason or "blocked_file_write"
                            logger.warning(
                                "Blocked file_ops write/edit for %s node (CUA path)",
                                normalized_node_type,
                            )
                        else:
                            result = await self._run_plugin(
                                fn_name,
                                fn_args,
                                plugins,
                                node_type=node_type,
                                node=node,
                            )
                    else:
                        if fn_name == "file_ops":
                            self._builder_capture_prewrite_snapshot(
                                node=node,
                                input_data=input_data,
                                tool_action=str(parsed_args.get("action", "")).strip().lower(),
                                parsed_args=parsed_args,
                                snapshot_cache=builder_support_snapshots,
                            )
                        result = await self._run_plugin(
                            fn_name,
                            fn_args,
                            plugins,
                            node_type=node_type,
                            node=node,
                        )
                    # V4.2: Enrich tool result for report trace table
                    if isinstance(result, dict):
                        result.setdefault("tool", fn_name)
                        result.setdefault("started_at", _tool_call_start_ts)
                        result.setdefault("duration_ms", int((time.time() - _tool_call_start_ts) * 1000) if _tool_call_start_ts else 0)
                        # V4.2 FIX (Codex #3): inject args for ALL tools
                        result.setdefault("args", parsed_args)
                    tool_results.append(result)
                    tool_call_stats[fn_name] = tool_call_stats.get(fn_name, 0) + 1
                    tool_action = (
                        self._infer_file_ops_action(fn_args, result)
                        if fn_name == "file_ops"
                        else str(parsed_args.get("action", "")).strip().lower()
                    )
                    if fn_name == "file_ops":
                        result = self._builder_guard_html_write_result(
                            node=node,
                            input_data=input_data,
                            tool_action=tool_action,
                            result=result,
                            snapshot_cache=builder_support_snapshots,
                        )
                    if fn_name == "browser":
                        browser_data = result.get("data", {}) if isinstance(result, dict) else {}
                        if not isinstance(browser_data, dict):
                            browser_data = {}
                        browser_event = {
                            "action": tool_action or parsed_args.get("action") or "unknown",
                            "subaction": browser_data.get("subaction"),
                            "intent": browser_data.get("intent"),
                            "ok": bool(result.get("success", False)) if isinstance(result, dict) else False,
                            "url": browser_data.get("url"),
                            "target": browser_data.get("target"),
                            "observation": browser_data.get("observation"),
                            "snapshot_refs_preview": browser_data.get("snapshot_refs_preview", []),
                            "snapshot_ref_count": browser_data.get("snapshot_ref_count", 0),
                            "state_hash": browser_data.get("state_hash"),
                            "previous_state_hash": browser_data.get("previous_state_hash"),
                            "state_changed": bool(browser_data.get("state_changed", False)),
                            "scroll_y": browser_data.get("scroll_y", 0),
                            "viewport_height": browser_data.get("viewport_height", 0),
                            "page_height": browser_data.get("page_height", 0),
                            "at_page_top": bool(browser_data.get("at_page_top", False)),
                            "at_page_bottom": bool(browser_data.get("at_page_bottom", False)),
                            "keys_count": browser_data.get("keys_count", 0),
                            "console_error_count": browser_data.get("console_error_count", 0),
                            "page_error_count": browser_data.get("page_error_count", 0),
                            "failed_request_count": browser_data.get("failed_request_count", 0),
                            "recent_console_errors": browser_data.get("recent_console_errors", []),
                            "recent_page_errors": browser_data.get("recent_page_errors", []),
                            "browser_mode": browser_data.get("browser_mode"),
                            "requested_mode": browser_data.get("requested_mode"),
                            "launch_note": browser_data.get("launch_note"),
                            "error": (result.get("error") if isinstance(result, dict) else "") or "",
                        }
                        browser_action_events.append(browser_event)
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "browser_action",
                                "plugin": "browser",
                                **browser_event,
                            })
                    elif fn_name == "browser_use":
                        browser_use_events = self._browser_use_action_events(result)
                        for browser_event in browser_use_events:
                            browser_action_events.append(browser_event)
                            if on_progress:
                                await self._emit_noncritical_progress(on_progress, {
                                    "stage": "browser_action",
                                    "plugin": "browser_use",
                                    **browser_event,
                                })
                    repair_prompt = None
                    wrote_file = False
                    if fn_name == "file_ops":
                        wrote_file = self._tool_result_has_write(result)
                    normalized_node_type = normalize_node_role(node_type)
                    if normalized_node_type == "builder":
                        if wrote_file:
                            builder_has_written_file = True
                            builder_non_write_streak = 0
                            if on_progress:
                                write_path = ""
                                if isinstance(result, dict):
                                    data = result.get("data")
                                    if isinstance(data, dict):
                                        write_path = str(data.get("path") or "").strip()
                                metrics = self._written_file_code_metrics(write_path)
                                await self._emit_noncritical_progress(on_progress, {
                                    "stage": "builder_write",
                                    "plugin": fn_name,
                                    "path": write_path,
                                    **metrics,
                                })
                        else:
                            # Count all non-write tool turns so malformed file_ops args
                            # cannot bypass the loop guard.
                            builder_non_write_streak += 1
                    else:
                        if normalized_node_type == "polisher":
                            if wrote_file:
                                polisher_has_written_file = True
                                polisher_non_write_streak = 0
                            else:
                                polisher_non_write_streak += 1
                        if wrote_file and on_progress:
                            write_path = ""
                            if isinstance(result, dict):
                                data = result.get("data")
                                if isinstance(data, dict):
                                    write_path = str(data.get("path") or "").strip()
                            metrics = self._written_file_code_metrics(write_path)
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "artifact_write",
                                "plugin": fn_name,
                                "path": write_path,
                                "agent": normalized_node_type or node_type,
                                "writer": normalized_node_type or node_type,
                                **metrics,
                            })
                    if fn_name == "file_ops" and normalized_node_type == "builder" and builder_repair_prompts_sent < 2:
                        repair_prompt = self._builder_tool_repair_prompt(input_data, tool_action, result)
                        if not repair_prompt and not wrote_file:
                            repair_prompt = self._builder_non_write_followup_prompt(
                                input_data,
                                tool_action,
                                result,
                                builder_non_write_streak,
                            )
                    elif normalized_node_type == "polisher" and polisher_repair_prompts_sent < 2 and not wrote_file:
                        repair_prompt = self._polisher_non_write_followup_prompt(
                            fn_name,
                            tool_action,
                            result,
                            polisher_non_write_streak,
                        )
                    # Truncate tool output to prevent token overflow
                    result_str = json.dumps(result)
                    _tool_char_limit = MAX_ANALYST_TOOL_RESULT_CHARS if normalized_node_type == "analyst" else MAX_TOOL_RESULT_CHARS
                    if len(result_str) > _tool_char_limit:
                        result_str = result_str[:_tool_char_limit] + '... [TRUNCATED]'
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})
                    if repair_prompt and repair_prompt not in pending_repair_prompts:
                        pending_repair_prompts.append(repair_prompt)
                        if normalized_node_type == "builder":
                            builder_repair_prompts_sent += 1
                        elif normalized_node_type == "polisher":
                            polisher_repair_prompts_sent += 1
                            if fn_name == "file_ops" and not polisher_force_write_grace_used:
                                polisher_force_write_grace_available = True

                if pending_repair_prompts:
                    messages.append({"role": "user", "content": "\n\n".join(pending_repair_prompts)})

                if (
                    normalized_node_type in {"analyst", "scribe", "uidesign"}
                    and plain_text_force_reason == "blocked_file_write"
                ):
                    logger.info(
                        "Plain-text node blocked from file_ops write (litellm) — ending tool loop and switching to final no-tool synthesis (%s)",
                        normalized_node_type,
                    )
                    break

                if (
                    normalize_node_role(node_type) == "builder"
                    and builder_non_write_streak >= builder_force_threshold
                    and not any(self._tool_result_has_write(tr) for tr in tool_results)
                ):
                    builder_force_text_early = True
                    builder_force_reason = "tool_research_loop"
                    logger.info(
                        "Builder loop guard triggered (litellm): non-write tool streak=%s (threshold=%s)",
                        builder_non_write_streak,
                        builder_force_threshold,
                    )
                    if on_progress:
                        await self._emit_noncritical_progress(on_progress, {
                            "stage": "builder_loop_guard",
                            "streak": builder_non_write_streak,
                            "threshold": builder_force_threshold,
                            "reason": builder_force_reason,
                        })
                    break

                if (
                    normalized_node_type == "polisher"
                    and not polisher_has_written_file
                    and polisher_non_write_streak >= polisher_force_threshold
                ):
                    if polisher_force_write_grace_available and not polisher_force_write_grace_used:
                        polisher_force_write_grace_available = False
                        polisher_force_write_grace_used = True
                        logger.info(
                            "Polisher force-write grace granted (litellm): non-write tool streak=%s (threshold=%s)",
                            polisher_non_write_streak,
                            polisher_force_threshold,
                        )
                        if on_progress:
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "polisher_force_write_warning",
                                "streak": polisher_non_write_streak,
                                "threshold": polisher_force_threshold,
                                "message": "Polisher inspection limit reached; one final response is allowed to obey the forced-write prompt.",
                            })
                        kwargs["messages"] = self._prepare_messages_for_request(messages, litellm_model)
                        response = await _await_completion(kwargs)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        total_cost += self._estimate_litellm_cost(response, litellm_model)
                        continue
                    polisher_loop_guard_reason = (
                        "polisher loop guard triggered after "
                        f"{polisher_non_write_streak} non-write tool iterations without any file write."
                    )
                    logger.warning("%s (litellm)", polisher_loop_guard_reason)
                    if on_progress:
                        await self._emit_noncritical_progress(on_progress, {
                            "stage": "polisher_loop_guard",
                            "streak": polisher_non_write_streak,
                            "threshold": polisher_force_threshold,
                            "reason": polisher_loop_guard_reason,
                        })
                    break

                if on_progress:
                    await self._emit_noncritical_progress(
                        on_progress,
                        {"stage": "continuing", "iteration": iteration},
                    )
                kwargs["messages"] = self._prepare_messages_for_request(messages, litellm_model)
                response = await _await_completion(kwargs)
                usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                total_cost += self._estimate_litellm_cost(response, litellm_model)

            if polisher_loop_guard_reason:
                return self._attach_browser_action_events({
                    "success": False,
                    "output": output_text,
                    "tool_results": tool_results,
                    "model": litellm_model,
                    "iterations": iteration,
                    "mode": "litellm_tools",
                    "usage": usage_totals,
                    "cost": total_cost,
                    "tool_call_stats": dict(tool_call_stats),
                    "error": polisher_loop_guard_reason,
                    "qa_browser_use_available": qa_browser_use_available,
                }, browser_action_events)

            force_text_reason = ""
            if self._builder_needs_forced_text(node_type, output_text, tool_results):
                if builder_force_text_early:
                    force_text_reason = builder_force_reason or "tool_research_loop"
                elif iteration >= max_iterations:
                    force_text_reason = "tool_iterations_exhausted"
                else:
                    force_text_reason = "no_html_or_file_output"

            if force_text_reason:
                if on_progress:
                    await self._emit_noncritical_progress(
                        on_progress,
                        self._forcing_text_progress_payload(node_type, force_text_reason),
                    )
                final_messages = self._builder_forced_text_messages(
                    system_prompt,
                    input_data,
                    tool_results,
                    output_text,
                    force_text_reason,
                )
                final_kwargs = dict(kwargs)
                final_kwargs.pop("tools", None)
                final_kwargs.pop("tool_choice", None)
                final_kwargs["messages"] = self._prepare_messages_for_request(final_messages, litellm_model)
                final_resp = await _await_completion(final_kwargs)
                final_msg = final_resp.choices[0].message
                if final_msg.content:
                    output_text += final_msg.content
                    await self._publish_partial_output(on_progress, output_text, phase="finalizing")
                usage_totals = self._merge_usage(usage_totals, getattr(final_resp, "usage", None))
                total_cost += self._estimate_litellm_cost(final_resp, litellm_model)

            plain_text_final_reason = ""
            if self._plain_text_node_needs_forced_output(node_type, output_text, tool_results):
                if plain_text_force_reason:
                    plain_text_final_reason = plain_text_force_reason
                elif iteration >= max_iterations:
                    plain_text_final_reason = "tool_iterations_exhausted"
                else:
                    plain_text_final_reason = "missing_final_handoff"

            if plain_text_final_reason:
                if on_progress:
                    await self._emit_noncritical_progress(
                        on_progress,
                        self._forcing_text_progress_payload(node_type, plain_text_final_reason),
                    )
                final_messages = self._plain_text_node_forced_messages(
                    system_prompt,
                    input_data,
                    tool_results,
                    output_text,
                    node_type,
                    plain_text_final_reason,
                )
                final_kwargs = dict(kwargs)
                final_kwargs.pop("tools", None)
                final_kwargs.pop("tool_choice", None)
                final_kwargs["messages"] = self._prepare_messages_for_request(final_messages, litellm_model)
                final_resp = await _await_completion(final_kwargs)
                final_msg = final_resp.choices[0].message
                final_content = str(final_msg.content or "").strip()
                if final_content:
                    if output_text and not output_text.endswith("\n"):
                        output_text += "\n"
                    output_text += final_content
                    await self._publish_partial_output(on_progress, output_text, phase="finalizing")
                usage_totals = self._merge_usage(usage_totals, getattr(final_resp, "usage", None))
                total_cost += self._estimate_litellm_cost(final_resp, litellm_model)

            reviewer_force_reason = ""
            if self._reviewer_needs_forced_verdict(node_type, output_text, tool_results):
                reviewer_force_reason = (
                    self._review_output_format_followup_reason(node_type, output_text)
                    or "missing_reviewer_verdict_json"
                )

            if reviewer_force_reason:
                if on_progress:
                    await self._emit_noncritical_progress(
                        on_progress,
                        self._forcing_text_progress_payload(node_type, reviewer_force_reason),
                    )
                final_messages = self._reviewer_forced_verdict_messages(
                    system_prompt,
                    input_data,
                    tool_results,
                    output_text,
                    reviewer_force_reason,
                )
                final_kwargs = dict(kwargs)
                final_kwargs.pop("tools", None)
                final_kwargs.pop("tool_choice", None)
                final_kwargs["messages"] = self._prepare_messages_for_request(final_messages, litellm_model)
                final_resp = await _await_completion(final_kwargs)
                final_msg = final_resp.choices[0].message
                final_content = str(final_msg.content or "").strip()
                if (
                    final_content
                    and self._review_output_format_followup_reason(node_type, final_content) is None
                ):
                    output_text = final_content
                    await self._publish_partial_output(on_progress, output_text, phase="finalizing")
                usage_totals = self._merge_usage(usage_totals, getattr(final_resp, "usage", None))
                total_cost += self._estimate_litellm_cost(final_resp, litellm_model)

            if normalized_node_type == "builder":
                tool_result_text = self._builder_tool_result_salvage_text(tool_results, input_data)
                disk_result_text = self._builder_disk_salvage_text(
                    input_data,
                    incremental_text=output_text,
                )
                combined_salvage_text = "\n\n".join(
                    part for part in (tool_result_text, disk_result_text) if str(part or "").strip()
                )
                preferred_output = self._select_builder_salvage_text(
                    "",
                    output_text,
                    tool_result_text=combined_salvage_text,
                )
                if (
                    preferred_output
                    and preferred_output != output_text
                    and self._builder_partial_text_is_salvageable(input_data, preferred_output)
                ):
                    output_text = preferred_output
                    await self._publish_partial_output(on_progress, output_text, phase="finalizing")

                async def _request_builder_continuation(cont_messages):
                    cont_kwargs = dict(kwargs)
                    cont_kwargs.pop("tools", None)
                    cont_kwargs.pop("tool_choice", None)
                    cont_kwargs["messages"] = self._prepare_messages_for_request(cont_messages, litellm_model)
                    return await self._litellm_stream_completion(**cont_kwargs)

                output_text, continuation_count, cont_resp = await self._attempt_builder_game_text_continuation(
                    output_text=output_text,
                    input_data=input_data,
                    system_prompt=system_prompt,
                    continuation_count=continuation_count,
                    max_continuations=max_continuations,
                    request_continuation=_request_builder_continuation,
                    on_progress=on_progress,
                    log_prefix="Builder text-mode (litellm)",
                )
                if cont_resp is not None:
                    usage_totals = self._merge_usage(usage_totals, getattr(cont_resp, "usage", None))
                    total_cost += self._estimate_litellm_cost(cont_resp, litellm_model)

                saved_paths = await self._auto_save_builder_text_output(
                    output_text=output_text,
                    input_data=input_data,
                    node=node,
                    tool_results=tool_results,
                    tool_call_stats=tool_call_stats,
                    on_progress=on_progress,
                )
                if saved_paths:
                    builder_has_written_file = True
                elif not self._builder_text_output_has_persistable_html(output_text, input_data):
                    failure_msg = self._builder_non_deliverable_output_reason(output_text, input_data)
                    logger.warning("%s (litellm)", failure_msg)
                    return self._attach_browser_action_events({
                        "success": False,
                        "output": output_text,
                        "tool_results": tool_results,
                        "model": litellm_model,
                        "iterations": iteration,
                        "mode": "litellm_tools",
                        "usage": usage_totals,
                        "cost": total_cost,
                        "tool_call_stats": dict(tool_call_stats),
                        "error": failure_msg,
                        "qa_browser_use_available": qa_browser_use_available,
                    }, browser_action_events)

            return self._attach_browser_action_events(
                {"success": True, "output": output_text, "tool_results": tool_results,
                 "model": litellm_model, "iterations": iteration, "mode": "litellm_tools", "usage": usage_totals, "cost": total_cost,
                 "tool_call_stats": tool_call_stats, "qa_browser_use_available": qa_browser_use_available},
                browser_action_events,
            )
        except Exception as e:
            err = _sanitize_error(str(e))
            if normalized_node_type == "builder" and not builder_has_written_file and "timeout" in err.lower():
                fallback = await _force_builder_text_timeout_fallback(err)
                if fallback is not None:
                    return fallback
            logger.error(f"LiteLLM tools error: {_sanitize_error(str(e))}")
            return self._attach_browser_action_events(
                {"success": False, "output": "", "error": err},
                locals().get("browser_action_events"),
            )

    # ─────────────────────────────────────────
    # Path 3: LiteLLM Direct Chat
    # ─────────────────────────────────────────
    async def _execute_litellm_chat(self, node, input_data, model_info, on_progress) -> Dict:
        system_prompt = self._compose_system_prompt(node, input_data=input_data)
        node_type = node.get("type", "builder")
        litellm_model = model_info["litellm_id"]
        max_tokens = self._max_tokens_for_node(
            node_type,
            retry_attempt=int(node.get("retry_attempt", 0)),
            node=node,
        )
        timeout_sec = min(self._effective_timeout_for_node(node_type, input_data, node=node), 240)
        direct_multifile = (
            normalize_node_role(node_type) == "builder"
            and str(node.get("builder_delivery_mode") or "").strip().lower() == "direct_multifile"
        )
        if direct_multifile:
            max_tokens, timeout_sec = self._builder_direct_multifile_budget(
                input_data,
                max_tokens=max_tokens,
                timeout_sec=timeout_sec,
            )

        if on_progress:
            await self._emit_noncritical_progress(
                on_progress,
                {"stage": "calling_ai", "model": litellm_model, "tools_count": 0},
            )

        try:
            kwargs_base = {
                "model": litellm_model,
                "timeout": timeout_sec,
                "num_retries": 0,
                "max_tokens": max_tokens,
            }
            # V4.3 PERF: Node-specific temperature
            _node_temp = self._temperature_for_node(node_type, litellm_model)
            if _node_temp is not None:
                kwargs_base["temperature"] = _node_temp
            if model_info.get("api_base"):
                provider = model_info.get("provider", "")
                env_base_key = self._provider_api_base_env_key(provider)
                env_base = os.getenv(env_base_key, "") if env_base_key else ""
                kwargs_base["api_base"] = env_base if env_base else model_info["api_base"]
            if model_info.get("extra_headers"):
                kwargs_base["extra_headers"] = model_info["extra_headers"]
            api_key_env = self._provider_api_key_env_key(model_info.get("provider", ""))
            if api_key_env:
                kwargs_base["api_key"] = self.config.get(api_key_env.lower()) or os.getenv(api_key_env)

            async def _call_chat(current_messages: List[Dict[str, str]]):
                kwargs = dict(kwargs_base)
                kwargs["messages"] = self._prepare_messages_for_request(current_messages, litellm_model)
                return await self._litellm_stream_completion(**kwargs)

            response = await _call_chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_data},
            ])
            usage = self._normalize_usage(getattr(response, "usage", None))
            total_cost = self._estimate_litellm_cost(response, litellm_model)
            output_parts: List[str] = []
            content = response.choices[0].message.content or ""
            if content:
                output_parts.append(content)
                await self._publish_partial_output(on_progress, content, phase="drafting")

            continuation_limit = (
                self._read_int_env("EVERMIND_BUILDER_DIRECT_MULTIFILE_CONTINUATIONS", 2, 0, 4)
                if direct_multifile
                else 0
            )
            continuation_count = 0
            while direct_multifile and continuation_count < continuation_limit:
                combined_output = "\n\n".join(part for part in output_parts if str(part).strip())
                remaining_targets = self._builder_missing_html_targets(input_data, combined_output)
                if not remaining_targets:
                    break
                previous_remaining = list(remaining_targets)
                if on_progress:
                    await self._emit_noncritical_progress(on_progress, {
                        "stage": "builder_multifile_continue",
                        "continuation": continuation_count + 1,
                        "remaining_targets": remaining_targets[:12],
                        "finish_reason": self._response_finish_reason(response),
                    })
                response = await _call_chat(
                    self._builder_direct_multifile_continuation_messages(
                        system_prompt,
                        input_data,
                        combined_output,
                        remaining_targets,
                    )
                )
                usage = self._merge_usage(usage, getattr(response, "usage", None))
                total_cost += self._estimate_litellm_cost(response, litellm_model)
                continuation_count += 1
                continuation_content = response.choices[0].message.content or ""
                if not continuation_content.strip():
                    break
                output_parts.append(continuation_content)
                await self._publish_partial_output(on_progress, continuation_content, phase="finalizing")
                next_remaining = self._builder_missing_html_targets(
                    input_data,
                    "\n\n".join(output_parts),
                )
                if len(next_remaining) >= len(previous_remaining) and self._response_finish_reason(response).lower() not in {"length", "max_tokens"}:
                    break

            final_output = "\n\n".join(part for part in output_parts if str(part).strip()).strip()
            return {"success": True, "output": final_output,
                    "model": litellm_model, "tool_results": [], "mode": "litellm_chat",
                    "usage": usage,
                    "cost": total_cost}
        except Exception as e:
            logger.error(f"LiteLLM chat error: {_sanitize_error(str(e))}")
            return {"success": False, "output": "", "error": _sanitize_error(str(e))}

    # ─────────────────────────────────────────
    # Fallback: Direct OpenAI
    # ─────────────────────────────────────────
    async def _execute_openai_direct(self, node, plugins, input_data, on_progress) -> Dict:
        client = await self._get_openai()
        if not client:
            return {"success": False, "output": "", "error": "No AI backend available"}
        system_prompt = self._compose_system_prompt(node, plugins=plugins, input_data=input_data)
        try:
            response = await client.chat.completions.create(
                model="gpt-4o", messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": input_data}
                ]
            )
            await self._publish_partial_output(on_progress, response.choices[0].message.content or "", phase="drafting")
            usage = self._normalize_usage(getattr(response, "usage", None))
            cost = self._estimate_litellm_cost(response, "gpt-4o") if self._litellm else self._estimate_response_cost("gpt-4o", usage)
            return {"success": True, "output": response.choices[0].message.content, "model": "gpt-4o", "tool_results": [], "mode": "openai_direct",
                    "usage": usage, "cost": cost}
        except Exception as e:
            return {"success": False, "output": "", "error": _sanitize_error(str(e))}

    # ─────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────
    async def _run_plugin(
        self,
        name: str,
        args,
        plugins: List[Plugin],
        node_type: str = "",
        node: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        # Enforce per-node plugin allowlist.
        normalized_node_type = normalize_node_role(node_type) or str(node_type or "").strip().lower()
        plugin = next((p for p in plugins if p and p.name == name), None)
        if not plugin:
            return {"error": f"Plugin {name} not enabled for this node"}
        if args is None:
            parsed: Dict[str, Any] = {}
        elif isinstance(args, str):
            try:
                parsed_obj = json.loads(args)
            except json.JSONDecodeError:
                # Keep plugin call alive even with malformed tool args.
                parsed_obj = {}
            parsed = parsed_obj if isinstance(parsed_obj, dict) else {"value": parsed_obj}
        elif isinstance(args, dict):
            parsed = args
        else:
            parsed = {"value": args}
        parsed = self._apply_qa_browser_tool_defaults(name, node_type, parsed)

        plugin_context = dict(self.config or {})
        force_visible = str(
            plugin_context.get(
                "reviewer_tester_force_headful",
                os.getenv("EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL", "0"),
            )
        ).strip().lower() in ("1", "true", "yes", "on")
        if name in {"browser", "browser_use"} and normalized_node_type not in ("reviewer", "tester", "browser", "uicontrol"):
            plugin_context.pop("browser_headful", None)
            plugin_context.pop("browser_force_reason", None)
        if name == "browser" and force_visible and normalized_node_type in ("reviewer", "tester"):
            plugin_context["browser_headful"] = True
            plugin_context["browser_force_reason"] = f"{normalized_node_type}_visible_review"
        if name == "browser":
            node_meta = node if isinstance(node, dict) else {}
            plugin_context["node_type"] = normalized_node_type or node_type
            plugin_context["node_execution_id"] = str(node_meta.get("node_execution_id") or "").strip()
            plugin_context["run_id"] = str(node_meta.get("run_id") or "").strip()
            plugin_context["browser_save_evidence"] = normalized_node_type in ("reviewer", "tester")
            action = str(parsed.get("action", "navigate") or "navigate").strip().lower()
            url = str(parsed.get("url") or "").strip()
            if normalized_node_type == "analyst" and action == "navigate" and self._is_local_preview_or_nonweb_url(url):
                return {
                    "success": False,
                    "data": {},
                    "error": (
                        "Analyst browser research must use external implementation sources only. "
                        "Do not open localhost, /preview/, or blank URLs."
                    ),
                    "artifacts": [],
                    "_plugin": name,
                }
        if name == "browser_use":
            node_meta = node if isinstance(node, dict) else {}
            plugin_context["node_type"] = normalized_node_type or node_type
            plugin_context["node_execution_id"] = str(node_meta.get("node_execution_id") or "").strip()
            plugin_context["run_id"] = str(node_meta.get("run_id") or "").strip()
            plugin_context["output_dir"] = str(
                node_meta.get("output_dir")
                or plugin_context.get("output_dir")
                or self._current_output_dir()
            ).strip()
            if force_visible and normalized_node_type in ("reviewer", "tester"):
                plugin_context["browser_headful"] = True
                plugin_context["browser_force_reason"] = f"{normalized_node_type}_visible_review"
        if name == "file_ops":
            node_meta = node if isinstance(node, dict) else {}
            plugin_context["file_ops_node_type"] = normalized_node_type or node_type
            plugin_context["node_execution_id"] = str(node_meta.get("node_execution_id") or "").strip()
            output_dir = str(
                node_meta.get("output_dir")
                or plugin_context.get("output_dir")
                or self._current_output_dir()
            ).strip()
            if output_dir:
                plugin_context["file_ops_output_dir"] = output_dir
            if normalized_node_type in ("reviewer", "tester", "deployer", "analyst", "scribe", "uidesign"):
                plugin_context["file_ops_mode"] = "read_only"
            elif normalized_node_type == "builder":
                builder_output_dir = str(
                    node_meta.get("builder_file_ops_output_dir")
                    or node_meta.get("builder_staging_output_dir")
                    or node_meta.get("output_dir")
                    or plugin_context.get("output_dir")
                    or self._current_output_dir()
                ).strip()
                if builder_output_dir:
                    plugin_context["file_ops_output_dir"] = builder_output_dir
                allowed_html_targets = node_meta.get("allowed_html_targets") or node_meta.get("file_ops_allowed_html_targets") or []
                if isinstance(allowed_html_targets, list) and allowed_html_targets:
                    plugin_context["file_ops_allowed_html_targets"] = [
                        str(item).strip()
                        for item in allowed_html_targets
                        if str(item).strip()
                    ]
                plugin_context["file_ops_write_token"] = str(
                    node_meta.get("file_ops_write_token")
                    or node_meta.get("write_token")
                    or ""
                ).strip()
                plugin_context["file_ops_can_write_root_index"] = bool(
                    node_meta.get("can_write_root_index")
                    or node_meta.get("file_ops_can_write_root_index")
                )
                plugin_context["file_ops_enforce_html_targets"] = bool(
                    node_meta.get("enforce_html_targets")
                    or node_meta.get("file_ops_enforce_html_targets")
                )
                plugin_context["file_ops_require_existing_artifact_read"] = bool(
                    node_meta.get("builder_existing_artifact_patch_mode")
                    or node_meta.get("file_ops_require_existing_artifact_read")
                )
                required_read_targets = node_meta.get("builder_patch_required_read_targets") or []
                if isinstance(required_read_targets, list) and required_read_targets:
                    plugin_context["file_ops_required_read_targets"] = [
                        str(item).strip()
                        for item in required_read_targets
                        if str(item).strip()
                    ]

        result = await plugin.execute(parsed, context=plugin_context)
        if result is None:
            return {"error": f"Plugin {name} returned no result"}
        if hasattr(result, "to_dict"):
            payload = result.to_dict()
        elif isinstance(result, dict):
            payload = result
        else:
            payload = {"ok": True, "data": str(result)}
        if isinstance(payload, dict):
            payload.setdefault("_plugin", name)
        return payload


# ─────────────────────────────────────────────
# Handoff Manager
# ─────────────────────────────────────────────
class HandoffManager:
    """Agent-to-agent task delegation (OpenAI Agents SDK pattern)."""

    def __init__(self, ai_bridge: AIBridge):
        self.ai_bridge = ai_bridge

    async def handoff(self, from_node: Dict, to_node_type: str, task: str,
                      all_nodes: List[Dict], on_progress: Callable = None) -> Dict:
        target = next((n for n in all_nodes if n["type"] == to_node_type), None)
        if not target:
            return {"success": False, "error": f"No {to_node_type} node found"}
        if on_progress:
            await self.ai_bridge._emit_noncritical_progress(
                on_progress,
                {
                    "stage": "handoff",
                    "from": from_node.get("name", ""),
                    "to": target.get("name", ""),
                    "task": task[:100],
                },
            )
        enabled = resolve_enabled_plugins_for_node(
            to_node_type,
            explicit_plugins=target.get("plugins"),
            config=self.ai_bridge.config,
        )
        plugins = [PluginRegistry.get(p) for p in enabled if PluginRegistry.get(p)]
        return await self.ai_bridge.execute(node=target, plugins=plugins,
                                            input_data=f"[Handoff from {from_node.get('name', '?')}] {task}", on_progress=on_progress)
