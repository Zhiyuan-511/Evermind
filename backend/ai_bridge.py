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
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from json_repair import repair_json as _json_repair_fn
except ImportError:
    _json_repair_fn = None
from urllib.parse import urlparse

from plugins.base import (
    Plugin,
    PluginResult,
    PluginRegistry,
    is_builder_browser_enabled,
    is_image_generation_available,
    resolve_enabled_plugins_for_node,
)
from agent_skills import build_skill_context, resolve_skill_names_for_goal
from node_roles import normalize_node_role
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
BUILDER_DIRECT_MULTIFILE_MARKER = "DIRECT MULTI-FILE DELIVERY ONLY."
BUILDER_TARGET_OVERRIDE_MARKER = "HTML TARGET OVERRIDE:"

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

PROVIDER_ENV_KEY_MAP = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "KIMI_API_KEY",
    "qwen": "QWEN_API_KEY",
}

LEGACY_AUTO_FALLBACK_ORDER = [
    "kimi-coding",
    "gpt-5.4",
    "deepseek-v3",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "qwen-max",
    "gpt-4o",
]

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
            "1. Match the requested delivery shape exactly: single-page requests can be one index.html, but explicit multi-page requests must create index.html plus the required linked HTML pages/routes\n"
            "2. Start with <!DOCTYPE html> and end with </html>; no truncation, no placeholders\n"
            "3. Implement responsive design with at least one @media breakpoint\n"
            "4. Include accessibility basics: lang attr, aria labels, focus styles\n"
            "5. Use CSS variables for colors, spacing, and shadows\n"
            "6. Choose typography intentionally for the product category; avoid default-looking font decisions\n"
            "7. Do NOT use emoji characters as UI icons; use inline SVG instead\n"
            "   Treat any emoji glyph in the final page as a hard failure and remove it before finishing\n"
            "8. Smooth animations with cubic-bezier(0.4,0,0.2,1); honor prefers-reduced-motion\n"
            "9. Implement as much code as the task requires; do not compress the result into a low-quality stub\n"
            "10. Treat loaded skills, analyst notes, reviewer acceptance criteria, and task constraints as mandatory contracts\n"
            "11. For new standalone website work, do not spend more than one non-write tool turn before the first real file_ops write\n"
            "12. If multiple pages share one JavaScript file, it MUST run safely on every page: guard queried elements before classList/addEventListener/style access, or keep the DOM contract identical across routes\n"
            "13. Prefer local/system font stacks over remote font CDNs; do not depend on Google Fonts or similar hosts for core rendering\n"
            "14. If a visual slot cannot be filled with a reliable real image, build a finished CSS/SVG composition instead of leaving a blank frame or a giant generic placeholder icon\n\n"
            "15. For editorial/travel/lifestyle pages, destination/package/story cards must feel media-led: do not use bare outline service icons as the main visual treatment for content modules\n\n"
            "17. CSS must be self-contained for preview: use an inline <style> block or a local stylesheet file. Do NOT rely on Tailwind CDN or other remote CSS runtimes as the primary styling path\n\n"
            "PERSONALIZATION:\n"
            "- Read the user's goal carefully and tailor EVERYTHING to it\n"
            "- Choose colors, layout, animations, and content that match the goal's industry/mood\n"
            "- Never use generic placeholder content; create realistic, relevant content\n"
            "- The task description will contain specific design guidance — follow it closely\n\n"
            "HOW TO DELIVER THE FINAL HTML:\n"
            "Option A (preferred): Use file_ops write to save directly.\n"
            "For multi-page delivery, your FIRST substantive move should be real file_ops write calls, not a long prose plan.\n"
            "  file_ops({\"action\": \"write\", \"path\": \"/tmp/evermind_output/index.html\", \"content\": \"<full HTML>\"})\n"
            "  If the brief requires multiple pages/routes, also write the additional linked HTML files one by one with file_ops.\n"
            "  Do NOT fake a multi-page brief as one long scrolling landing page.\n"
            "Option B: Return the final files directly in text ONLY if tool calling is unavailable or a write attempt failed.\n"
            "  - Single-page delivery: one ```html``` code block is fine.\n"
            "  - Multi-page delivery: output one fenced block per file using headers like ```html index.html``` and ```html collections.html```.\n"
            "Either way, the full HTML MUST appear — never just describe what you would build.\n\n"
            "TOOL DISCIPLINE:\n"
            "- For new projects, call file_ops write directly; do NOT spend turns on list/read research\n"
            "- Use at most one quick list/read if you must inspect an existing file before editing\n\n"
            "QUALITY BAR:\n"
            "- Must look professional and premium, never like a student project\n"
            "- Must be fully viewable without external build tools\n"
            "- Must include concrete content, clear hierarchy, and visible polish rather than a bare scaffold\n"
            "- For multi-page work, every requested page must have real content and working navigation, not stubs\n"
            "- Self-check before finishing: remove placeholders, confirm core interactions work, confirm the page is commercially credible"
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
            "- SVG FIX: If any inline <svg> lacks explicit width and height attributes (viewBox alone is NOT enough), add sensible sizes (24-48px for icons, 64-96px for feature illustrations). Unconstrained SVG renders as giant shapes filling the viewport.\n"
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
            "STEP 2 — Visual browser test (MANDATORY unless a [Desktop QA Session Evidence] block is already present in your input):\n"
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
            "  Prefer browser with {\"action\": \"record_scroll\", \"amount\": 500} so the full scrolling evidence is captured as a filmstrip/GIF.\n"
            "  If record_scroll is unavailable, repeatedly call browser with {\"action\": \"scroll\", \"direction\": \"down\", \"amount\": 500}\n"
            "  until the browser reports the bottom of the page or the page is clearly non-scrollable.\n"
            "  Check if below-the-fold content exists and renders correctly.\n"
            "\n"
            "STEP 4 — Interaction test (MANDATORY when interactive UI exists):\n"
            "  Prefer browser act with semantic targets for buttons, forms, or controls; fall back to direct click/fill only when needed.\n"
            "  After interaction, you MUST call browser wait_for or observe to verify the page state actually changed.\n"
            "  A PASS verdict is invalid if post-action verification evidence is missing.\n"
            "  If browser_use is available and the product is a GAME or a click-heavy app, prefer browser_use for the actual multi-step play / interaction session,\n"
            "  then return to browser observe/snapshot so the final visible state is captured explicitly in Evermind artifacts.\n"
            "  If the product is a GAME, you MUST click the start/play button and use browser press actions\n"
            "  with keys such as ArrowUp, ArrowDown, ArrowLeft, ArrowRight, KeyW, KeyA, KeyS, KeyD, Space, or Enter.\n"
            "  Prefer browser press_sequence for games so multiple inputs are tested in one run.\n"
            "  PASS only if the game is actually playable after those inputs and the state hash or visible HUD changes.\n"
            "  FAIL if browser diagnostics report console/page runtime errors.\n"
            "  If a [Desktop QA Session Evidence] block is already present, treat it as the primary gameplay record and do NOT open browser/browser_use unless you need one extra confirmation for a concrete bug.\n"
            "\n"
            "STEP 5 - Multi-page completeness check:\n"
            "  If the brief asks for multiple pages/routes/screens, you MUST visit each requested page via the real navigation links.\n"
            "  PASS is invalid if only the first page was checked.\n"
            "\n"
            "OUTPUT: {\"status\": \"pass\"/\"fail\", \"visual_score\": 1-10, \"issues\": [...], \"screenshot\": \"taken\"}\n"
            "IMPORTANT: Do NOT skip the browser step. You MUST navigate to the preview URL.\n"
        ),
    },
    "reviewer": {
        "instructions": (
            "You are a STRICT quality gatekeeper reviewing web artifacts.\n"
            "Your job is to decide: APPROVED (ship it) or REJECTED (builder must redo).\n\n"
            "VISUAL REVIEW (MANDATORY unless a [Desktop QA Session Evidence] block is already present in your input):\n"
            "1. Use browser tool → navigate to http://127.0.0.1:8765/preview/\n"
            "2. Call browser observe to inspect visible controls and current state\n"
            "3. Take a full-page screenshot\n"
            "4. Prefer browser record_scroll on the homepage and one representative secondary route so the full scrolling evidence is captured continuously; otherwise keep scrolling in ~500px steps until the browser reports the bottom of the page, taking screenshots along the way when helpful\n"
            "5. If the artifact is interactive, you MUST use browser act for the main interaction test whenever possible.\n"
            "   After interaction, you MUST call browser wait_for or browser observe before approval.\n"
            "   If browser_use is available and the artifact is a GAME or a click-heavy interactive product, prefer browser_use for the actual play / multi-step interaction session,\n"
            "   then use browser observe/snapshot immediately after it so Evermind still captures explicit final-state evidence.\n"
            "   If the artifact is a GAME, you MUST click the start/play UI and use browser press actions\n"
            "   with gameplay keys (Arrow keys / WASD / Space / Enter) before approving it.\n"
            "   Prefer press_sequence for games and verify the page state changes after gameplay input.\n"
            "6. If the brief asks for multiple pages/routes/screens, first validate at least one real internal navigation path via the UI. After that, you MUST cover all requested pages; direct preview-path visits are acceptable once navigation is proven or when navigation is broken.\n"
            "7. Reject if browser diagnostics show runtime errors, if post-action verification is missing, or if the post-action state looks unchanged.\n"
            "If a [Desktop QA Session Evidence] block is already present, treat it as the primary gameplay record and do NOT open browser/browser_use unless you need one extra confirmation for a concrete bug.\n"
            "You MUST use a real interaction record — either browser/browser_use or the provided desktop QA session evidence. Do NOT skip the interaction review.\n\n"
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
            "- Reject if the page feels generic, unfinished, or commercially weak even if it technically works\n"
            "- Reject if the site collapses into flat pure-black / pure-white slabs without a layered palette and supporting surfaces\n"
            "- Reject if key routes are missing a meaningful visual anchor, use oversized awkward images, or swap topic-matched imagery for weaker filler\n\n"
            "VERDICT RULES:\n"
            "- Average score ≥ 7 → APPROVED\n"
            "- Average score < 7 → REJECTED (builder must fix and resubmit)\n"
            "- Any single dimension < 5 → auto REJECTED\n"
            "- Any functionality/completeness/originality score < 6 → REJECTED\n"
            "- APPROVED is invalid if blocking_issues or required_changes are non-empty\n\n"
            "OUTPUT FORMAT (strict JSON):\n"
            "In issues / blocking_issues / required_changes / acceptance_criteria, name exact page routes like index.html or cities.html whenever possible.\n"
            "required_changes MUST be executable and should prefix the owner when you know it, e.g. 'Builder: ...' or 'Polisher: ...'.\n"
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
            "CRITICAL: Do NOT write HTML, CSS, or JavaScript code. Do NOT use file_ops write to create code files.\n"
            "Your ONLY output is a structured TEXT research report with XML-tagged sections.\n"
            "Do NOT attempt to build the website or generate page content — that is the builder's job.\n"
            "When the task asks for references, inspiration, competitors, trends, or design analysis, "
            "you MUST use the browser tool to gather evidence, but prioritize implementation-friendly sources first.\n"
            "Prefer GitHub repos, source trees, README files, tutorials, docs, implementation guides, devlogs, and postmortems.\n"
            "Use live product websites only as supporting visual evidence, not as the primary research set.\n"
            "Prefer browser observe/extract for initial inspection, and use browser act only when interaction is required.\n"
            "Visit at most 2 distinct URLs. Use a 3rd only if the first 2 leave a concrete execution gap.\n"
            "Those URLs should favor GitHub/source/docs/tutorial pages before live product websites.\n"
            "If a site is blocked by captcha, login wall, or bot-detection, skip it immediately and try another URL.\n"
            "Always include the visited URLs in your final report before the analysis summary.\n"
            "Do not stop after a single source unless the task explicitly forbids browsing.\n"
            "For game tasks, do NOT browse playable web games as your main research flow. Prefer GitHub repos, source code, tutorials, docs, devlogs, and postmortems.\n"
            "For browser game tasks, prioritize implementation-grade open-source references such as three.js examples, pmndrs/postprocessing, donmccurdy/three-pathfinding, Mugen87/yuka, and Kenney asset packs when they fit the requested genre.\n"
            "You are optimizing the next nodes' execution quality, not writing a vague inspiration memo.\n"
            "Prefer a mixed evidence set: source repo(s), technical docs/tutorials, and visual/product references.\n"
            "Translate research into concrete downstream constraints, implementation advice, and review criteria.\n"
            "Your report is not freeform. It must contain downstream execution handoffs using exact XML tags:\n"
            "<reference_sites>, <design_direction>, <non_negotiables>, <deliverables_contract>, <risk_register>, "
            "<builder_1_handoff>, <builder_2_handoff>, <reviewer_handoff>, "
            "<tester_handoff>, <debugger_handoff>.\n"
            "Use those tags to optimize the next nodes' prompts. "
            "Enforce a premium quality bar and explicitly ban emoji glyphs inside generated pages.\n"
            "After browser research, produce your report as plain text content (not via file_ops). Keep it under 3000 chars."
        ),
    },
    "scribe": {
        "instructions": (
            "You are a technical writer.\n"
            "When supporting a build workflow, produce a SHORT content-architecture handoff, not code.\n"
            "Do NOT write HTML, CSS, or JavaScript unless the task explicitly asks for source code.\n"
            "Prefer page-by-page structure, CTA/copy priorities, concise headings, examples, and checklists over filler prose.\n"
            "Keep the output compact and implementation-ready."
        ),
    },
    "uidesign": {
        "instructions": (
            "You are a senior UI designer. Produce a concise implementation-ready design brief.\n"
            "Define hierarchy, layout rhythm, motion intent, component behavior, and visual consistency.\n"
            "Do NOT write production HTML/CSS/JS; focus on direction and constraints.\n"
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
            "Output ONLY compact JSON with asset_families, animation_states, palette_constraints, export_layout,\n"
            "frame_counts, and builder_replacement_rules. No prose, no file writes, no speculative extras."
        ),
    },
    "assetimport": {
        "instructions": (
            "You are an asset pipeline coordinator. Organize imported assets, naming, folder structure, usage mapping, and handoff notes.\n"
            "Output ONLY compact JSON with naming_rules, folder_structure, manifest_fields, runtime_mapping,\n"
            "and builder_integration_notes. No prose, no file writes, no speculative extras."
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

    def _filter_viable_model_candidates(self, candidates: List[str]) -> List[str]:
        filtered: List[str] = []
        seen: set[str] = set()
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
            if self._check_api_key(model_name, model_info):
                continue
            filtered.append(model_name)
        return filtered

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
            _push_many(["kimi-coding"])
        filtered_candidates = self._filter_viable_model_candidates(candidates)
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

    def _max_tokens_for_node(self, node_type: str, *, retry_attempt: int = 0) -> int:
        normalized_node_type = normalize_node_role(node_type)
        # Builder often returns full HTML/CSS/JS; keep a higher budget.
        # Escalate on retry to avoid the finish=length death chain:
        # retry 0 → 16384, retry 1 → 20480, retry 2 → 24576
        if normalized_node_type == "builder":
            base = self._read_int_env("EVERMIND_BUILDER_MAX_TOKENS", 16384, 4096, 32768)
            escalated = base + retry_attempt * 4096
            return min(escalated, 32768)
        if normalized_node_type == "polisher":
            # P0 FIX: Polisher needs a much larger budget to avoid finish=length
            # truncation loops. Base increased 6144→12288, cap 16384→32768.
            # retry 0 → 12288, retry 1 → 16384, retry 2 → 20480
            base = self._read_int_env("EVERMIND_POLISHER_MAX_TOKENS", 12288, 4096, 32768)
            return min(base + retry_attempt * 4096, 32768)
        if normalized_node_type in ("spritesheet", "assetimport"):
            return self._read_int_env("EVERMIND_ASSET_PLAN_MAX_TOKENS", 2048, 512, 4096)
        return self._read_int_env("EVERMIND_MAX_TOKENS", 4096, 1024, 16384)

    def _timeout_for_node(self, node_type: str) -> int:
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type == "builder":
            return self._read_int_env("EVERMIND_BUILDER_TIMEOUT_SEC", 900, 30, 960)
        if normalized_node_type == "polisher":
            return self._read_int_env("EVERMIND_POLISHER_TIMEOUT_SEC", 540, 60, 900)
        if normalized_node_type == "imagegen":
            return self._read_int_env("EVERMIND_IMAGEGEN_TIMEOUT_SEC", 150, 45, 300)
        if normalized_node_type in ("spritesheet", "assetimport"):
            return self._read_int_env("EVERMIND_ASSET_PLAN_TIMEOUT_SEC", 75, 30, 180)
        if normalized_node_type in ("planner", "planner_degraded"):
            # P0-1: Planner should finish fast — only outputs skeleton, not full content.
            return self._read_int_env("EVERMIND_PLANNER_TIMEOUT_SEC", 60, 20, 180)
        if normalized_node_type == "analyst":
            # Analyst should finish research quickly — only produces text reports, no code.
            return self._read_int_env("EVERMIND_ANALYST_TIMEOUT_SEC", 120, 30, 240)
        return self._read_int_env("EVERMIND_TIMEOUT_SEC", 90, 30, 600)

    def _stream_stall_timeout_for_node(self, node_type: str) -> int:
        """
        Max allowed gap between streamed chunks before we treat the call as stalled.
        Builder can reasonably take longer before first meaningful chunk.
        Planner uses a short stall window — if it goes silent, it's stuck.
        """
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type == "builder":
            return self._read_int_env("EVERMIND_BUILDER_STREAM_STALL_SEC", 180, 60, 600)
        if normalized_node_type == "polisher":
            return self._read_int_env("EVERMIND_POLISHER_STREAM_STALL_SEC", 150, 45, 360)
        if normalized_node_type == "imagegen":
            return self._read_int_env("EVERMIND_IMAGEGEN_STREAM_STALL_SEC", 90, 20, 180)
        if normalized_node_type in ("spritesheet", "assetimport"):
            return self._read_int_env("EVERMIND_ASSET_PLAN_STREAM_STALL_SEC", 45, 15, 120)
        if normalized_node_type in ("planner", "planner_degraded"):
            # P0-2: Short stall window for planner — fail fast if stuck.
            return self._read_int_env("EVERMIND_PLANNER_STREAM_STALL_SEC", 45, 15, 120)
        return self._read_int_env("EVERMIND_STREAM_STALL_SEC", 180, 30, 300)

    def _effective_stream_stall_timeout(self, node_type: str, input_data: str = "") -> int:
        base = self._stream_stall_timeout_for_node(node_type)
        if normalize_node_role(node_type) != "builder":
            return base

        text = str(input_data or "")
        lower = text.lower()
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
            base = min(
                base,
                self._read_int_env("EVERMIND_BUILDER_RETRY_STREAM_STALL_SEC", 150, 90, 600),
            )
        return base

    def _builder_prewrite_call_timeout(self, node_type: str, input_data: str = "") -> int:
        """
        Absolute cap for a single builder model call before the first real HTML write.
        This guards against long planning / streaming loops that never reach file_ops.
        """
        if normalize_node_role(node_type) != "builder":
            return 0
        base = self._read_int_env("EVERMIND_BUILDER_FIRST_WRITE_TIMEOUT_SEC", 90, 60, 300)
        text = str(input_data or "")
        if task_classifier.wants_multi_page(text):
            base = max(
                base,
                self._read_int_env("EVERMIND_BUILDER_MULTI_PAGE_FIRST_WRITE_SEC", 120, 90, 300),
            )
        retry_markers = (
            "previous attempt failed",
            "previous attempt timed out",
            "retry 1/3",
            "retry 2/3",
            "retry 3/3",
            "multi-page delivery incomplete",
        )
        if any(marker in text.lower() for marker in retry_markers):
            base = min(
                base,
                self._read_int_env("EVERMIND_BUILDER_RETRY_FIRST_WRITE_SEC", 120, 60, 240),
            )
        return base

    def _builder_force_text_threshold(self, input_data: str = "") -> int:
        """
        Limit how many non-write tool turns a builder may spend before switching to
        direct final-file delivery. Multi-page builders should write almost
        immediately; repeated list/read loops are usually wasted time.
        """
        base = self._read_int_env("EVERMIND_BUILDER_FORCE_TEXT_STREAK", 3, 1, 20)
        text = str(input_data or "")
        lower = text.lower()
        assigned_targets = len(self._builder_assigned_html_targets(text))

        if task_classifier.wants_multi_page(text) and assigned_targets >= 3:
            base = min(
                base,
                self._read_int_env("EVERMIND_BUILDER_MULTI_PAGE_FORCE_TEXT_STREAK", 2, 1, 6),
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
            base = min(
                base,
                self._read_int_env("EVERMIND_BUILDER_RETRY_FORCE_TEXT_STREAK", 2, 1, 4),
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

    def _polisher_browser_call_limit(self) -> int:
        return self._read_int_env("EVERMIND_POLISHER_MAX_BROWSER_CALLS", 1, 0, 8)

    def _builder_direct_multifile_requested(self, node_type: str, input_data: str = "") -> bool:
        if normalize_node_role(node_type) != "builder":
            return False
        text = str(input_data or "")
        if not text:
            return False
        return BUILDER_DIRECT_MULTIFILE_MARKER.lower() in text.lower()

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
        lower = str(input_data or "").lower()
        if BUILDER_TARGET_OVERRIDE_MARKER.lower() in lower and assigned_targets >= 1:
            return True
        if assigned_targets < 3:
            return False
        try:
            return task_classifier.wants_multi_page(input_data) or assigned_targets >= 3
        except Exception:
            return True

    def _builder_should_auto_direct_text(
        self,
        node_type: str,
        *,
        model_name: str = "",
        model_info: Optional[Dict[str, Any]] = None,
        input_data: str = "",
    ) -> bool:
        if normalize_node_role(node_type) != "builder":
            return False
        text = str(input_data or "")
        if not text or task_classifier.wants_multi_page(text):
            return False
        resolved = model_info or self._resolve_model(model_name or "")
        provider = str((resolved or {}).get("provider") or "").strip().lower()
        if provider != "kimi":
            return False
        try:
            profile = task_classifier.classify(text)
        except Exception:
            return False
        if profile.task_type != "game":
            return False
        return True

    def _max_tool_iterations_for_node(self, node_type: str) -> int:
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type == "builder":
            return self._read_int_env("EVERMIND_BUILDER_MAX_TOOL_ITERS", 12, 1, 20)
        if normalized_node_type == "polisher":
            return self._read_int_env("EVERMIND_POLISHER_MAX_TOOL_ITERS", 8, 3, 16)
        if normalized_node_type in ("reviewer", "tester"):
            return self._read_int_env("EVERMIND_QA_MAX_TOOL_ITERS", 10, 4, 12)
        if normalized_node_type == "analyst":
            # Analyst commonly needs: source A, source B, one corrective/tool-error turn,
            # then a final text-only handoff. Default 4 keeps that path viable.
            return self._read_int_env("EVERMIND_ANALYST_MAX_TOOL_ITERS", 4, 2, 8)
        return self._read_int_env("EVERMIND_DEFAULT_MAX_TOOL_ITERS", 3, 1, 10)

    def _analyst_browser_call_limit(self) -> int:
        # Distinct source URLs are still capped by the prompt, but one URL often needs
        # multiple browser actions (navigate + observe/extract). Allow a slightly larger
        # call budget so analyst can finish two-source research without artificial stalls.
        return self._read_int_env("EVERMIND_ANALYST_MAX_BROWSER_CALLS", 5, 0, 12)

    def _should_block_browser_call(self, node_type: str, tool_call_stats: Dict[str, int]) -> bool:
        normalized_node_type = normalize_node_role(node_type)
        if normalized_node_type == "analyst":
            limit = self._analyst_browser_call_limit()
        elif normalized_node_type == "polisher":
            limit = self._polisher_browser_call_limit()
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

    def _current_output_dir(self) -> str:
        current = str((self.config or {}).get("output_dir") or "").strip()
        return current or "/tmp/evermind_output"

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

    def _apply_runtime_node_contracts(self, node: Optional[Dict[str, Any]], input_data: str) -> str:
        text = str(input_data or "")
        if normalize_node_role(str((node or {}).get("type") or "").strip()) != "builder":
            return text

        allowed_targets = self._builder_allowed_html_targets_from_node(node)
        if not allowed_targets:
            return text

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
        returned = set(self._builder_completed_html_targets(output_text))
        return [name for name in assigned if name not in returned]

    def _builder_requires_shared_assets(self, input_data: str) -> bool:
        assigned_targets = len(self._builder_assigned_html_targets(input_data))
        try:
            wants_multi_page = task_classifier.wants_multi_page(input_data)
        except Exception:
            wants_multi_page = False
        return assigned_targets >= 2 and (wants_multi_page or assigned_targets >= 3)

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
            "Prefer one shared ```css styles.css``` block and one shared ```js app.js``` block in this first batch if the site reuses the same design system or motion logic.\n"
            "Link those shared assets from the HTML pages instead of duplicating the same CSS/JS inside every page.\n"
            "Do NOT inline large CSS or JS blobs into the HTML when shared assets are available.\n"
            "If you use shared app.js, it MUST be route-safe: every queried element needs a null guard before classList/addEventListener/style access, unless every linked page contains the same DOM structure.\n"
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
        delivered_targets = self._builder_returned_html_targets(accumulated_output)
        batch_size = self._builder_direct_multifile_batch_size(input_data)
        next_batch = remaining_targets[:batch_size]
        output_dir = self._current_output_dir().rstrip("/")
        remaining_line = ", ".join(next_batch[:12])
        allowed_route_line = ", ".join(self._builder_assigned_html_targets(input_data))
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
        return (
            "\n\nRUNTIME OUTPUT CONTRACT:\n"
            f"- Current output directory: {current}\n"
            "- /tmp/evermind_output may be a compatibility alias, but you must treat the current output directory as the source of truth.\n"
            "- Do not inherit stale placeholder files from any other directory.\n"
        )

    def _builder_forced_text_prompt(self, input_data: str) -> str:
        assigned_targets = self._builder_assigned_html_targets(input_data)
        assigned_line = ""
        exact_paths_line = ""
        forbidden_index_line = ""
        example_blocks = ""
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
        return (
            "You have used all your tool calls. Now output the COMPLETE HTML code directly as text. "
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

    def _plain_text_node_needs_forced_output(
        self,
        node_type: str,
        output_text: str,
        tool_results: List[Dict[str, Any]],
    ) -> bool:
        normalized = normalize_node_role(node_type)
        if normalized not in {"analyst", "scribe", "uidesign"}:
            return False
        if self._plain_text_node_output_is_sufficient(node_type, output_text):
            return False
        return bool(tool_results or str(output_text or "").strip())

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
        if len(text) < 500:
            return {}

        files: Dict[str, str] = {}

        # Pattern 1: ```html filename.html\n...\n```
        # Pattern 2: ```html\n...\n``` (no filename → default to index.html)
        pattern = re.compile(
            r"```(?:html?|htm)\s*([A-Za-z0-9._/-]*\.html?)?\s*\n(.*?)```",
            re.DOTALL | re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            filename = (match.group(1) or "").strip()
            content = match.group(2).strip()
            if not content or len(content) < 200:
                continue
            content_lower = content.lower()
            if '<html' not in content_lower and '<!doctype' not in content_lower:
                continue
            if not filename:
                filename = "index.html"
            # Sanitize filename
            filename = Path(filename).name
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html?", filename, re.IGNORECASE):
                filename = "index.html"
            files[filename] = content

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
                files["index.html"] = stripped

        if files:
            logger.info(
                "Extracted %s HTML files from builder text output: %s",
                len(files),
                {k: len(v) for k, v in files.items()},
            )
        return files

    def _select_builder_salvage_text(
        self,
        latest_stream_text: str,
        output_text: str,
        recovered_html: Optional[str] = None,
    ) -> str:
        """Prefer the fullest salvage candidate, especially recovered HTML from truncated tool calls."""
        latest = str(latest_stream_text or "").strip()
        output = str(output_text or "").strip()
        recovered = str(recovered_html or "").strip()

        def _looks_like_html(text: str) -> bool:
            lower = text.lower()
            return "<!doctype" in lower or "<html" in lower or "<body" in lower

        def _body_signal_score(text: str) -> int:
            lower = text.lower()
            score = 0
            for token in ("<body", "<main", "<canvas", "<section", "<button", "<h1", "hud", "start", "play"):
                if token in lower:
                    score += 1
            return score

        best = latest or output
        if recovered and _looks_like_html(recovered):
            recovered_score = (_body_signal_score(recovered), len(recovered))
            best_score = (_body_signal_score(best), len(best))
            if (
                not best
                or not _looks_like_html(best)
                or recovered_score[0] > best_score[0]
                or len(recovered) > max(len(best), 1) * 1.35
            ):
                return recovered
        return best

    async def _builder_partial_text_salvage_result(
        self,
        *,
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
        if not self._builder_partial_text_is_salvageable(input_data, output_text):
            return None
        if on_progress:
            await self._emit_noncritical_progress(on_progress, {
                "stage": "builder_timeout_salvage",
                "reason": str(reason or "partial_text_timeout"),
            })
        await self._publish_partial_output(on_progress, output_text, phase="finalizing")
        logger.info(
            "Salvaging builder partial text after timeout/fallback failure (%s chars, reason=%s)",
            len(str(output_text or "")),
            _sanitize_error(str(reason or ""))[:160],
        )
        payload: Dict[str, Any] = {
            "success": True,
            "output": output_text,
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

        if "html target not assigned" in error.lower():
            return (
                "Your previous file_ops write targeted an HTML file that is not assigned to you. "
                f"{assigned_line}"
                f"{output_path_line}"
                f"Write ONLY those exact HTML filenames under {output_dir}/. "
                "If index.html is not in your assigned list, do not touch it. "
                "Retry immediately with file_ops write for the missing assigned pages only."
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
                if candidate and candidate not in urls:
                    urls.append(candidate)
        return urls

    def _analyst_browser_followup_reason(
        self,
        node_type: str,
        tool_call_stats: Dict[str, int],
        tool_results: List[Dict[str, Any]],
        available_tool_names: Optional[set[str]] = None,
    ) -> Optional[str]:
        if normalize_node_role(node_type) != "analyst":
            return None
        available_names = available_tool_names or set()
        if "browser" not in available_names:
            return None
        browser_calls = int(tool_call_stats.get("browser", 0) or 0)
        visited_urls = self._tool_results_reference_urls(tool_results)
        if browser_calls < 1 or not visited_urls:
            return "You must use the browser tool on at least 2 different source URLs before final report."
        if len(visited_urls) < 2:
            return (
                "You have only visited 1 source URL. "
                "Use the browser tool on one more distinct GitHub/doc/tutorial/source page before final report."
            )
        return None

    def _analyst_browser_followup_message(self, reason: str) -> str:
        reason_text = str(reason or "").strip()
        lower = reason_text.lower()
        action_hint = (
            'Call browser now on an implementation-grade source such as a GitHub repo README, docs page, '
            'tutorial, or postmortem. Visit 2 distinct URLs total before finalizing.'
        )
        if "1 source url" in lower or "one more distinct" in lower:
            action_hint = (
                'Call browser on one more distinct URL now, preferably a GitHub repo, official docs page, '
                'or technical tutorial that materially informs implementation.'
            )
        return (
            "Your research pass is incomplete.\n"
            f"Missing requirement: {reason_text}\n"
            f"{action_hint}\n"
            "Do not output the final analyst report yet. Browse first, then finalize with the visited URLs listed."
        )

    def _browser_use_enabled_for_qa(self) -> bool:
        raw = self.config.get("qa_enable_browser_use", os.getenv("EVERMIND_QA_ENABLE_BROWSER_USE", "0"))
        return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}

    def _qa_browser_use_available(self, node_type: str, available_tool_names: set[str]) -> bool:
        normalized = normalize_node_role(node_type)
        return (
            normalized in {"reviewer", "tester"}
            and self._browser_use_enabled_for_qa()
            and "browser_use" in (available_tool_names or set())
        )

    def _qa_browser_use_required(
        self,
        node_type: str,
        task_type: str,
        available_tool_names: set[str],
    ) -> bool:
        return self._qa_browser_use_available(node_type, available_tool_names) and task_type == "game"

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
        final_url = str(data.get("final_url") or data.get("url") or "").strip() or "http://127.0.0.1:8765/preview/"
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
            return None
        if int(tool_call_stats.get("browser_use", 0) or 0) >= 1:
            return None
        if not plugins or "browser_use" not in available_tool_names:
            return None

        params = {
            "url": "http://127.0.0.1:8765/preview/",
            "task": self._qa_browser_use_prefetch_task(node_type, task_type, input_data),
            "max_steps": 10 if task_type == "game" else 8,
            "timeout_sec": 150 if task_type == "game" else 120,
            "use_vision": True,
            "model": str(
                self.config.get("browser_use_model")
                or self.config.get("default_model")
                or "gpt-4o"
            ).strip() or "gpt-4o",
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
        for item in successful:
            action = _normalized_action(item)
            if action in {"click", "fill", "press", "press_sequence"}:
                seen_interaction = True
                continue
            if seen_interaction and _is_verification_action(item):
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

        # ── Fix: Validate tool_call_id sequencing after compaction ──
        # Ensure every assistant message with tool_calls has a corresponding
        # tool response. Inject synthetic responses for orphaned tool_call_ids
        # to prevent 400 Bad Request from the API.
        prepared = self._fix_orphaned_tool_call_ids(prepared)

        return prepared

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
        node_type = node.get("type", "builder")
        normalized_node_type = normalize_node_role(node_type) or str(node_type or "builder")
        preset = AGENT_PRESETS.get(str(node_type or ""), AGENT_PRESETS.get(normalized_node_type, {}))
        base_prompt = node.get("prompt") or preset.get("instructions", "You are a helpful assistant.")
        prompt_source = str(input_data or node.get("goal") or node.get("task") or "").strip()
        skill_block = build_skill_context(normalized_node_type, prompt_source)
        skill_names = resolve_skill_names_for_goal(normalized_node_type, prompt_source)
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
                return f"{base_prompt}{backend_hint}{runtime_output_block}\n\nLOADED NODE SKILLS:\n{skill_block}{skill_contract}{repo_block}"
            return base_prompt + backend_hint + runtime_output_block + repo_block

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
                return f"{base_prompt}{runtime_output_block}\n\nLOADED NODE SKILLS:\n{skill_block}{skill_contract}{repo_block}"
            return base_prompt + runtime_output_block + repo_block

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
            return f"{base_prompt}{runtime_output_block}\n\nLOADED NODE SKILLS:\n{skill_block}{skill_contract}{mode_hint}"
        return base_prompt + runtime_output_block + mode_hint

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

    def _should_retry_same_model(self, error_message: str) -> bool:
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
            "builder pre-write timeout",
            "polisher pre-write timeout",
            "polisher loop guard",
        )
        return not any(marker in error_lower for marker in non_retryable_markers)

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
            "builder first-write timeout",
            "polisher pre-write timeout",
            "polisher loop guard",
            "stalled",
            "502",
            "503",
            "504",
            "429",
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
        )
        force_builder_direct_multifile = (
            self._builder_direct_multifile_requested(node_type, masked_input)
            or (
                normalized_node_type == "builder"
                and str(node.get("builder_delivery_mode") or "").strip().lower() == "direct_multifile"
            )
            or auto_builder_direct_multifile
        )
        force_builder_direct_text = (
            normalized_node_type == "builder"
            and str(node.get("builder_delivery_mode") or "").strip().lower() == "direct_text"
        ) or auto_builder_direct_text
        effective_node = dict(node or {})
        effective_node["model"] = model_name
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
            "execute(): node=%s model=%s direct_multifile=%s auto_direct=%s extra_headers=%s litellm=%s assigned_targets=%s candidate_count=%s",
            node_type,
            model_name,
            force_builder_direct_multifile,
            auto_builder_direct_multifile or auto_builder_direct_text,
            bool(model_info.get("extra_headers")),
            bool(self._litellm),
            len(assigned_builder_targets),
            total_candidate_count,
        )
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    # F3-1: Jitter + exponential backoff to prevent thundering herd
                    base_wait = 2 ** attempt
                    jitter = random.uniform(0, 1.5)
                    # F3-2: 429 rate-limit — use longer wait and retry same model first
                    is_rate_limit = last_error and any(
                        kw in str(last_error).lower()
                        for kw in ("429", "rate limit", "too many requests", "quota")
                    )
                    if is_rate_limit and attempt == 1:
                        base_wait = 8  # Give the rate limit time to clear
                        logger.info(f"Rate-limited (429) on {model_name}, waiting {base_wait}s before same-model retry...")
                    wait = base_wait + jitter
                    logger.info(f"Retry {attempt}/{max_retries} for {model_name}, waiting {wait:.1f}s (jitter={jitter:.1f})...")
                    if on_progress:
                        await self._emit_noncritical_progress(on_progress, {
                            "stage": "retrying",
                            "attempt": attempt,
                            "wait": round(wait, 1),
                            "assignedModel": model_name,
                            "assignedProvider": model_info.get("provider", ""),
                            "is_rate_limited": is_rate_limit,
                        })
                    await asyncio.sleep(wait)

                if (force_builder_direct_multifile or force_builder_direct_text) and model_info.get("extra_headers"):
                    result = await self._execute_openai_compatible_chat(effective_node, masked_input, model_info, on_progress)
                elif (force_builder_direct_multifile or force_builder_direct_text) and self._litellm:
                    result = await self._execute_litellm_chat(effective_node, masked_input, model_info, on_progress)
                elif force_builder_direct_multifile or force_builder_direct_text:
                    result = await self._execute_openai_direct(effective_node, [], masked_input, on_progress)
                elif model_info.get("supports_cua") and any(p.name == "computer_use" for p in plugins):
                    result = await self._execute_cua_loop(effective_node, plugins, masked_input, on_progress)
                elif model_info.get("provider") == "relay":
                    result = await self._execute_relay(effective_node, masked_input, model_info, on_progress)
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
                    break

                last_error = result.get("error", "Unknown error")
                if not self._should_retry_same_model(last_error):
                    break

            except Exception as e:
                last_error = str(e)
                error_lower = last_error.lower()
                logger.warning(f"Execute attempt {attempt+1} failed: {_sanitize_error(last_error[:200])}")

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
                    result = {
                        "success": False,
                        "output": "",
                        "error": friendly,
                        "model": model_name,
                        "assigned_model": model_name,
                        "assigned_provider": model_info.get("provider", ""),
                    }
                    break

                result = {
                    "success": False,
                    "output": "",
                    "error": _sanitize_error(last_error),
                    "model": model_name,
                    "assigned_model": model_name,
                    "assigned_provider": model_info.get("provider", ""),
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
            has_next_model = index + 1 < len(candidate_models)
            if has_next_model and self._should_fallback_to_next_model(error_message, node_type=node_type):
                next_model = candidate_models[index + 1]
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
            break

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
            await self._emit_noncritical_progress(
                on_progress,
                {"stage": "calling_relay", "relay": model_info.get("relay_name", "?"), "model": model_name},
            )

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
            await self._emit_noncritical_progress(on_progress, event)

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
        max_tokens = self._max_tokens_for_node(node_type, retry_attempt=int(node.get("retry_attempt", 0)))
        timeout_sec = self._timeout_for_node(node_type)
        max_continuations = self._read_int_env("EVERMIND_MAX_CONTINUATIONS", 2, 0, 5)
        output_text = ""
        latest_stream_text = ""
        latest_stream_activity_at = 0.0
        meaningful_stream_activity_at = 0.0
        stream_has_meaningful_activity = False
        tool_results: List[Dict[str, Any]] = []
        tool_call_stats: Dict[str, int] = {}
        builder_has_written_file = False
        polisher_has_written_file = False

        # Get API key
        api_key_env = {
            "kimi": "KIMI_API_KEY", "qwen": "QWEN_API_KEY"
        }.get(model_info.get("provider"))
        api_key = None
        if api_key_env:
            api_key = self.config.get(api_key_env.lower()) or os.getenv(api_key_env)

        if not api_key:
            return {"success": False, "output": "", "error": f"API key not configured for {model_info.get('provider')}"}

        logger.info(
            "_execute_openai_compatible: model=%s timeout=%ss stall=%ss tools=%s",
            model_name, timeout_sec, self._effective_stream_stall_timeout(node_type, input_data),
            len(plugins) if plugins else 0,
        )
        if on_progress:
            await self._emit_noncritical_progress(
                on_progress,
                {"stage": "calling_ai", "model": model_name, "mode": "openai_compatible"},
            )

        # Build tools from plugins
        tools = []
        if plugins:
            for p in plugins:
                if p.name != "computer_use":
                    defn = p.get_tool_definition()
                    tools.append({"type": "function", "function": defn} if "function" not in defn else defn)
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

            # Streaming stall timeout: cancel when the stream stops producing chunks.
            stall_timeout = self._effective_stream_stall_timeout(node_type, input_data)

            def _call_streaming(msgs, tls):
                """Make API call with streaming to detect stalls early."""
                nonlocal latest_stream_text, latest_stream_activity_at, meaningful_stream_activity_at, stream_has_meaningful_activity
                prepared_msgs = self._prepare_messages_for_request(msgs, model_name)
                kwargs = {
                    "model": model_name,
                    "messages": prepared_msgs,
                    "max_tokens": max_tokens,
                    "stream": True,
                    # F4-1: Request usage data in streaming mode for accurate token monitoring
                    "stream_options": {"include_usage": True},
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
                stream_started_at = last_chunk_time
                first_chunk_at: Optional[float] = None
                first_content_at: Optional[float] = None
                first_tool_call_at: Optional[float] = None
                chunk_count = 0

                for chunk in stream:
                    now = time.time()
                    latest_stream_activity_at = now
                    if now - last_chunk_time > stall_timeout:
                        raise TimeoutError(f"Stream stalled: no chunk for {stall_timeout}s")
                    if first_chunk_at is None:
                        first_chunk_at = now
                    chunk_count += 1
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
                                stream_has_meaningful_activity = True
                                meaningful_stream_activity_at = now
                                if tc.function.name:
                                    entry["function"]["name"] = tc.function.name
                                if tc.function.arguments:
                                    entry["function"]["arguments"] += tc.function.arguments
                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason

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

                            # Strategy 1: json_repair library (best-effort structural repair)
                            if _json_repair_fn and fn_name in ("file_ops", "write_file", "write"):
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
                salvage_text = self._select_builder_salvage_text(
                    latest_stream_text,
                    output_text,
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
                    extracted_files = self._extract_html_files_from_text_output(salvage_text, input_data)
                    if extracted_files:
                        output_dir = self._current_output_dir()
                        saved_count = 0
                        for filename, html_content in extracted_files.items():
                            if len(html_content) < 200:
                                continue
                            try:
                                target_path = Path(output_dir) / filename
                                target_path.parent.mkdir(parents=True, exist_ok=True)
                                target_path.write_text(html_content, encoding="utf-8")
                                builder_has_written_file = True
                                saved_count += 1
                                tool_results.append({
                                    "success": True,
                                    "data": {"path": str(target_path), "bytes_written": len(html_content.encode("utf-8"))},
                                    "error": None,
                                    "artifacts": [str(target_path)],
                                })
                                logger.info(
                                    "Salvage auto-saved text-mode HTML: %s (%s chars)",
                                    filename,
                                    len(html_content),
                                )
                            except Exception as save_exc:
                                logger.warning("Salvage auto-save failed for %s: %s", filename, str(save_exc)[:200])
                        if builder_has_written_file:
                            if saved_count > 0:
                                tool_call_stats["file_ops"] = tool_call_stats.get("file_ops", 0) + saved_count
                            # Return success since we saved usable HTML
                            return {
                                "success": True,
                                "output": salvage_text,
                                "model": model_name,
                                "tool_results": tool_results,
                                "mode": "openai_compatible_text_mode_auto_save",
                                "tool_call_stats": dict(tool_call_stats),
                            }

                salvage = await self._builder_partial_text_salvage_result(
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
                        {"stage": "forcing_text_output", "reason": reason or "prewrite_call_timeout"},
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
                    20,
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

            async def _await_stream_call(msgs, tls):
                nonlocal latest_stream_text
                call_task = asyncio.create_task(asyncio.to_thread(_call_streaming, msgs, tls))
                timeout_phase = "hard"
                timeout_value = 0
                hard_timeout = 0
                prewrite_timeout = 0
                if normalized_node_type == "builder" and not builder_has_written_file:
                    prewrite_timeout = self._builder_prewrite_call_timeout(node_type, input_data)
                elif normalized_node_type == "polisher" and not polisher_has_written_file:
                    prewrite_timeout = self._polisher_prewrite_call_timeout(node_type, input_data)
                # Always enforce a hard ceiling timeout — even after the first file write.
                hard_timeout = timeout_sec
                effective = prewrite_timeout if prewrite_timeout > 0 else hard_timeout
                logger.info(
                    "_await_stream_call: prewrite_timeout=%s hard_timeout=%s effective=%s written=%s",
                    prewrite_timeout,
                    hard_timeout,
                    effective,
                    builder_has_written_file if normalized_node_type == "builder" else polisher_has_written_file,
                )
                try:
                    started_at = time.time()
                    hard_deadline = started_at + hard_timeout
                    prewrite_deadline = started_at + prewrite_timeout if prewrite_timeout > 0 else None
                    activity_grace = max(12.0, min(float(stall_timeout) / 4.0, 45.0))

                    while True:
                        now = time.time()
                        remaining_hard = hard_deadline - now
                        if remaining_hard <= 0:
                            call_task.cancel()
                            timeout_phase = "hard"
                            timeout_value = hard_timeout
                            raise asyncio.TimeoutError()

                        next_poll = min(1.0, remaining_hard)
                        if prewrite_deadline is not None:
                            next_poll = min(next_poll, max(0.01, prewrite_deadline - now))
                        done, _ = await asyncio.wait({call_task}, timeout=next_poll)
                        if call_task in done:
                            return call_task.result()

                        if prewrite_deadline is None or now < prewrite_deadline:
                            continue

                        meaningful_age = None
                        if meaningful_stream_activity_at > 0:
                            meaningful_age = now - meaningful_stream_activity_at
                        latest_len = len(latest_stream_text or "")
                        active_writer = (
                            builder_has_written_file if normalized_node_type == "builder"
                            else polisher_has_written_file
                        )
                        if (
                            not active_writer
                            and stream_has_meaningful_activity
                            and meaningful_age is not None
                            and meaningful_age <= activity_grace
                        ):
                            logger.info(
                                "%s prewrite deadline reached but stream is active (latest_chars=%s meaningful_age=%.2fs grace=%.2fs) — continuing until hard timeout",
                                normalized_node_type or node_type,
                                latest_len,
                                meaningful_age,
                                activity_grace,
                            )
                            prewrite_deadline = None
                            continue

                        call_task.cancel()
                        timeout_phase = "pre-write"
                        timeout_value = prewrite_timeout
                        raise asyncio.TimeoutError()
                except asyncio.TimeoutError as exc:
                    if normalized_node_type == "builder":
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
            max_iterations = self._max_tool_iterations_for_node(node_type)
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

                    if tc_type != "function" or not fn_name:
                        logger.warning("Skipping unsupported tool call payload: type=%s id=%s", tc_type, tc_id)
                        continue

                    if on_progress:
                        await self._emit_noncritical_progress(
                            on_progress,
                            {"stage": "executing_plugin", "plugin": fn_name},
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
                    elif fn_name == "browser" and self._should_block_browser_call(node_type, tool_call_stats):
                        limit = (
                            self._polisher_browser_call_limit()
                            if normalized_node_type == "polisher"
                            else self._analyst_browser_call_limit()
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
                        if action == "write":
                            result = {
                                "success": False,
                                "data": {},
                                "error": self._plain_text_node_write_guard_error(normalized_node_type),
                                "artifacts": [],
                            }
                            plain_text_force_reason = plain_text_force_reason or "blocked_file_write"
                            logger.warning(
                                "Blocked file_ops write for %s node — must produce text report only",
                                normalized_node_type,
                            )
                        else:
                            result = await self._run_plugin(
                                fn_name,
                                fn_args,
                                approved_plugins=approved_plugins,
                                plugin_context=plugin_context,
                            )
                    else:
                        result = await self._run_plugin(
                            fn_name,
                            fn_args,
                            plugins or [],
                            node_type=node_type,
                            node=node,
                        )
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
                                await self._emit_noncritical_progress(on_progress, {
                                    "stage": "builder_write",
                                    "plugin": fn_name,
                                    "path": write_path,
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
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "artifact_write",
                                "plugin": fn_name,
                                "path": write_path,
                                "agent": normalized_node_type or node_type,
                                "writer": normalized_node_type or node_type,
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
                    if len(result_str) > MAX_TOOL_RESULT_CHARS:
                        result_str = result_str[:MAX_TOOL_RESULT_CHARS] + '... [TRUNCATED]'
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
                return {
                    "success": False,
                    "output": output_text,
                    "error": polisher_loop_guard_reason,
                    "model": model_name,
                    "tool_results": tool_results,
                    "mode": "openai_compatible",
                    "usage": usage_totals,
                    "tool_call_stats": dict(tool_call_stats),
                    "qa_browser_use_available": qa_browser_use_available,
                }

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
                        {"stage": "forcing_text_output", "reason": force_text_reason},
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
                        {"stage": "forcing_text_output", "reason": plain_text_final_reason},
                    )
                forced_messages = self._plain_text_node_forced_messages(
                    system_prompt,
                    input_data,
                    tool_results,
                    output_text,
                    node_type,
                    plain_text_final_reason,
                )
                try:
                    final_resp = await _await_stream_call(forced_messages, [])
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

            # ── P0 FIX B: Auto-save HTML from text output if builder never used file_ops ──
            # Kimi K2.5 sometimes outputs full HTML as markdown code blocks in content text
            # instead of calling file_ops. When this happens, output_text has 37k+ chars of
            # complete HTML that needs to be saved to disk, otherwise quality gate sees empty body.
            if (
                normalized_node_type == "builder"
                and not builder_has_written_file
                and output_text
                and len(output_text) > 1000
            ):
                extracted_files = self._extract_html_files_from_text_output(output_text, input_data)
                if extracted_files:
                    output_dir = self._current_output_dir()
                    saved_count = 0
                    for filename, html_content in extracted_files.items():
                        if len(html_content) < 200:
                            continue
                        try:
                            target_path = Path(output_dir) / filename
                            target_path.parent.mkdir(parents=True, exist_ok=True)
                            target_path.write_text(html_content, encoding="utf-8")
                            builder_has_written_file = True
                            saved_count += 1
                            tool_results.append({
                                "success": True,
                                "data": {"path": str(target_path), "bytes_written": len(html_content.encode("utf-8"))},
                                "error": None,
                                "artifacts": [str(target_path)],
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
                    if saved_count > 0:
                        tool_call_stats["file_ops"] = tool_call_stats.get("file_ops", 0) + saved_count
                        logger.info(
                            "Builder text-mode auto-save complete: %s files written to %s",
                            saved_count,
                            output_dir,
                        )

            return {
                "success": True, "output": output_text,
                "model": getattr(response, "model", model_name),
                "tool_results": tool_results, "mode": "openai_compatible",
                "usage": usage_totals,
                "tool_call_stats": tool_call_stats,
                "qa_browser_use_available": qa_browser_use_available,
            }
        except TimeoutError as e:
            fallback = await _force_builder_text_timeout_fallback(str(e))
            if fallback is not None:
                return fallback
            if on_progress:
                await self._emit_noncritical_progress(
                    on_progress,
                    {"stage": "stream_stalled", "reason": str(e)},
                )
            return {"success": False, "output": "", "error": _sanitize_error(str(e))}
        except Exception as e:
            err = _sanitize_error(str(e))
            if normalized_node_type == "builder" and not builder_has_written_file and "timeout" in err.lower():
                fallback = await _force_builder_text_timeout_fallback(err)
                if fallback is not None:
                    return fallback
            if on_progress and ("timed out" in err.lower() or "timeout" in err.lower()):
                await self._emit_noncritical_progress(
                    on_progress,
                    {"stage": "stream_stalled", "reason": err},
                )
            return {"success": False, "output": "", "error": err}

    async def _execute_openai_compatible_chat(self, node, input_data, model_info, on_progress) -> Dict:
        from openai import OpenAI

        node_type = node.get("type", "builder")
        system_prompt = self._compose_system_prompt(node, input_data=input_data)
        model_name = model_info["litellm_id"].replace("openai/", "")
        max_tokens = self._max_tokens_for_node(node_type, retry_attempt=int(node.get("retry_attempt", 0)))
        timeout_sec = self._timeout_for_node(node_type)
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

        api_key_env = {"kimi": "KIMI_API_KEY", "qwen": "QWEN_API_KEY"}.get(model_info.get("provider"))
        api_key = None
        if api_key_env:
            api_key = self.config.get(api_key_env.lower()) or os.getenv(api_key_env)
        if not api_key:
            return {"success": False, "output": "", "error": f"API key not configured for {model_info.get('provider')}"}

        if on_progress:
            await self._emit_noncritical_progress(
                on_progress,
                {"stage": "calling_ai", "model": model_name, "tools_count": 0, "mode": "openai_compatible_chat"},
            )

        stall_timeout = self._effective_stream_stall_timeout(node_type, input_data)
        latest_stream_text = ""
        output_parts: List[str] = []
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
            client = OpenAI(
                api_key=api_key,
                base_url=model_info.get("api_base"),
                default_headers=model_info.get("extra_headers", {}),
                timeout=timeout_sec,
            )
            kwargs_base: Dict[str, Any] = {
                "model": model_name,
                "max_tokens": max_tokens,
            }
            if model_info.get("provider") == "kimi":
                if os.getenv("EVERMIND_KIMI_THINKING", "disabled").lower() != "enabled":
                    kwargs_base["extra_body"] = {"thinking": {"type": "disabled"}}

            def _call_chat_streaming(current_messages):
                """Make API call with streaming to detect stalls early."""
                nonlocal latest_stream_text
                latest_stream_text = ""
                kwargs = dict(kwargs_base)
                kwargs["messages"] = current_messages
                kwargs["stream"] = True
                # F4-1: Request usage data in streaming mode for accurate token monitoring
                kwargs["stream_options"] = {"include_usage": True}
                kwargs["timeout"] = stall_timeout
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
                for chunk in stream:
                    now = time.time()
                    if now - last_chunk_time > stall_timeout:
                        raise TimeoutError(f"Chat stream stalled: no chunk for {stall_timeout}s")
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
                return await asyncio.wait_for(
                    asyncio.to_thread(_call_chat_streaming, current_messages),
                    timeout=timeout_sec,
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
            # Try to salvage whatever the stream produced before the timeout
            if normalize_node_role(node_type) == "builder" and stitched_output:
                salvage = await self._builder_partial_text_salvage_result(
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
    # Path 3: LiteLLM with Tool Calling
    # ─────────────────────────────────────────
    async def _execute_litellm_tools(self, node, plugins, input_data, model_info, on_progress) -> Dict:
        node_type = node.get("type", "builder")
        normalized_node_type = normalize_node_role(node_type)
        system_prompt = self._compose_system_prompt(node, plugins=plugins, input_data=input_data)
        litellm_model = model_info["litellm_id"]
        max_tokens = self._max_tokens_for_node(node_type, retry_attempt=int(node.get("retry_attempt", 0)))
        timeout_sec = self._timeout_for_node(node_type)
        builder_has_written_file = False
        polisher_has_written_file = False
        output_text = ""
        tool_results: List[Dict[str, Any]] = []
        tool_call_stats: Dict[str, int] = {}
        usage_totals: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        total_cost = 0.0

        # Build OpenAI-format tools from plugins
        tools = []
        for p in plugins:
            if p.name != "computer_use":
                defn = p.get_tool_definition()
                tools.append({"type": "function", "function": defn} if "function" not in defn else defn)
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

            async def _force_builder_text_timeout_fallback(reason: str) -> Optional[Dict[str, Any]]:
                if normalized_node_type != "builder" or builder_has_written_file:
                    return None
                salvage = await self._builder_partial_text_salvage_result(
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
                        {"stage": "forcing_text_output", "reason": reason or "prewrite_call_timeout"},
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
                    20,
                    15,
                    120,
                )
                final_kwargs = dict(kwargs)
                final_kwargs.pop("tools", None)
                final_kwargs.pop("tool_choice", None)
                final_kwargs["messages"] = self._prepare_messages_for_request(forced_messages, litellm_model)
                try:
                    final_resp = await asyncio.wait_for(
                        asyncio.to_thread(self._litellm.completion, **final_kwargs),
                        timeout=forced_timeout,
                    )
                except Exception as fallback_exc:
                    salvage = await self._builder_partial_text_salvage_result(
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
                call = asyncio.to_thread(self._litellm.completion, **current_kwargs)
                timeout = 0
                if normalized_node_type == "builder" and not builder_has_written_file:
                    timeout = self._builder_prewrite_call_timeout(node_type, input_data)
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
                self._max_tool_iterations_for_node(node_type)
                if normalized_node_type in {"builder", "polisher"}
                else self._read_int_env("EVERMIND_LITELLM_MAX_TOOL_ITERS", 10, 1, 30)
            )
            usage_totals = self._normalize_usage(getattr(response, "usage", None))
            total_cost = self._estimate_litellm_cost(response, litellm_model)
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
            plain_text_force_reason = ""

            while iteration < max_iterations:
                iteration += 1
                msg = response.choices[0].message
                msg_content = getattr(msg, "content", "") or ""
                if msg.tool_calls and msg_content:
                    output_text += msg.content

                # Check for tool calls
                if not msg.tool_calls:
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
                    if msg_content:
                        output_text += msg_content
                    break

                messages.append(self._serialize_assistant_message(msg))
                pending_repair_prompts: List[str] = []
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    fn_args = tc.function.arguments
                    if on_progress:
                        await self._emit_noncritical_progress(
                            on_progress,
                            {"stage": "executing_plugin", "plugin": fn_name},
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
                    elif fn_name == "browser" and self._should_block_browser_call(node_type, tool_call_stats):
                        limit = (
                            self._polisher_browser_call_limit()
                            if normalized_node_type == "polisher"
                            else self._analyst_browser_call_limit()
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
                        if block_action == "write":
                            result = {
                                "success": False,
                                "data": {},
                                "error": self._plain_text_node_write_guard_error(normalized_node_type),
                                "artifacts": [],
                            }
                            plain_text_force_reason = plain_text_force_reason or "blocked_file_write"
                            logger.warning(
                                "Blocked file_ops write for %s node (CUA path)",
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
                        result = await self._run_plugin(
                            fn_name,
                            fn_args,
                            plugins,
                            node_type=node_type,
                            node=node,
                        )
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
                                await self._emit_noncritical_progress(on_progress, {
                                    "stage": "builder_write",
                                    "plugin": fn_name,
                                    "path": write_path,
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
                            await self._emit_noncritical_progress(on_progress, {
                                "stage": "artifact_write",
                                "plugin": fn_name,
                                "path": write_path,
                                "agent": normalized_node_type or node_type,
                                "writer": normalized_node_type or node_type,
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
                    if len(result_str) > MAX_TOOL_RESULT_CHARS:
                        result_str = result_str[:MAX_TOOL_RESULT_CHARS] + '... [TRUNCATED]'
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
                return {
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
                }

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
                        {"stage": "forcing_text_output", "reason": force_text_reason},
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
                        {"stage": "forcing_text_output", "reason": plain_text_final_reason},
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

            return {"success": True, "output": output_text, "tool_results": tool_results,
                    "model": litellm_model, "iterations": iteration, "mode": "litellm_tools", "usage": usage_totals, "cost": total_cost,
                    "tool_call_stats": tool_call_stats, "qa_browser_use_available": qa_browser_use_available}
        except Exception as e:
            err = _sanitize_error(str(e))
            if normalized_node_type == "builder" and not builder_has_written_file and "timeout" in err.lower():
                fallback = await _force_builder_text_timeout_fallback(err)
                if fallback is not None:
                    return fallback
            logger.error(f"LiteLLM tools error: {_sanitize_error(str(e))}")
            return {"success": False, "output": "", "error": err}

    # ─────────────────────────────────────────
    # Path 3: LiteLLM Direct Chat
    # ─────────────────────────────────────────
    async def _execute_litellm_chat(self, node, input_data, model_info, on_progress) -> Dict:
        system_prompt = self._compose_system_prompt(node, input_data=input_data)
        node_type = node.get("type", "builder")
        litellm_model = model_info["litellm_id"]
        max_tokens = self._max_tokens_for_node(node_type, retry_attempt=int(node.get("retry_attempt", 0)))
        timeout_sec = min(self._timeout_for_node(node_type), 240)
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
            if model_info.get("api_base"):
                provider = model_info.get("provider", "")
                env_base_key = {
                    "openai": "OPENAI_API_BASE", "anthropic": "ANTHROPIC_API_BASE",
                    "google": "GEMINI_API_BASE", "deepseek": "DEEPSEEK_API_BASE",
                    "kimi": "KIMI_API_BASE", "qwen": "QWEN_API_BASE",
                }.get(provider, "")
                env_base = os.getenv(env_base_key, "") if env_base_key else ""
                kwargs_base["api_base"] = env_base if env_base else model_info["api_base"]
            if model_info.get("extra_headers"):
                kwargs_base["extra_headers"] = model_info["extra_headers"]
            api_key_env = {
                "kimi": "KIMI_API_KEY", "qwen": "QWEN_API_KEY"
            }.get(model_info.get("provider"))
            if api_key_env:
                kwargs_base["api_key"] = self.config.get(api_key_env.lower()) or os.getenv(api_key_env)

            async def _call_chat(current_messages: List[Dict[str, str]]):
                kwargs = dict(kwargs_base)
                kwargs["messages"] = self._prepare_messages_for_request(current_messages, litellm_model)
                return await asyncio.to_thread(self._litellm.completion, **kwargs)

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
                    node_meta.get("output_dir")
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
                plugin_context["file_ops_can_write_root_index"] = bool(
                    node_meta.get("can_write_root_index")
                    or node_meta.get("file_ops_can_write_root_index")
                )
                plugin_context["file_ops_enforce_html_targets"] = bool(
                    node_meta.get("enforce_html_targets")
                    or node_meta.get("file_ops_enforce_html_targets")
                )

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
