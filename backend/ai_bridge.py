"""
Evermind Backend — AI Bridge v3 (LiteLLM Unified Interface)
Supports 100+ LLM models through a single interface.
References: https://github.com/BerriAI/litellm
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

from plugins.base import Plugin, PluginResult, PluginRegistry, is_builder_browser_enabled, is_image_generation_available
from agent_skills import build_skill_context, resolve_skill_names_for_goal
from repo_map import build_repo_context
import task_classifier
from privacy import get_masker, PrivacyMasker
from proxy_relay import get_relay_manager

logger = logging.getLogger("evermind.ai_bridge")

# ─────────────────────────────────────────────
# Security — sanitize error messages to remove API keys
# ─────────────────────────────────────────────
_SENSITIVE_RE = re.compile(
    r"(?:sk|key|token|api[_-]?key|Bearer)[-_\s]?[a-zA-Z0-9._\-]{8,}",
    re.IGNORECASE,
)


def _sanitize_error(msg: str) -> str:
    """Strip potential API keys / secrets from error messages."""
    if not msg:
        return "Unknown error"
    sanitized = _SENSITIVE_RE.sub("[REDACTED]", msg)
    return sanitized or "Unknown error"

# Maximum characters kept from each tool call result before injecting into messages.
# Prevents token explosion when file_ops reads large HTML files (24K+ chars → 700K+ tokens).
MAX_TOOL_RESULT_CHARS = int(os.getenv("EVERMIND_MAX_TOOL_RESULT_CHARS", "8000"))
# Maximum content from assistant replayed back to tool-loop context.
MAX_ASSISTANT_REPLAY_CHARS = int(os.getenv("EVERMIND_MAX_ASSISTANT_REPLAY_CHARS", "4000"))
# Maximum reasoning trace retained in replay context.
MAX_REASONING_REPLAY_CHARS = int(os.getenv("EVERMIND_MAX_REASONING_REPLAY_CHARS", "1200"))
# Maximum tool arguments retained in assistant tool_call replay payload.
MAX_TOOL_ARGS_REPLAY_CHARS = int(os.getenv("EVERMIND_MAX_TOOL_ARGS_REPLAY_CHARS", "2000"))
# Generic cap for user/system message content in replay.
MAX_MESSAGE_CONTENT_CHARS = int(os.getenv("EVERMIND_MAX_MESSAGE_CONTENT_CHARS", "12000"))
# Global safety cap: compact older context when total message chars exceed this budget.
MAX_REQUEST_TOTAL_CHARS = int(os.getenv("EVERMIND_MAX_REQUEST_TOTAL_CHARS", "120000"))
MAX_CONTEXT_KEEP_LAST_MESSAGES = int(os.getenv("EVERMIND_MAX_CONTEXT_KEEP_LAST_MESSAGES", "10"))
CONTEXT_OMITTED_MARKER = "... [OLDER_CONTEXT_OMITTED_FOR_TOKEN_BUDGET]"

# ─────────────────────────────────────────────
# Model Registry — all supported models
# ─────────────────────────────────────────────
MODEL_REGISTRY = {
    # OpenAI
    "gpt-5.4": {"provider": "openai", "litellm_id": "gpt-5.4", "supports_tools": True, "supports_cua": True},
    "gpt-4.1": {"provider": "openai", "litellm_id": "gpt-4.1", "supports_tools": True, "supports_cua": False},
    "gpt-4o": {"provider": "openai", "litellm_id": "gpt-4o", "supports_tools": True, "supports_cua": False},
    "o3": {"provider": "openai", "litellm_id": "o3", "supports_tools": True, "supports_cua": False},
    # Anthropic
    "claude-4-sonnet": {"provider": "anthropic", "litellm_id": "claude-4-sonnet-20260514", "supports_tools": True, "supports_cua": False},
    "claude-4-opus": {"provider": "anthropic", "litellm_id": "claude-4-opus-20260514", "supports_tools": True, "supports_cua": False},
    "claude-3.5-sonnet": {"provider": "anthropic", "litellm_id": "claude-3-5-sonnet-20241022", "supports_tools": True, "supports_cua": False},
    # Google
    "gemini-2.5-pro": {"provider": "google", "litellm_id": "gemini/gemini-2.5-pro-preview-06-05", "supports_tools": True, "supports_cua": False},
    "gemini-2.0-flash": {"provider": "google", "litellm_id": "gemini/gemini-2.0-flash", "supports_tools": True, "supports_cua": False},
    # DeepSeek
    "deepseek-v3": {"provider": "deepseek", "litellm_id": "deepseek/deepseek-chat", "supports_tools": True, "supports_cua": False},
    "deepseek-r1": {"provider": "deepseek", "litellm_id": "deepseek/deepseek-reasoner", "supports_tools": False, "supports_cua": False},
    # Kimi / Moonshot (sk-kimi-* keys 需要 Kimi Coding 端点)
    "kimi": {"provider": "kimi", "litellm_id": "openai/kimi-k2.5", "supports_tools": True, "supports_cua": False,
             "api_base": "https://api.kimi.com/coding/v1",
             "extra_headers": {"User-Agent": "claude-code/1.0", "X-Client-Name": "claude-code"}},
    # Kimi Coding (新平台: api.kimi.com, sk-kimi-* keys, 需要 User-Agent)
    "kimi-k2.5": {"provider": "kimi", "litellm_id": "openai/kimi-k2.5", "supports_tools": True, "supports_cua": False,
                  "api_base": "https://api.kimi.com/coding/v1",
                  "extra_headers": {"User-Agent": "claude-code/1.0", "X-Client-Name": "claude-code"}},
    "kimi-coding": {"provider": "kimi", "litellm_id": "openai/kimi-k2.5", "supports_tools": True, "supports_cua": False,
                    "api_base": "https://api.kimi.com/coding/v1",
                    "extra_headers": {"User-Agent": "claude-code/1.0", "X-Client-Name": "claude-code"}},
    # Qwen / 通义千问
    "qwen-max": {"provider": "qwen", "litellm_id": "openai/qwen-max", "supports_tools": True, "supports_cua": False,
                 "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    # Local / Ollama
    "ollama-llama3": {"provider": "ollama", "litellm_id": "ollama/llama3", "supports_tools": False, "supports_cua": False},
    "ollama-qwen2.5": {"provider": "ollama", "litellm_id": "ollama/qwen2.5", "supports_tools": False, "supports_cua": False},
}

# ─────────────────────────────────────────────
# Agent Presets
# ─────────────────────────────────────────────
AGENT_PRESETS = {
    "router": {
        "instructions": (
            "You are a task router and planner. Analyze the user's request and output a JSON plan.\n"
            "Format: {\"subtasks\": [{\"agent\": \"builder|tester|reviewer|deployer|analyst|debugger|scribe|uidesign|imagegen|spritesheet|assetimport\", \"task\": \"description\", \"depends_on\": []}]}\n"
            "Each subtask should have a clear agent assignment and description.\n"
            "IMPORTANT RULES for website/app tasks:\n"
            "- The builder task should specify: 'Create a complete, self-contained HTML file with embedded CSS and JavaScript'\n"
            "- The deployer task should say: 'List the generated files and provide the local preview URL'\n"
            "- The tester should say: 'Verify the generated HTML files exist and are valid'\n"
            "- Use scribe for documentation/manual/report tasks\n"
            "- Use imagegen for image-prompt / concept-art / poster / cover generation tasks only when a real image backend is configured or when prompt packs alone are explicitly acceptable\n"
            "- Use spritesheet or assetimport for game-asset pipeline tasks when useful, but do NOT insert them as fake filler nodes when no actual asset pipeline is available\n"
            "- Use uidesign for design-system or UI-direction tasks when explicit design output is needed\n"
            "- For game research tasks, do NOT send analyst to spend time playing browser games; prefer GitHub repos, source references, tutorials, docs, postmortems, and implementation writeups\n"
            "- Keep the plan to 3-5 subtasks for efficiency"
        ),
    },
    "planner": {
        "instructions": (
            "You are a senior project planner. Your ONLY job is to produce a lean execution skeleton.\n"
            "\n"
            "## What you MUST output (JSON):\n"
            "{\n"
            '  "architecture": "brief info-architecture summary (2-3 sentences)",\n'
            '  "modules": ["module1", "module2", ...],\n'
            '  "execution_order": ["step1 -> step2 -> ..."],\n'
            '  "key_dependencies": ["dep1", "dep2"]\n'
            "}\n"
            "\n"
            "## What you must NOT do:\n"
            "- Do NOT write any code, HTML, CSS, or JavaScript\n"
            "- Do NOT write marketing copy, slogans, or detailed text content\n"
            "- Do NOT design animations, transitions, or visual effects\n"
            "- Do NOT produce more than 400 words total\n"
            "\n"
            "You are the SKELETON planner. Other nodes (builder, analyst, reviewer) will handle details.\n"
            "Keep it short. Keep it structural. Finish fast."
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
            "HARD RULES:\n"
            "1. Output ONE complete index.html with ALL CSS in <style> and ALL JS in <script>\n"
            "2. Start with <!DOCTYPE html> and end with </html>; no truncation, no placeholders\n"
            "3. Implement responsive design with at least one @media breakpoint\n"
            "4. Include accessibility basics: lang attr, aria labels, focus styles\n"
            "5. Use CSS variables for colors, spacing, and shadows\n"
            "6. Choose typography intentionally for the product category; avoid default-looking font decisions\n"
            "7. Do NOT use emoji characters as UI icons; use inline SVG instead\n"
            "   Treat any emoji glyph in the final page as a hard failure and remove it before finishing\n"
            "8. Smooth animations with cubic-bezier(0.4,0,0.2,1); honor prefers-reduced-motion\n"
            "9. Implement as much code as the task requires; do not compress the result into a low-quality stub\n"
            "10. Treat loaded skills, analyst notes, reviewer acceptance criteria, and task constraints as mandatory contracts\n\n"
            "PERSONALIZATION:\n"
            "- Read the user's goal carefully and tailor EVERYTHING to it\n"
            "- Choose colors, layout, animations, and content that match the goal's industry/mood\n"
            "- Never use generic placeholder content; create realistic, relevant content\n"
            "- The task description will contain specific design guidance — follow it closely\n\n"
            "HOW TO DELIVER THE FINAL HTML:\n"
            "Option A (preferred): Use file_ops write to save directly:\n"
            "  file_ops({\"action\": \"write\", \"path\": \"/tmp/evermind_output/index.html\", \"content\": \"<full HTML>\"})\n"
            "Option B: Return the COMPLETE HTML inside a single ```html code block in your text response.\n"
            "Either way, the full HTML MUST appear — never just describe what you would build.\n\n"
            "TOOL DISCIPLINE:\n"
            "- For new projects, call file_ops write directly; do NOT spend turns on list/read research\n"
            "- Use at most one quick list/read if you must inspect an existing file before editing\n\n"
            "QUALITY BAR:\n"
            "- Must look professional and premium, never like a student project\n"
            "- Must be fully viewable without external build tools\n"
            "- Must include concrete content, clear hierarchy, and visible polish rather than a bare scaffold\n"
            "- Self-check before finishing: remove placeholders, confirm core interactions work, confirm the page is commercially credible"
        ),
    },
    "tester": {
        "instructions": (
            "You are a QA engineer. Verify generated websites structurally and VISUALLY.\n"
            "\n"
            "STEP 1 — Structural check:\n"
            "  Call file_ops with {\"action\": \"list\", \"path\": \"/tmp/evermind_output/\"}\n"
            "  Then read the main HTML file to verify DOCTYPE, html, head, body tags.\n"
            "\n"
            "STEP 2 — Visual browser test (MANDATORY):\n"
            "  Call browser with {\"action\": \"navigate\", \"url\": \"http://127.0.0.1:8765/preview/\", \"full_page\": true}\n"
            "  If page is blank, try subdirectory: {\"action\": \"navigate\", \"url\": \"http://127.0.0.1:8765/preview/task_1/index.html\", \"full_page\": true}\n"
            "  Immediately call browser with {\"action\": \"observe\"} to inspect visible controls before interacting.\n"
            "  The browser tool returns a screenshot automatically. Analyze it for:\n"
            "  - Layout: are sections visible and properly spaced?\n"
            "  - Colors: is the color scheme consistent and professional?\n"
            "  - Typography: are fonts readable and hierarchy clear?\n"
            "  - Images: are there broken image placeholders?\n"
            "  - Mobile: is there responsive design evidence?\n"
            "\n"
            "STEP 3 — Scroll test:\n"
            "  Call browser with {\"action\": \"scroll\", \"direction\": \"down\", \"amount\": 500}\n"
            "  Check if below-the-fold content exists and renders correctly.\n"
            "\n"
            "STEP 4 — Interaction test (MANDATORY when interactive UI exists):\n"
            "  Prefer browser act with semantic targets for buttons, forms, or controls; fall back to direct click/fill only when needed.\n"
            "  After interaction, you MUST call browser wait_for or observe to verify the page state actually changed.\n"
            "  A PASS verdict is invalid if post-action verification evidence is missing.\n"
            "  If the product is a GAME, you MUST click the start/play button and use browser press actions\n"
            "  with keys such as ArrowUp, ArrowDown, ArrowLeft, ArrowRight, KeyW, KeyA, KeyS, KeyD, Space, or Enter.\n"
            "  Prefer browser press_sequence for games so multiple inputs are tested in one run.\n"
            "  PASS only if the game is actually playable after those inputs and the state hash or visible HUD changes.\n"
            "  FAIL if browser diagnostics report console/page runtime errors.\n"
            "\n"
            "OUTPUT: {\"status\": \"pass\"/\"fail\", \"visual_score\": 1-10, \"issues\": [...], \"screenshot\": \"taken\"}\n"
            "IMPORTANT: Do NOT skip the browser step. You MUST navigate to the preview URL.\n"
        ),
    },
    "reviewer": {
        "instructions": (
            "You are a STRICT quality gatekeeper reviewing web artifacts.\n"
            "Your job is to decide: APPROVED (ship it) or REJECTED (builder must redo).\n\n"
            "VISUAL REVIEW (MANDATORY):\n"
            "1. Use browser tool → navigate to http://127.0.0.1:8765/preview/\n"
            "2. Call browser observe to inspect visible controls and current state\n"
            "3. Take a full-page screenshot\n"
            "4. Scroll down 500px, take another screenshot\n"
            "5. If the artifact is interactive, you MUST use browser act for the main interaction test whenever possible.\n"
            "   After interaction, you MUST call browser wait_for or browser observe before approval.\n"
            "   If the artifact is a GAME, you MUST click the start/play UI and use browser press actions\n"
            "   with gameplay keys (Arrow keys / WASD / Space / Enter) before approving it.\n"
            "   Prefer press_sequence for games and verify the page state changes after gameplay input.\n"
            "6. Reject if browser diagnostics show runtime errors, if post-action verification is missing, or if the post-action state looks unchanged.\n"
            "You MUST use the browser tool — do NOT skip. No excuses.\n\n"
            "SCORE EACH DIMENSION (1-10):\n"
            "- layout: spacing, alignment, visual hierarchy, section flow\n"
            "- color: palette harmony, contrast, dark/light consistency\n"
            "- typography: font choice, size scale, line height, readability\n"
            "- animation: hover effects, transitions, scroll reveals, micro-interactions\n"
            "- responsive: mobile-friendly, no horizontal scroll, touch-ready\n"
            "- functionality: core interactions really work after you test them\n"
            "- completeness: no blank sections, thin placeholders, or unfinished modules\n"
            "- originality: not generic, not commercially weak, not template-looking\n\n"
            "HARD REJECTION RULES:\n"
            "- Reject if emoji glyphs are used as icons, bullets, CTA ornaments, or fake illustrations\n"
            "- Reject if the page feels generic, unfinished, or commercially weak even if it technically works\n\n"
            "VERDICT RULES:\n"
            "- Average score ≥ 7 → APPROVED\n"
            "- Average score < 7 → REJECTED (builder must fix and resubmit)\n"
            "- Any single dimension < 5 → auto REJECTED\n"
            "- Any functionality/completeness/originality score < 6 → REJECTED\n"
            "- APPROVED is invalid if blocking_issues or required_changes are non-empty\n\n"
            "OUTPUT FORMAT (strict JSON):\n"
            '{"verdict": "APPROVED" or "REJECTED", '
            '"scores": {"layout": N, "color": N, "typography": N, "animation": N, "responsive": N, "functionality": N, "completeness": N, "originality": N}, '
            '"ship_readiness": N, '
            '"average": N.N, '
            '"issues": ["specific issue 1", "specific issue 2"], '
            '"blocking_issues": ["what prevents approval"], '
            '"missing_deliverables": ["missing artifact / missing section / missing interaction"], '
            '"required_changes": ["exact builder changes"], '
            '"acceptance_criteria": ["how the resubmission will pass"], '
            '"strengths": ["what is already strong enough to preserve"]}\n\n'
            "Be STRICT. A professional product must score ≥ 7 average.\n"
            "Generic/student-quality work should be REJECTED.\n"
        ),
    },
    "deployer": {
        "instructions": (
            "You are a local deployment specialist. Your job is to confirm that generated files\n"
            "are ready for preview via the local server.\n"
            "\n"
            "STEPS:\n"
            "1. Use file_ops with action='list' on /tmp/evermind_output/ to find generated files\n"
            "2. Identify the main HTML file (usually index.html)\n"
            "3. Report the local preview URL: http://127.0.0.1:8765/preview/<folder>/index.html\n"
            "\n"
            "OUTPUT FORMAT:\n"
            "{\"status\": \"deployed\", \"preview_url\": \"http://127.0.0.1:8765/preview/task_X/index.html\", \"files\": [\"...\"]}\n"
            "\n"
            "IMPORTANT: Do NOT attempt to deploy to GitHub Pages, Netlify, or any external service.\n"
            "The files are served locally through the built-in preview server.\n"
        ),
    },
    "debugger": {
        "instructions": (
            "You are a debugging expert. Analyze error messages and failed tests.\n"
            "Identify the root cause, map the failing path through the relevant code, and produce the smallest coherent fix.\n"
            "When an existing repository context is injected, use the repo map to choose files deliberately instead of wandering.\n"
            "Use file_ops to inspect and edit files, and use shell to validate fixes when commands are available."
        ),
    },
    "analyst": {
        "instructions": (
            "You are a research analyst for product, UX, and design tasks.\n"
            "When the task asks for references, inspiration, competitors, trends, or design analysis, "
            "you MUST use the browser tool to inspect live websites.\n"
            "Prefer browser observe/extract for initial inspection, and use browser act only when interaction is required.\n"
            "Visit at least 2 distinct URLs, and prefer 3 when possible.\n"
            "If a site is blocked by captcha, login wall, or bot-detection, skip it immediately and try another URL.\n"
            "Always include the visited URLs in your final report before the analysis summary.\n"
            "Do not stop after a single site unless the task explicitly forbids browsing.\n"
            "For game tasks, do NOT browse playable web games as your main research flow. Prefer GitHub repos, source code, tutorials, docs, devlogs, and postmortems.\n"
            "You are optimizing the next nodes' execution quality, not writing a vague inspiration memo.\n"
            "Prefer a mixed evidence set: source repo(s), technical docs/tutorials, and visual/product references.\n"
            "Translate research into concrete downstream constraints, implementation advice, and review criteria.\n"
            "Your report is not freeform. It must contain downstream execution handoffs using exact XML tags:\n"
            "<reference_sites>, <design_direction>, <non_negotiables>, <deliverables_contract>, <risk_register>, "
            "<builder_1_handoff>, <builder_2_handoff>, <reviewer_handoff>, "
            "<tester_handoff>, <debugger_handoff>.\n"
            "Use those tags to optimize the next nodes' prompts. "
            "Enforce a premium quality bar and explicitly ban emoji glyphs inside generated pages."
        ),
    },
    "scribe": {
        "instructions": (
            "You are a technical writer. Create clear documentation, guides, reports, and structured explainers.\n"
            "Prefer strong information architecture, concise headings, examples, and checklists over filler prose."
        ),
    },
    "uidesign": {
        "instructions": (
            "You are a senior UI designer. Produce design-direction output that is concrete enough for implementation.\n"
            "Define hierarchy, layout rhythm, motion intent, component behavior, and visual consistency.\n"
            "If browser or screenshot tools are available, inspect references before finalizing decisions."
        ),
    },
    "imagegen": {
        "instructions": (
            "You are an image-generation art director. Produce production-ready prompt packs, shot lists, and visual constraints.\n"
            "If the comfyui plugin is available, call it to check health first and generate assets when the pipeline is configured.\n"
            "If no image tool is attached or the generation backend is unavailable, return highly-usable prompt variants, negative prompts,\n"
            "style lock notes, and fallback illustration guidance that builder/spritesheet can execute."
        ),
    },
    "spritesheet": {
        "instructions": (
            "You are a game asset pipeline specialist. Plan sprite families, frame states, palette constraints, and export layout.\n"
            "Output should be packaging-ready, not just a loose art wish list."
        ),
    },
    "assetimport": {
        "instructions": (
            "You are an asset pipeline coordinator. Organize imported assets, naming, folder structure, usage mapping, and handoff notes.\n"
            "Favor clean manifests and production readiness."
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
        self._setup_litellm()

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

    def _max_tokens_for_node(self, node_type: str) -> int:
        # Builder often returns full HTML/CSS/JS; keep a higher budget.
        if node_type == "builder":
            return self._read_int_env("EVERMIND_BUILDER_MAX_TOKENS", 8192, 2048, 16384)
        return self._read_int_env("EVERMIND_MAX_TOKENS", 4096, 1024, 16384)

    def _timeout_for_node(self, node_type: str) -> int:
        if node_type == "builder":
            return self._read_int_env("EVERMIND_BUILDER_TIMEOUT_SEC", 180, 30, 600)
        if node_type in ("planner", "planner_degraded"):
            # P0-1: Planner should finish fast — only outputs skeleton, not full content.
            return self._read_int_env("EVERMIND_PLANNER_TIMEOUT_SEC", 60, 20, 180)
        return self._read_int_env("EVERMIND_TIMEOUT_SEC", 90, 30, 600)

    def _stream_stall_timeout_for_node(self, node_type: str) -> int:
        """
        Max allowed gap between streamed chunks before we treat the call as stalled.
        Builder can reasonably take longer before first meaningful chunk.
        Planner uses a short stall window — if it goes silent, it's stuck.
        """
        if node_type == "builder":
            return self._read_int_env("EVERMIND_BUILDER_STREAM_STALL_SEC", 300, 60, 600)
        if node_type in ("planner", "planner_degraded"):
            # P0-2: Short stall window for planner — fail fast if stuck.
            return self._read_int_env("EVERMIND_PLANNER_STREAM_STALL_SEC", 45, 15, 120)
        return self._read_int_env("EVERMIND_STREAM_STALL_SEC", 180, 30, 300)

    def _max_tool_iterations_for_node(self, node_type: str) -> int:
        if node_type == "builder":
            return self._read_int_env("EVERMIND_BUILDER_MAX_TOOL_ITERS", 8, 1, 20)
        if node_type in ("reviewer", "tester"):
            return self._read_int_env("EVERMIND_QA_MAX_TOOL_ITERS", 8, 4, 12)
        if node_type == "analyst":
            return self._read_int_env("EVERMIND_ANALYST_MAX_TOOL_ITERS", 4, 2, 10)
        return self._read_int_env("EVERMIND_DEFAULT_MAX_TOOL_ITERS", 3, 1, 10)

    def _analyst_browser_call_limit(self) -> int:
        return self._read_int_env("EVERMIND_ANALYST_MAX_BROWSER_CALLS", 3, 0, 10)

    def _should_block_browser_call(self, node_type: str, tool_call_stats: Dict[str, int]) -> bool:
        if node_type != "analyst":
            return False
        limit = self._analyst_browser_call_limit()
        if limit < 0:
            return False
        current = int(tool_call_stats.get("browser", 0) or 0)
        return current >= limit

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

    def _tool_result_has_write(self, result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        if bool(result.get("written")):
            return True
        data = result.get("data")
        return isinstance(data, dict) and bool(data.get("written"))

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

    def _classify_task_type(self, prompt_source: str) -> str:
        source = str(prompt_source or "").strip()
        if not source:
            return "website"
        try:
            return task_classifier.classify(source).task_type
        except Exception:
            return "website"

    def _review_browser_followup_reason(
        self,
        node_type: str,
        task_type: str,
        browser_actions: List[Dict[str, Any]],
    ) -> Optional[str]:
        if node_type not in {"reviewer", "tester"}:
            return None

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
            if action == "act" and subaction:
                return subaction
            return action

        actions = [_normalized_action(item) for item in successful]
        if "snapshot" not in actions:
            return "You must inspect the page with browser.observe or browser.snapshot before final verdict."

        seen_interaction = False
        has_post_verify = False
        for item in successful:
            action = _normalized_action(item)
            if action in {"click", "fill", "press", "press_sequence"}:
                seen_interaction = True
                continue
            if seen_interaction and action in {"snapshot", "wait_for"}:
                has_post_verify = True
                break

        if task_type == "game":
            if "click" not in actions:
                return "You must click the start/play control before final verdict."
            if not any(action in {"press", "press_sequence"} for action in actions):
                return "You must test gameplay controls with press_sequence or press before final verdict."
            if not has_post_verify and not any(
                bool(item.get("state_changed", False))
                for item in successful
                if str(item.get("action") or "").strip().lower() in {"press", "press_sequence"}
            ):
                return "You must verify the visible game state changed after gameplay input."
            return None

        if task_type == "dashboard":
            if "click" not in actions:
                return "You must click at least one dashboard control before final verdict."
            if not has_post_verify:
                return "After clicking a dashboard control, you must call wait_for or snapshot to verify the changed state."
            return None

        if task_type == "website":
            if node_type == "reviewer" and "scroll" not in actions:
                return "Website reviews must scroll the page before final verdict."
            if not any(action in {"click", "fill"} for action in actions):
                return "You must click or fill at least one interactive element before final verdict."
            if not has_post_verify:
                return "After interacting with the website, you must call wait_for or snapshot to verify the changed state."
            return None

        if not any(action in {"click", "fill", "press", "press_sequence"} for action in actions):
            return "You must test at least one interactive control before final verdict."
        if not has_post_verify:
            return "After interaction, you must call wait_for or snapshot to verify the changed state."
        return None

    def _review_browser_followup_message(self, reason: str, task_type: str) -> str:
        reason_text = str(reason or "").strip()
        lower = reason_text.lower()
        action_hint = 'Call browser with {"action":"observe"} now.'
        if "scroll" in lower:
            action_hint = 'Call browser with {"action":"scroll","direction":"down","amount":500} now, then continue testing.'
        elif "start/play" in lower:
            action_hint = 'Call browser with {"action":"act","intent":"click","target":"Start Game"} or the visible play control now.'
        elif "gameplay controls" in lower:
            action_hint = (
                'Call browser with {"action":"act","intent":"press_sequence","keys":["ArrowRight","ArrowRight","Space","ArrowLeft"],'
                '"repeat":2,"interval_ms":150} now.'
            )
        elif "wait_for or snapshot" in lower or "browser.observe" in lower or "changed state" in lower:
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

    def _prepare_messages_for_request(self, messages: List[Dict[str, Any]], model_name: str) -> List[Dict[str, Any]]:
        """
        Normalize + compact replay messages before model call.
        Prevents context/token explosion in long tool loops.
        """
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
                        args = fn_copy.get("arguments")
                        if isinstance(args, str):
                            fn_copy["arguments"] = self._truncate_text(args, MAX_TOOL_ARGS_REPLAY_CHARS)
                        tc_copy["function"] = fn_copy
                    compact_calls.append(tc_copy)
                normalized["tool_calls"] = compact_calls

            prepared.append(normalized)

        original_total = sum(self._message_char_count(m) for m in prepared)
        if original_total <= MAX_REQUEST_TOTAL_CHARS:
            return prepared

        # Compact older context first, preserve the most recent turns.
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

        compact_total = sum(self._message_char_count(m) for m in prepared)
        if compact_total > MAX_REQUEST_TOTAL_CHARS:
            # Final safety clamp: shrink non-critical messages proportionally.
            per_msg_budget = max(256, MAX_REQUEST_TOTAL_CHARS // max(1, len(prepared)))
            for idx, msg in enumerate(prepared):
                if idx < 2:
                    continue
                if isinstance(msg.get("content"), str):
                    msg["content"] = self._truncate_text(msg["content"], per_msg_budget)
                if isinstance(msg.get("reasoning_content"), str):
                    msg["reasoning_content"] = self._truncate_text(msg["reasoning_content"], min(512, per_msg_budget // 2))
            compact_total = sum(self._message_char_count(m) for m in prepared)

        logger.warning(
            "Context compacted for model=%s chars=%s->%s messages=%s",
            model_name,
            original_total,
            compact_total,
            len(prepared),
        )
        return prepared

    def _builder_web_research_enabled(self) -> bool:
        return is_builder_browser_enabled(config=self.config)

    def _compose_system_prompt(
        self,
        node: Dict[str, Any],
        plugins: Optional[List[Plugin]] = None,
        input_data: str = "",
    ) -> str:
        node_type = node.get("type", "builder")
        preset = AGENT_PRESETS.get(node_type, {})
        base_prompt = node.get("prompt") or preset.get("instructions", "You are a helpful assistant.")
        prompt_source = str(input_data or node.get("goal") or node.get("task") or "").strip()
        skill_block = build_skill_context(node_type, prompt_source)
        skill_names = resolve_skill_names_for_goal(node_type, prompt_source)
        repo_context = build_repo_context(node_type, prompt_source, self.config)
        skill_contract = ""
        if skill_names:
            skill_contract = (
                "\nACTIVE SKILL CHECKLIST:\n"
                + "\n".join(f"- {name}" for name in skill_names)
                + "\nYou MUST apply every loaded skill as part of the acceptance criteria.\n"
            )
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
                if is_image_generation_available(config=self.config):
                    backend_hint = (
                        "\nIMAGE BACKEND STATUS:\n"
                        "- Configured image backend detected\n"
                        "- Health-check the comfyui plugin first, then generate concrete assets if the run requires final art files\n"
                    )
                else:
                    backend_hint = (
                        "\nIMAGE BACKEND STATUS:\n"
                        "- No configured image backend detected in runtime settings\n"
                        "- Do NOT pretend to have raster generation\n"
                        "- Return production-ready prompt packs, negative prompts, style locks, and replacement guidance instead\n"
                    )
            if skill_block:
                return f"{base_prompt}{backend_hint}\n\nLOADED NODE SKILLS:\n{skill_block}{skill_contract}{repo_block}"
            return base_prompt + backend_hint + repo_block

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
                return f"{base_prompt}\n\nLOADED NODE SKILLS:\n{skill_block}{skill_contract}{repo_block}"
            return base_prompt + repo_block

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
            return f"{base_prompt}\n\nLOADED NODE SKILLS:\n{skill_block}{skill_contract}{mode_hint}"
        return base_prompt + mode_hint

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

        # Map provider → env var name
        provider_key_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GEMINI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "kimi": "KIMI_API_KEY",
            "qwen": "QWEN_API_KEY",
        }
        env_key = provider_key_map.get(provider)
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

    # ─────────────────────────────────────────
    # Main dispatch
    # ─────────────────────────────────────────
    async def execute(self, node: Dict, plugins: List[Plugin], input_data: str,
                      model: str = "gpt-5.4", on_progress: Callable = None,
                      privacy_settings: Dict = None) -> Dict:
        node_type = node.get("type", "")
        node_model = node.get("model", model)

        # ── Privacy: mask PII before sending to AI ──
        masker = get_masker(privacy_settings) if privacy_settings else get_masker()
        masked_input, restore_map = masker.mask(input_data, node_type=node_type)
        if restore_map and on_progress:
            await on_progress({"stage": "privacy_masked", "pii_count": len(restore_map)})

        # Resolve model info (static registry + relay)
        model_info = self._resolve_model(node_model)

        # ── Pre-flight: check API key before calling LLM ──
        key_error = self._check_api_key(node_model, model_info)
        if key_error:
            # Auto-fallback for workflow nodes when default model key is missing.
            # This keeps execute_workflow usable even if only non-OpenAI keys are configured.
            fallback_order = [
                "kimi-coding",
                "kimi-k2.5",
                "deepseek-v3",
                "gemini-2.5-pro",
                "claude-4-sonnet",
                "qwen-max",
                "gpt-4o",
            ]
            for fallback_model in fallback_order:
                if fallback_model == node_model:
                    continue
                fallback_info = self._resolve_model(fallback_model)
                fallback_error = self._check_api_key(fallback_model, fallback_info)
                if not fallback_error:
                    logger.info(f"Model auto-fallback: {node_model} -> {fallback_model}")
                    if on_progress:
                        await on_progress({
                            "stage": "system_info",
                            "message": f"🔄 自动切换模型: {node_model} -> {fallback_model}",
                        })
                    node_model = fallback_model
                    model_info = fallback_info
                    key_error = None
                    break
        if key_error:
            logger.warning(f"API key missing for model {node_model}: {key_error[:80]}")
            if on_progress:
                await on_progress({"stage": "error", "message": key_error})
            return {"success": False, "output": "", "error": key_error}

        # ── Execute with retry ──
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    wait = 2 ** attempt
                    logger.info(f"Retry {attempt}/{max_retries} for {node_model}, waiting {wait}s...")
                    if on_progress:
                        await on_progress({"stage": "retrying", "attempt": attempt, "wait": wait})
                    await asyncio.sleep(wait)

                # Path 1: CUA mode
                if model_info.get("supports_cua") and any(p.name == "computer_use" for p in plugins):
                    result = await self._execute_cua_loop(node, plugins, masked_input, on_progress)
                # Path 2: Relay endpoint
                elif model_info.get("provider") == "relay":
                    result = await self._execute_relay(node, masked_input, model_info, on_progress)
                # Path 2.5: Models with extra_headers (Kimi Coding — needs direct OpenAI SDK)
                elif model_info.get("extra_headers"):
                    result = await self._execute_openai_compatible(node, masked_input, model_info, on_progress, plugins=plugins)
                # Path 3: LiteLLM with tools
                elif self._litellm and model_info.get("supports_tools") and plugins:
                    result = await self._execute_litellm_tools(node, plugins, masked_input, model_info, on_progress)
                # Path 4: LiteLLM direct chat
                elif self._litellm:
                    result = await self._execute_litellm_chat(node, masked_input, model_info, on_progress)
                # Fallback: direct OpenAI
                else:
                    result = await self._execute_openai_direct(node, plugins, masked_input, on_progress)

                if result.get("success"):
                    # ── Track usage ──
                    try:
                        from settings import get_usage_tracker
                        tracker = get_usage_tracker()
                        usage = self._normalize_usage(result.get("usage", {}))
                        tracker.record(
                            model=result.get("model", node_model),
                            prompt_tokens=usage.get("prompt_tokens", 0),
                            completion_tokens=usage.get("completion_tokens", 0),
                            cost=float(result.get("cost", 0) or 0),
                            provider=model_info.get("provider", "unknown"),
                            mode=result.get("mode", "unknown"),
                        )
                    except Exception:
                        pass
                    break  # Success, exit retry loop
                else:
                    last_error = result.get("error", "Unknown error")
                    # Don't retry on non-retryable errors
                    if "api key" in last_error.lower() or "auth" in last_error.lower():
                        break

            except Exception as e:
                last_error = str(e)
                error_lower = last_error.lower()
                logger.warning(f"Execute attempt {attempt+1} failed: {_sanitize_error(last_error[:200])}")

                # ── Don't retry on authentication / permission errors ──
                is_auth_error = any(kw in error_lower for kw in [
                    "auth", "api key", "api_key", "invalid key", "permission",
                    "unauthorized", "forbidden", "401", "403",
                ])
                if is_auth_error:
                    friendly = (
                        f"API 密钥无效或已过期，请在「设置」中重新填入正确的密钥。\n"
                        f"API key invalid or expired. Please update it in Settings.\n"
                        f"({_sanitize_error(last_error[:100])})"
                    )
                    result = {"success": False, "output": "", "error": friendly}
                    break

                result = {"success": False, "output": "", "error": _sanitize_error(last_error)}

        # ── Privacy: unmask PII in AI response ──
        if restore_map and result.get("output"):
            result["output"] = masker.unmask(result["output"], restore_map)
            result["privacy_masked"] = len(restore_map)

        return result

    # ─────────────────────────────────────────
    # Path: Relay Endpoint
    # ─────────────────────────────────────────
    async def _execute_relay(self, node, input_data, model_info, on_progress) -> Dict:
        """Execute through a proxy/relay endpoint."""
        relay_mgr = get_relay_manager()
        relay_id = model_info.get("relay_id")
        model_name = model_info["litellm_id"].replace("openai/", "")

        system_prompt = self._compose_system_prompt(node, input_data=input_data)

        if on_progress:
            await on_progress({"stage": "calling_relay", "relay": model_info.get("relay_name", "?"), "model": model_name})

        result = await relay_mgr.call(
            endpoint_id=relay_id,
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_data},
            ],
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
            await on_progress({"stage": "cua_start", "instruction": input_data[:100]})

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
                    await on_progress({"stage": "cua_iteration", "iteration": iteration, "max": max_iterations})
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
                            await on_progress({"stage": "cua_action", "action": getattr(action, "type", "unknown"), "iteration": iteration})
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

    def _compact_partial_output(self, text: Any, limit: int = 3200) -> str:
        preview = str(text or "").strip()
        if not preview:
            return ""
        if len(preview) <= limit:
            return preview
        head = max(1200, min(1800, limit // 2))
        tail = max(900, limit - head - 8)
        return f"{preview[:head]}\n...\n{preview[-tail:]}"

    def _build_partial_output_event(self, text: Any, phase: str = "drafting") -> Optional[Dict[str, Any]]:
        preview = self._compact_partial_output(text)
        if not preview:
            return None
        return {
            "stage": "partial_output",
            "phase": phase,
            "preview": preview,
            "source": "model",
        }

    async def _publish_partial_output(self, on_progress, text: Any, phase: str = "drafting") -> None:
        if not on_progress:
            return
        event = self._build_partial_output_event(text, phase=phase)
        if event:
            await on_progress(event)

    # ─────────────────────────────────────────
    # Path 2.5: Direct OpenAI-compatible SDK (for APIs needing custom headers, e.g. Kimi Coding)
    #           Now supports tool calling for file_ops, shell, etc.
    # ─────────────────────────────────────────
    async def _execute_openai_compatible(self, node, input_data, model_info, on_progress, plugins=None) -> Dict:
        """Execute via OpenAI SDK directly with custom default_headers (bypasses LiteLLM).
        Now supports tool calling so AI can use file_ops/shell plugins."""
        from openai import OpenAI

        node_type = node.get("type", "builder")
        system_prompt = self._compose_system_prompt(node, plugins=plugins, input_data=input_data)
        model_name = model_info["litellm_id"].replace("openai/", "")
        max_tokens = self._max_tokens_for_node(node_type)
        timeout_sec = self._timeout_for_node(node_type)
        max_continuations = self._read_int_env("EVERMIND_MAX_CONTINUATIONS", 2, 0, 5)

        # Get API key
        api_key_env = {
            "kimi": "KIMI_API_KEY", "qwen": "QWEN_API_KEY"
        }.get(model_info.get("provider"))
        api_key = None
        if api_key_env:
            api_key = self.config.get(api_key_env.lower()) or os.getenv(api_key_env)

        if not api_key:
            return {"success": False, "output": "", "error": f"API key not configured for {model_info.get('provider')}"}

        if on_progress:
            await on_progress({"stage": "calling_ai", "model": model_name, "mode": "openai_compatible"})

        # Build tools from plugins
        tools = []
        if plugins:
            for p in plugins:
                if p.name != "computer_use":
                    defn = p.get_tool_definition()
                    tools.append({"type": "function", "function": defn} if "function" not in defn else defn)

        try:
            client = OpenAI(
                api_key=api_key,
                base_url=model_info.get("api_base"),
                default_headers=model_info.get("extra_headers", {}),
                timeout=timeout_sec,
            )
            loop = asyncio.get_running_loop()

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_data},
            ]

            # Streaming stall timeout: cancel when the stream stops producing chunks.
            stall_timeout = self._stream_stall_timeout_for_node(node_type)

            def _call_streaming(msgs, tls):
                """Make API call with streaming to detect stalls early."""
                prepared_msgs = self._prepare_messages_for_request(msgs, model_name)
                kwargs = {
                    "model": model_name,
                    "messages": prepared_msgs,
                    "max_tokens": max_tokens,
                    "stream": True,
                    # Enforce read-timeout at transport layer so blocked streams fail fast.
                    "timeout": stall_timeout,
                }
                if tls:
                    kwargs["tools"] = tls
                    kwargs["tool_choice"] = "auto"
                if model_info.get("provider") == "kimi":
                    if os.getenv("EVERMIND_KIMI_THINKING", "disabled").lower() != "enabled":
                        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                if os.getenv("EVERMIND_DEBUG_KIMI_CALLS"):
                    print(
                        f"KIMI_DEBUG_CALL model={model_name} msg_count={len(prepared_msgs)} "
                        f"tools={bool(tls)} stream=True",
                        flush=True,
                    )
                self._debug_log_tool_messages(prepared_msgs, model_name)
                stream = client.chat.completions.create(**kwargs)

                # Collect streamed chunks with stall detection
                content_parts = []
                tool_calls_map: Dict[int, Dict] = {}
                finish_reason = None
                usage_data = None
                last_chunk_time = time.time()
                last_preview_emit = 0.0

                for chunk in stream:
                    now = time.time()
                    if now - last_chunk_time > stall_timeout:
                        raise TimeoutError(f"Stream stalled: no chunk for {stall_timeout}s")
                    last_chunk_time = now

                    if not chunk.choices:
                        # Usage-only final chunk
                        if hasattr(chunk, "usage") and chunk.usage:
                            usage_data = chunk.usage
                        continue

                    delta = chunk.choices[0].delta
                    if delta.content:
                        content_parts.append(delta.content)
                        if on_progress and (now - last_preview_emit >= 0.75):
                            event = self._build_partial_output_event("".join(content_parts), phase="streaming")
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
                                if tc.function.name:
                                    entry["function"]["name"] = tc.function.name
                                if tc.function.arguments:
                                    entry["function"]["arguments"] += tc.function.arguments
                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason

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

            response = await asyncio.to_thread(_call_streaming, messages, tools)

            await self._publish_partial_output(
                on_progress,
                getattr(response.choices[0].message, "content", "") or "",
                phase="sectioning" if node_type == "planner" else "drafting",
            )

            output_text = ""
            tool_results = []
            tool_call_stats: Dict[str, int] = {}
            iteration = 0
            max_iterations = self._max_tool_iterations_for_node(node_type)
            continuation_count = 0
            usage_totals = self._normalize_usage(getattr(response, "usage", None))
            builder_non_write_streak = 0
            builder_force_text_early = False
            builder_force_reason = ""
            builder_force_threshold = self._read_int_env("EVERMIND_BUILDER_FORCE_TEXT_STREAK", 3, 2, 20)
            qa_followup_count = 0
            qa_task_type = self._classify_task_type(input_data)
            browser_action_events: List[Dict[str, Any]] = []

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
                    followup_reason = self._review_browser_followup_reason(
                        node_type,
                        qa_task_type,
                        browser_action_events,
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
                            await on_progress({
                                "stage": "qa_followup",
                                "message": followup_reason,
                                "continuation": qa_followup_count,
                            })
                        response = await asyncio.to_thread(_call_streaming, messages, tools)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        continue
                    if msg_content:
                        output_text += msg_content
                        await self._publish_partial_output(on_progress, output_text, phase="drafting")
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
                            await on_progress({
                                "stage": "continuing",
                                "reason": "length_truncated",
                                "continuation": continuation_count,
                            })
                        response = await asyncio.to_thread(_call_streaming, messages, tools)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        continue
                    break

                # Process tool calls
                messages.append(self._serialize_assistant_message(msg))
                processed_calls = 0
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

                    if tc_type != "function" or not fn_name:
                        logger.warning("Skipping unsupported tool call payload: type=%s id=%s", tc_type, tc_id)
                        continue

                    if on_progress:
                        await on_progress({"stage": "executing_plugin", "plugin": fn_name})
                    if fn_name == "browser" and self._should_block_browser_call(node_type, tool_call_stats):
                        limit = self._analyst_browser_call_limit()
                        result = {
                            "success": False,
                            "data": {},
                            "error": (
                                f"Analyst browser call limit reached ({limit}). "
                                "Skip additional browsing and summarize from collected insights."
                            ),
                            "artifacts": [],
                        }
                    else:
                        result = await self._run_plugin(fn_name, fn_args, plugins or [], node_type=node_type)
                    tool_results.append(result)
                    tool_call_stats[fn_name] = tool_call_stats.get(fn_name, 0) + 1
                    parsed_args = self._safe_json_object(fn_args)
                    tool_action = (
                        self._infer_file_ops_action(fn_args, result)
                        if fn_name == "file_ops"
                        else str(parsed_args.get("action", "")).strip().lower()
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
                            await on_progress({
                                "stage": "browser_action",
                                "plugin": "browser",
                                **browser_event,
                            })
                    if node_type == "builder":
                        wrote_file = fn_name == "file_ops" and self._tool_result_has_write(result)
                        if wrote_file:
                            builder_non_write_streak = 0
                        else:
                            # Count all non-write tool turns so malformed file_ops args
                            # cannot bypass the loop guard.
                            builder_non_write_streak += 1
                    # Truncate tool output to prevent token overflow (Kimi 262K limit)
                    result_str = json.dumps(result)
                    if len(result_str) > MAX_TOOL_RESULT_CHARS:
                        result_str = result_str[:MAX_TOOL_RESULT_CHARS] + '... [TRUNCATED]'
                    messages.append({"role": "tool", "tool_call_id": tc_id, "content": result_str})
                    processed_calls += 1

                if msg.tool_calls and processed_calls == 0:
                    raise ValueError("No supported function tool calls were produced by model response")

                if (
                    node_type == "builder"
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
                        await on_progress({
                            "stage": "builder_loop_guard",
                            "streak": builder_non_write_streak,
                            "threshold": builder_force_threshold,
                            "reason": builder_force_reason,
                        })
                    break

                if on_progress:
                    await on_progress({"stage": "continuing", "iteration": iteration})
                response = await asyncio.to_thread(_call_streaming, messages, tools)
                usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))

            # ── Forced final text-only call for builder ──
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
                    await on_progress({"stage": "forcing_text_output", "reason": force_text_reason})
                messages.append({
                    "role": "user",
                    "content": (
                        "You have used all your tool calls. Now output the COMPLETE HTML code directly as text. "
                        "Start with <!DOCTYPE html> and end with </html>. "
                        "Put it inside a ```html code block. Do NOT describe it — output the full code NOW."
                    ),
                })
                try:
                    final_resp = await asyncio.to_thread(_call_streaming, messages, [])  # No tools
                    final_msg = final_resp.choices[0].message
                    if final_msg.content:
                        output_text += final_msg.content
                        await self._publish_partial_output(on_progress, output_text, phase="finalizing")
                    usage_totals = self._merge_usage(usage_totals, getattr(final_resp, "usage", None))
                    logger.info(f"Forced text output: {len(final_msg.content or '')} chars")
                except Exception as e:
                    logger.warning(f"Forced text-only call failed: {_sanitize_error(str(e))}")

            return {
                "success": True, "output": output_text,
                "model": getattr(response, "model", model_name),
                "tool_results": tool_results, "mode": "openai_compatible",
                "usage": usage_totals,
                "tool_call_stats": tool_call_stats,
            }
        except TimeoutError as e:
            if on_progress:
                await on_progress({"stage": "stream_stalled", "reason": str(e)})
            return {"success": False, "output": "", "error": _sanitize_error(str(e))}
        except Exception as e:
            err = _sanitize_error(str(e))
            if on_progress and ("timed out" in err.lower() or "timeout" in err.lower()):
                await on_progress({"stage": "stream_stalled", "reason": err})
            return {"success": False, "output": "", "error": err}

    # ─────────────────────────────────────────
    # Path 3: LiteLLM with Tool Calling
    # ─────────────────────────────────────────
    async def _execute_litellm_tools(self, node, plugins, input_data, model_info, on_progress) -> Dict:
        node_type = node.get("type", "builder")
        system_prompt = self._compose_system_prompt(node, plugins=plugins, input_data=input_data)
        litellm_model = model_info["litellm_id"]
        max_tokens = self._max_tokens_for_node(node_type)
        timeout_sec = self._timeout_for_node(node_type)

        # Build OpenAI-format tools from plugins
        tools = []
        for p in plugins:
            if p.name != "computer_use":
                defn = p.get_tool_definition()
                tools.append({"type": "function", "function": defn} if "function" not in defn else defn)

        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": input_data}]

        if on_progress:
            await on_progress({"stage": "calling_ai", "model": litellm_model, "tools_count": len(tools)})

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
                env_base_key = {
                    "openai": "OPENAI_API_BASE",
                    "anthropic": "ANTHROPIC_API_BASE",
                    "google": "GEMINI_API_BASE",
                    "deepseek": "DEEPSEEK_API_BASE",
                    "kimi": "KIMI_API_BASE",
                    "qwen": "QWEN_API_BASE",
                }.get(provider, "")
                env_base = os.getenv(env_base_key, "") if env_base_key else ""
                kwargs["api_base"] = env_base if env_base else model_info["api_base"]
            if model_info.get("extra_headers"):
                kwargs["extra_headers"] = model_info["extra_headers"]
            api_key_env = {
                "kimi": "KIMI_API_KEY", "qwen": "QWEN_API_KEY"
            }.get(model_info.get("provider"))
            if api_key_env:
                kwargs["api_key"] = self.config.get(api_key_env.lower()) or os.getenv(api_key_env)

            response = await asyncio.to_thread(self._litellm.completion, **kwargs)

            output_text = ""
            tool_results = []
            tool_call_stats: Dict[str, int] = {}
            iteration = 0
            max_iterations = (
                self._max_tool_iterations_for_node(node_type)
                if node_type == "builder"
                else self._read_int_env("EVERMIND_LITELLM_MAX_TOOL_ITERS", 10, 1, 30)
            )
            usage_totals = self._normalize_usage(getattr(response, "usage", None))
            total_cost = self._estimate_litellm_cost(response, litellm_model)
            builder_non_write_streak = 0
            builder_force_text_early = False
            builder_force_reason = ""
            builder_force_threshold = self._read_int_env("EVERMIND_BUILDER_FORCE_TEXT_STREAK", 3, 2, 20)
            qa_followup_count = 0
            qa_task_type = self._classify_task_type(input_data)
            browser_action_events: List[Dict[str, Any]] = []

            while iteration < max_iterations:
                iteration += 1
                msg = response.choices[0].message
                msg_content = getattr(msg, "content", "") or ""
                if msg.tool_calls and msg_content:
                    output_text += msg.content

                # Check for tool calls
                if not msg.tool_calls:
                    followup_reason = self._review_browser_followup_reason(
                        node_type,
                        qa_task_type,
                        browser_action_events,
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
                            await on_progress({
                                "stage": "qa_followup",
                                "message": followup_reason,
                                "continuation": qa_followup_count,
                            })
                        kwargs["messages"] = self._prepare_messages_for_request(messages, litellm_model)
                        response = await asyncio.to_thread(self._litellm.completion, **kwargs)
                        usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                        total_cost += self._estimate_litellm_cost(response, litellm_model)
                        continue
                    if msg_content:
                        output_text += msg_content
                    break

                messages.append(self._serialize_assistant_message(msg))
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    fn_args = tc.function.arguments
                    if on_progress:
                        await on_progress({"stage": "executing_plugin", "plugin": fn_name})
                    if fn_name == "browser" and self._should_block_browser_call(node_type, tool_call_stats):
                        limit = self._analyst_browser_call_limit()
                        result = {
                            "success": False,
                            "data": {},
                            "error": (
                                f"Analyst browser call limit reached ({limit}). "
                                "Skip additional browsing and summarize from collected insights."
                            ),
                            "artifacts": [],
                        }
                    else:
                        result = await self._run_plugin(fn_name, fn_args, plugins, node_type=node_type)
                    tool_results.append(result)
                    tool_call_stats[fn_name] = tool_call_stats.get(fn_name, 0) + 1
                    parsed_args = self._safe_json_object(fn_args)
                    tool_action = (
                        self._infer_file_ops_action(fn_args, result)
                        if fn_name == "file_ops"
                        else str(parsed_args.get("action", "")).strip().lower()
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
                            await on_progress({
                                "stage": "browser_action",
                                "plugin": "browser",
                                **browser_event,
                            })
                    if node_type == "builder":
                        wrote_file = fn_name == "file_ops" and self._tool_result_has_write(result)
                        if wrote_file:
                            builder_non_write_streak = 0
                        else:
                            # Count all non-write tool turns so malformed file_ops args
                            # cannot bypass the loop guard.
                            builder_non_write_streak += 1
                    # Truncate tool output to prevent token overflow
                    result_str = json.dumps(result)
                    if len(result_str) > MAX_TOOL_RESULT_CHARS:
                        result_str = result_str[:MAX_TOOL_RESULT_CHARS] + '... [TRUNCATED]'
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})

                if (
                    node_type == "builder"
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
                        await on_progress({
                            "stage": "builder_loop_guard",
                            "streak": builder_non_write_streak,
                            "threshold": builder_force_threshold,
                            "reason": builder_force_reason,
                        })
                    break

                if on_progress:
                    await on_progress({"stage": "continuing", "iteration": iteration})
                kwargs["messages"] = self._prepare_messages_for_request(messages, litellm_model)
                response = await asyncio.to_thread(self._litellm.completion, **kwargs)
                usage_totals = self._merge_usage(usage_totals, getattr(response, "usage", None))
                total_cost += self._estimate_litellm_cost(response, litellm_model)

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
                    await on_progress({"stage": "forcing_text_output", "reason": force_text_reason})
                final_messages = list(messages)
                final_messages.append({
                    "role": "user",
                    "content": (
                        "You have used all your tool calls. Now output the COMPLETE HTML code directly as text. "
                        "Start with <!DOCTYPE html> and end with </html>. "
                        "Put it inside a ```html code block. Do NOT describe it — output the full code NOW."
                    ),
                })
                final_kwargs = dict(kwargs)
                final_kwargs.pop("tools", None)
                final_kwargs.pop("tool_choice", None)
                final_kwargs["messages"] = self._prepare_messages_for_request(final_messages, litellm_model)
                final_resp = await asyncio.to_thread(self._litellm.completion, **final_kwargs)
                final_msg = final_resp.choices[0].message
                if final_msg.content:
                    output_text += final_msg.content
                    await self._publish_partial_output(on_progress, output_text, phase="finalizing")
                usage_totals = self._merge_usage(usage_totals, getattr(final_resp, "usage", None))
                total_cost += self._estimate_litellm_cost(final_resp, litellm_model)

            return {"success": True, "output": output_text, "tool_results": tool_results,
                    "model": litellm_model, "iterations": iteration, "mode": "litellm_tools", "usage": usage_totals, "cost": total_cost,
                    "tool_call_stats": tool_call_stats}
        except Exception as e:
            logger.error(f"LiteLLM tools error: {_sanitize_error(str(e))}")
            return {"success": False, "output": "", "error": _sanitize_error(str(e))}

    # ─────────────────────────────────────────
    # Path 3: LiteLLM Direct Chat
    # ─────────────────────────────────────────
    async def _execute_litellm_chat(self, node, input_data, model_info, on_progress) -> Dict:
        system_prompt = self._compose_system_prompt(node, input_data=input_data)
        litellm_model = model_info["litellm_id"]

        if on_progress:
            await on_progress({"stage": "calling_ai", "model": litellm_model, "tools_count": 0})

        try:
            kwargs = {"model": litellm_model, "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_data}
            ], "timeout": 120, "num_retries": 0}
            if model_info.get("api_base"):
                provider = model_info.get("provider", "")
                env_base_key = {
                    "openai": "OPENAI_API_BASE", "anthropic": "ANTHROPIC_API_BASE",
                    "google": "GEMINI_API_BASE", "deepseek": "DEEPSEEK_API_BASE",
                    "kimi": "KIMI_API_BASE", "qwen": "QWEN_API_BASE",
                }.get(provider, "")
                env_base = os.getenv(env_base_key, "") if env_base_key else ""
                kwargs["api_base"] = env_base if env_base else model_info["api_base"]
            if model_info.get("extra_headers"):
                kwargs["extra_headers"] = model_info["extra_headers"]
            api_key_env = {
                "kimi": "KIMI_API_KEY", "qwen": "QWEN_API_KEY"
            }.get(model_info.get("provider"))
            if api_key_env:
                kwargs["api_key"] = self.config.get(api_key_env.lower()) or os.getenv(api_key_env)

            response = await asyncio.to_thread(self._litellm.completion, **kwargs)
            content = response.choices[0].message.content or ""
            await self._publish_partial_output(on_progress, content, phase="drafting")
            return {"success": True, "output": content,
                    "model": litellm_model, "tool_results": [], "mode": "litellm_chat",
                    "usage": self._normalize_usage(getattr(response, "usage", None)),
                    "cost": self._estimate_litellm_cost(response, litellm_model)}
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
    ) -> Dict:
        # Enforce per-node plugin allowlist.
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

        plugin_context = dict(self.config or {})
        force_visible = str(
            plugin_context.get(
                "reviewer_tester_force_headful",
                os.getenv("EVERMIND_REVIEWER_TESTER_FORCE_HEADFUL", "1"),
            )
        ).strip().lower() in ("1", "true", "yes", "on")
        if name == "browser" and force_visible and node_type in ("reviewer", "tester"):
            plugin_context["browser_headful"] = True
            plugin_context["browser_force_reason"] = f"{node_type}_visible_review"

        result = await plugin.execute(parsed, context=plugin_context)
        if result is None:
            return {"error": f"Plugin {name} returned no result"}
        if hasattr(result, "to_dict"):
            return result.to_dict()
        if isinstance(result, dict):
            return result
        return {"ok": True, "data": str(result)}


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
            await on_progress({"stage": "handoff", "from": from_node.get("name", ""), "to": target.get("name", ""), "task": task[:100]})
        from plugins.base import get_default_plugins_for_node
        enabled = target.get("plugins", get_default_plugins_for_node(to_node_type, config=self.ai_bridge.config))
        plugins = [PluginRegistry.get(p) for p in enabled if PluginRegistry.get(p)]
        return await self.ai_bridge.execute(node=target, plugins=plugins,
                                            input_data=f"[Handoff from {from_node.get('name', '?')}] {task}", on_progress=on_progress)
