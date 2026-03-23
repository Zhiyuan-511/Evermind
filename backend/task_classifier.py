"""
Task Type Classifier + Specialized Prompt Templates

Detects the user's intent from goal text and provides task-specific
design guidance, structure blueprints, and quality criteria.
"""

import re
from pathlib import Path
from typing import Dict, NamedTuple

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _load_template(name: str) -> str:
    """Load a CSS template file. Returns empty string if not found."""
    path = _TEMPLATE_DIR / name
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""

# ─────────────────────────────────────────────────────────────────
# Task Types
# ─────────────────────────────────────────────────────────────────

class TaskProfile(NamedTuple):
    task_type: str
    role: str           # Who the builder acts as
    design_system: str  # Visual/technical design guidance
    blueprint: str      # Structure / architecture blueprint
    quality: str        # What "good" looks like
    analyst_hint: str   # Research guidance for pro mode analyst
    tester_hint: str    # What tester should check


# ─────────────────────────────────────────────────────────────────
# Keyword patterns for classification
# ─────────────────────────────────────────────────────────────────

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("game", re.compile(
        r"(游戏|game|play|玩|pixel|像素|弹球|贪吃蛇|snake|tetris|俄罗斯方块|打飞机|射击|"
        r"platformer|跑酷|flappy|pong|breakout|chess|棋|card game|纸牌|rpg|冒险|"
        r"arcade|迷宫|maze|puzzle game|益智游戏|打砖块|消消乐|match-3|tower defense|塔防)",
        re.IGNORECASE,
    )),
    ("dashboard", re.compile(
        r"(仪表盘|dashboard|后台|admin|管理面板|管理系统|control panel|analytics|"
        r"数据面板|data panel|monitor|监控|CRM|ERP|报表|stock tracker|"
        r"运营平台|管理后台|admin panel|overview page|统计|statistics)",
        re.IGNORECASE,
    )),
    ("tool", re.compile(
        r"(工具|tool|计算器|calculator|转换器|converter|编辑器|editor|"
        r"生成器|generator|format|格式化|密码|password|todo|待办|"
        r"记事本|notepad|markdown|json|timer|计时|countdown|倒计时|"
        r"color picker|取色|unit convert|翻译|translator|encoder|decoder)",
        re.IGNORECASE,
    )),
    ("presentation", re.compile(
        r"(PPT|slides|幻灯片|演示|presentation|keynote|slideshow|"
        r"展示页|pitch deck|报告页面|annual report|汇报)",
        re.IGNORECASE,
    )),
    ("creative", re.compile(
        r"(动画|animation|canvas art|创意|creative|3D|three\.?js|"
        r"generative|生成艺术|粒子|particle|shader|WebGL|svg art|"
        r"音乐可视化|music visual|interactive art|交互艺术|"
        r"loading animation|加载动画|clock|时钟|firework|烟花)",
        re.IGNORECASE,
    )),
    # website is the default fallback — broadest patterns
    ("website", re.compile(
        r"(网站|website|网页|web page|官网|landing|着陆页|商城|电商|"
        r"e-commerce|shop|store|blog|博客|portfolio|作品集|company|"
        r"企业|brand|品牌|restaurant|餐厅|hotel|酒店|登录|login|"
        r"signup|注册|首页|homepage|产品页|product|服务|service)",
        re.IGNORECASE,
    )),
]

# ─────────────────────────────────────────────────────────────────
# Specialized profiles
# ─────────────────────────────────────────────────────────────────

_COMMON_RULES = (
    "RULES: ONE index.html with inline <style>+<script>. "
    "Use CSS vars, responsive @media rules, and deliberate typography choices that fit the product. "
    "Prefer inline SVG, CSS illustration, or bespoke visual treatment over generic icon shortcuts. "
    "Implement as much code as the task actually needs; do not force a tiny low-quality output. "
    "Start <!DOCTYPE html>, end </html>. "
    "NEVER use emoji characters as UI icons, bullet decorations, status markers, or illustrations inside generated pages. "
    "Use inline SVG, CSS shapes, or typography instead. "
    "Before finishing, self-check for blank sections, placeholder copy, broken interactions, and weak visual polish.\n"
)

_COMMON_DELIVERY = (
    "\nDELIVER: file_ops write to /tmp/evermind_output/index.html (preferred). "
    "Or return full HTML in ```html block. Call file_ops write IMMEDIATELY for new projects.\n"
)


PROFILES: Dict[str, TaskProfile] = {

    # ─── Website ───────────────────────────────────────────────
    "website": TaskProfile(
        task_type="website",
        role="You are a senior product web designer and frontend engineer.",
        design_system=(
            "DESIGN SYSTEM:\n"
            "A. Color palette — choose by content:\n"
            "   Tech/SaaS: dark (#0a0a0f bg, #6c5ce7 primary, #00cec9 accent)\n"
            "   Fashion/Lifestyle: light (#fafafa bg, #1a1a2e primary, rose accent)\n"
            "   Food/Travel: warm (#fff8f0 bg, #e67e22 primary)\n"
            "   Corporate: clean (#ffffff bg, #2563eb primary)\n"
            "   Creative: bold dark (#0d1117 bg, #f97316 accent)\n"
            "   Define ALL colors as CSS variables.\n"
            "B. Typography: Inter; h1: clamp(2rem,5vw,3.5rem)/700\n"
            "C. 8px spacing scale; Cards: border-radius:16px + depth shadow\n"
            "D. Sticky glassmorphism header (backdrop-filter:blur(20px))\n"
            "E. Hero: gradient text (background-clip:text) + glowing CTA\n"
            "F. Buttons: padding:14px 32px, border-radius:12px, hover:translateY(-2px)\n"
            "G. Feature grid: CSS Grid auto-fit minmax(280px,1fr)\n"
            "H. Animations: fadeUp + slideIn with stagger delays\n"
        ),
        blueprint=(
            "STRUCTURE:\n"
            "<header> sticky nav + brand + 3 links + CTA button\n"
            "<main> hero → trust badges → feature grid → showcase → testimonials → CTA strip\n"
            "<footer> 2-4 columns + copyright\n"
            "At least 6 visible content blocks.\n"
        ),
        quality="Must look like a premium landing page by a pro designer. Not a student project.",
        analyst_hint=(
            "Visit 2-3 high-quality reference sites related to the goal. "
            "If a site has captcha or bot-detection, skip it and try a different URL. "
            "Extract: color scheme, layout pattern, typography, key features. Deliver a SHORT design brief. "
            "Try at least 3 different sites to ensure sufficient reference material."
        ),
        tester_hint=(
            "Step1: file-check (index.html exists); "
            "Step2: MUST USE browser tool to navigate to http://127.0.0.1:8765/preview/ and take full-page screenshot; "
            "Step3: MUST USE browser snapshot to inspect visible links/buttons/forms before interacting; "
            "Step4: MUST USE browser tool to scroll down 500px and take another screenshot; "
            "Step5: click at least one real interactive element and MUST verify changed state with wait_for or a second snapshot; "
            "Step6: a PASS verdict is invalid without post-action verification evidence; "
            "Step7: FAIL if browser diagnostics show runtime errors; give PASS/FAIL with concrete visual assessment."
        ),
    ),

    # ─── Game ──────────────────────────────────────────────────
    "game": TaskProfile(
        task_type="game",
        role="You are a senior game developer specializing in browser-based HTML5 games.",
        design_system=(
            "GAME DESIGN SYSTEM:\n"
            "A. Use <canvas> for rendering (2D context) OR pure CSS/DOM for simple games\n"
            "B. Implement a proper game loop: requestAnimationFrame with delta time\n"
            "C. State machine: MENU → PLAYING → PAUSED → GAME_OVER\n"
            "D. Keyboard/touch input handling with event listeners on document (NOT canvas)\n"
            "E. Score system with visual HUD overlay\n"
            "F. Collision detection (AABB or distance-based)\n"
            "G. Particle effects for impacts/explosions/scoring\n"
            "H. Sound: use Web Audio API oscillator for retro SFX (no external files)\n"
            "I. Color palette: use a cohesive game palette (e.g. pico-8 inspired)\n"
            "J. Pixel-perfect rendering: image-rendering: pixelated for retro; smooth for modern\n"
            "K. Start screen with title + clickable 'Start Game' button (MUST use onclick handler)\n"
            "   IMPORTANT: Start button MUST work via mouse click, not only keyboard!\n"
            "   The game may run inside an iframe where keyboard focus requires a click first.\n"
            "L. Game over screen with score + high score (localStorage) + restart button (clickable)\n"
            "M. Keyboard listeners MUST be on document.addEventListener('keydown', ...) not on canvas\n"
            "N. Auto-focus: when game starts, call canvas.focus() and add tabindex='0' to canvas\n"
            "O. If custom art is required but no external asset generator is attached, create high-quality SVG or pixel placeholders\n"
            "   with a clear asset manifest so imagegen / spritesheet nodes can replace them later without re-architecting the game\n"
        ),
        blueprint=(
            "STRUCTURE:\n"
            "- Full-viewport canvas or game container (no scroll)\n"
            "- HUD overlay: score (top-left), lives/health (top-right)\n"
            "- Start menu: centered title + subtitle + start button\n"
            "- Game over: fade overlay + final score + play again\n"
            "- Mobile: add on-screen touch controls if applicable\n"
        ),
        quality=(
            "Must feel like a polished indie browser game. Smooth 60fps animation. "
            "Responsive controls. Visual feedback on every action. No jank."
        ),
        analyst_hint=(
            "Research implementation-grade game references. Prioritize GitHub repositories, "
            "technical tutorials, devlogs, postmortems, collision/game-loop writeups, "
            "level-design breakdowns, and official docs for browser game techniques. "
            "You may inspect showcase pages for visual direction, but DO NOT spend time playing "
            "online browser games or treating gameplay as the main research method. "
            "If a site has captcha or bot-detection, skip it and try a different URL. "
            "Summarize mechanics, rendering patterns, controls, asset strategy, and production risks."
        ),
        tester_hint=(
            "Step1: file-check /tmp/evermind_output/ for HTML game files; "
            "Step2: browser navigate to http://127.0.0.1:8765/preview/, take screenshot of start screen; "
            "Step3: browser snapshot to find start/play controls and HUD; "
            "Step4: check browser console for JS errors; "
            "Step5: CLICK the start/play button to begin the game; "
            "Step6: TEST CONTROLS — use press_sequence with Arrow keys/WASD/Space for at least 15 seconds; "
            "Step7: MUST verify changed state hash or visible HUD/score/player movement after gameplay input; "
            "Step8: VERIFY GAMEPLAY — does the player move? Do enemies/obstacles appear? Does scoring work? "
            "Take a screenshot MID-GAMEPLAY showing active gameplay (not the start screen); "
            "Step9: Try to trigger game over and check if game over screen appears; "
            "Step10: PASS only if game is ACTUALLY PLAYABLE — player can move, interact, and game responds to input. "
            "FAIL if: game doesn't start, controls don't work, no gameplay visible, state never changes, or JS errors prevent play."
        ),
    ),

    # ─── Dashboard ─────────────────────────────────────────────
    "dashboard": TaskProfile(
        task_type="dashboard",
        role="You are a senior product designer specializing in data dashboards and admin panels.",
        design_system=(
            "DASHBOARD DESIGN SYSTEM:\n"
            "A. Dark sidebar (240px) + light/dark main content area\n"
            "B. Color: dark sidebar (#1e1e2e), content bg (#f8f9fa or #0f0f1a)\n"
            "   Status colors: green=#22c55e, yellow=#eab308, red=#ef4444, blue=#3b82f6\n"
            "C. Grid layout: CSS Grid for card arrangement; gap:16px\n"
            "D. Stat cards: large number + label + trend indicator (↑/↓ with color)\n"
            "E. Charts: use pure CSS/SVG bar/line/donut charts (no external libs)\n"
            "F. Tables: striped rows, sticky header, hover highlight, compact spacing\n"
            "G. Navigation: sidebar with icon + label, active state highlight\n"
            "H. Top bar: search input + notification bell + user avatar\n"
            "I. Use realistic mock data (not lorem ipsum)\n"
            "J. Typography: Inter; sidebar items 13px, card values 28px bold\n"
        ),
        blueprint=(
            "STRUCTURE:\n"
            "- Sidebar: logo + nav links (Overview, Analytics, Users, Settings) + collapse\n"
            "- Top bar: page title + search + notifications + profile\n"
            "- Main: 4 stat cards (row) → line/bar chart → data table\n"
            "- Use realistic numbers and labels\n"
        ),
        quality=(
            "Must look like a professional SaaS admin panel (Stripe/Vercel quality). "
            "Clean data hierarchy. Scannable at a glance. Pixel-perfect alignment."
        ),
        analyst_hint=(
            "Visit 2-3 premium dashboard examples (real SaaS products, Dribbble, or admin templates). "
            "If a site has captcha or bot-detection, skip it and try a different URL. "
            "Note layout pattern, card styles, chart types. SHORT summary with concrete examples."
        ),
        tester_hint=(
            "Step1: file-check; "
            "Step2: browser navigate, screenshot full layout; "
            "Step3: browser snapshot to inspect tabs/filters/buttons; "
            "Step4: click at least one filter/tab/control and MUST verify visible state changes with wait_for or a second snapshot; "
            "Step5: a PASS verdict is invalid without post-action verification evidence; "
            "Step6: check sidebar + cards + table render correctly; "
            "Step7: FAIL if browser diagnostics show runtime errors; PASS/FAIL with visual assessment."
        ),
    ),

    # ─── Tool / Utility ────────────────────────────────────────
    "tool": TaskProfile(
        task_type="tool",
        role="You are a senior full-stack engineer building polished web utilities.",
        design_system=(
            "TOOL DESIGN SYSTEM:\n"
            "A. Clean, minimal UI focused on the core function\n"
            "B. Large, clear input areas with proper labels and placeholders\n"
            "C. Instant feedback — output updates live as user types (no page reload)\n"
            "D. Color: neutral bg (#f5f5f5 or #1a1a2e dark), accent for interactive elements\n"
            "E. Copy-to-clipboard buttons on outputs (with ✓ feedback animation)\n"
            "F. Input validation with clear error states (red border + message)\n"
            "G. Keyboard shortcuts where applicable\n"
            "H. Responsive: works on mobile (min-width:320px)\n"
            "I. Clean typography: Inter; input text 14px, labels 12px\n"
            "J. Smooth transitions on all state changes (0.2s ease)\n"
        ),
        blueprint=(
            "STRUCTURE:\n"
            "- Header: tool name + brief description (one line)\n"
            "- Main: input panel (left/top) + output panel (right/bottom)\n"
            "- Options/settings row if needed\n"
            "- Footer: usage tips or keyboard shortcuts legend\n"
        ),
        quality=(
            "Must feel like a polished utility from a developer tools company. "
            "Instant responsiveness. Zero confusion about what to do."
        ),
        analyst_hint=(
            "Visit 2-3 similar tools online (search for alternatives). "
            "If a site has captcha or bot-detection, skip it and try a different URL. "
            "Note UX pattern, input/output layout, key interactions. SHORT summary."
        ),
        tester_hint=(
            "Step1: file-check; "
            "Step2: browser navigate, screenshot; "
            "Step3: check interactive elements are visible; "
            "Step4: PASS/FAIL — does it look functional and polished?"
        ),
    ),

    # ─── Presentation / Slides ──────────────────────────────────
    "presentation": TaskProfile(
        task_type="presentation",
        role="You are a presentation designer building interactive slide decks with PDF export.",
        design_system=(
            "SLIDES: Each slide 100vw×100vh. Nav: arrows+prev/next buttons+dots+F fullscreen. "
            "Transitions: CSS translateX 0.5s. Large headings clamp(2rem,4vw,4rem). "
            "One key message per slide. Slide counter + progress bar top. "
            "Color by topic: dark #0f172a for tech, white for academic, warm for history. "
            "CSS decorations only (gradients, shapes). "
            "MUST: Download PDF button (window.print). "
            "@media print { .slide{break-after:page;height:auto;min-height:100vh} "
            ".nav-controls,.progress-bar,.dots,.pdf-btn{display:none!important} "
            "*{print-color-adjust:exact} }\n"
        ),
        blueprint=(
            "STRUCTURE (8-10 slides): Title\u2192Overview\u2192Background\u2192Content\u00d74\u2192Takeaways\u2192Q&A. "
            "Nav: prev/next + dots + keyboard + PDF button.\n"
        ),
        quality=(
            "Must look like a premium Keynote presentation. "
            "Clean, bold, one-message-per-slide. Smooth transitions. "
            "PDF export must produce clean pages with no UI controls visible."
        ),
        analyst_hint=(
            "Research the topic: find 3-5 key facts, timeline, and structure ideas. "
            "Also visit 1 premium slide design for layout inspiration. Be FAST."
        ),
        tester_hint=(
            "Step1: file-check; "
            "Step2: MUST USE browser tool to navigate to http://127.0.0.1:8765/preview/, screenshot first slide; "
            "Step3: verify navigation buttons and slide dots are visible; "
            "Step4: verify Download PDF button exists; "
            "Step5: PASS/FAIL — is it a complete, navigable, professional presentation?"
        ),
    ),

    # ─── Creative / Art / Animation ─────────────────────────────
    "creative": TaskProfile(
        task_type="creative",
        role="You are a creative coder building stunning interactive visual experiences.",
        design_system=(
            "CREATIVE DESIGN SYSTEM:\n"
            "A. Full-viewport canvas (100vw × 100vh), no scroll, black or gradient bg\n"
            "B. Use <canvas> 2D or CSS animations (choose what fits best)\n"
            "C. requestAnimationFrame loop with smooth delta-time rendering\n"
            "D. Responsive canvas (resize listener, DPI-aware)\n"
            "E. Interaction: mouse/touch influences the visual (parallax, trails, attraction)\n"
            "F. Color: vibrant, artistic palette; HSL for programmatic color generation\n"
            "G. Easing functions for organic movement: ease-in-out-cubic, spring physics\n"
            "H. Performance: limit particle count, use object pooling if needed\n"
            "I. Subtle UI: small credits text + interaction hint, auto-fade after 3s\n"
            "J. Optional: audio reactivity with Web Audio API\n"
        ),
        blueprint=(
            "STRUCTURE:\n"
            "- Full-viewport container (no visible UI chrome)\n"
            "- Canvas or CSS art fills entire viewport\n"
            "- Subtle instruction text that fades ('Move your mouse...')\n"
            "- Optional: small toggle button for settings/pause\n"
        ),
        quality=(
            "Must look like a CodePen 'Picked Pen' — visually stunning, "
            "smooth, interactive, and surprising. 60fps minimum."
        ),
        analyst_hint=(
            "Visit 2-3 creative coding examples (CodePen, Shadertoy, or similar). "
            "If a site has captcha or bot-detection, skip it and try a different URL. "
            "Note animation technique, color strategy, and interaction patterns. SHORT summary."
        ),
        tester_hint=(
            "Step1: file-check; "
            "Step2: browser navigate, screenshot; "
            "Step3: check animation runs without errors; "
            "Step4: PASS/FAIL — is it visually impressive?"
        ),
    ),
}


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────

def classify(goal: str) -> TaskProfile:
    """Classify a user goal into a task type and return its profile."""
    text = (goal or "").strip()
    for task_type, pattern in _PATTERNS:
        if pattern.search(text):
            return PROFILES[task_type]
    # Default to website
    return PROFILES["website"]


def builder_system_prompt(goal: str) -> str:
    """Generate a task-adaptive builder system prompt WITH injected CSS template.
    The CSS template provides pre-built professional styling so the model
    only needs to write HTML structure and content."""
    profile = classify(goal)

    # Load pre-built CSS templates
    base_css = _load_template("base.css")
    # Map task types to template files
    type_css_map = {
        "website": "website.css",
        "presentation": "presentation.css",
        "game": "game.css",
        "dashboard": "dashboard.css",
        "tool": "website.css",  # tools use website base
        "creative": "game.css",  # creative uses game base
    }
    type_css = _load_template(type_css_map.get(profile.task_type, "website.css"))

    css_block = ""
    if base_css or type_css:
        combined = base_css
        if type_css:
            combined += "\n\n" + type_css
        css_block = (
            "\n=== PRE-BUILT CSS DESIGN SYSTEM (MUST USE) ===\n"
            "Copy this ENTIRE CSS into your <style> tag. You may customize CSS variable values "
            "(colors, sizes) to match the project, but KEEP all the component classes.\n"
            "This saves you from writing CSS from scratch — focus on HTML structure and content.\n\n"
            f"```css\n{combined}\n```\n"
            "=== END CSS ===\n\n"
        )

    return (
        f"{profile.role}\n"
        f"{_COMMON_RULES}"
        f"{css_block}"
        f"{profile.design_system}"
        f"{profile.blueprint}"
        f"Quality: {profile.quality}\n"
        f"{_COMMON_DELIVERY}"
    )


def builder_task_description(goal: str) -> str:
    """Generate a CONCISE task-adaptive builder task description for the orchestrator.
    NOTE: design_system/blueprint/knowledge are already in the system prompt — do NOT repeat them here.
    """
    profile = classify(goal)
    type_label = {
        "website": "website", "game": "HTML5 game",
        "dashboard": "dashboard/admin panel", "tool": "web utility/tool",
        "presentation": "presentation/slides", "creative": "creative visual experience",
    }.get(profile.task_type, "web application")

    return (
        f"Build a commercial-grade single-file {type_label} for: {goal}. "
        "Save final HTML via file_ops write to /tmp/evermind_output/index.html. "
        "Follow the design system and structure rules from your system prompt. "
        "Treat any upstream planner/analyst notes, loaded skills, reviewer blockers, and acceptance criteria as hard requirements, not optional inspiration. "
        "Do not use emoji glyphs in the generated product; use SVG/CSS alternatives instead. "
        "Make the result materially complete: real sections, real content, real interactions, and visible polish. "
        "For game or asset-heavy tasks, preserve a clean asset manifest / placeholder structure so dedicated asset nodes can upgrade art without rewriting the core logic. "
        "After saving, briefly describe exactly what you built and what quality checks you satisfied."
    )


def analyst_description(goal: str) -> str:
    """Generate analyst task description based on task type."""
    profile = classify(goal)
    game_research_rule = ""
    if profile.task_type == "game":
        game_research_rule = (
            "GAME RESEARCH OVERRIDE:\n"
            "- Do NOT browse playable web games as your primary workflow\n"
            "- Do NOT get stuck interacting with game portals or gameplay embeds\n"
            "- Prefer GitHub repos, source code, technical articles, tutorials, devlogs, postmortems, and engine/docs pages\n"
            "- If you inspect a reference page, extract mechanics/UI/asset insights quickly and move on\n\n"
        )
    return (
        f"{profile.analyst_hint}\n\n"
        "MANDATORY: You MUST search and visit AT LEAST 2-3 different reference websites. "
        "Do NOT stop after visiting just 1 site. If a site blocks you (captcha, 403, etc), "
        "skip it immediately and try another URL. Include ALL visited URLs in your report.\n\n"
        "REFERENCE MIX REQUIREMENT:\n"
        "- For implementation-heavy tasks, include at least 1 GitHub/source-code reference when possible\n"
        "- Include at least 1 tutorial / official doc / technical writeup when possible\n"
        "- Include visual/product references only as supporting evidence, not the whole report\n\n"
        "CRITICAL ROLE: You are not only researching. You are writing optimized downstream execution briefs "
        "for the other agents. Your report will be injected directly into builder/reviewer/tester/debugger prompts.\n\n"
        "Prompt-engineering standard you MUST follow:\n"
        "- state the concrete objective before style notes\n"
        "- separate hard constraints from optional inspiration\n"
        "- convert vague taste words into executable instructions\n"
        "- define what success looks like so reviewer/tester can enforce it\n\n"
        "OPERATING MODEL:\n"
        "- Treat this as a lightweight SOP package for the downstream nodes, not a loose inspiration memo\n"
        "- Explicitly define deliverables, completion criteria, integration order, and likely risks\n\n"
        "HARD CONSTRAINTS YOU MUST ENFORCE:\n"
        "- Generated pages must NEVER use emoji glyphs as icons, bullets, or decorative illustrations\n"
        "- Use inline SVG / CSS shapes / type treatment instead of emoji or cheap stock icon shortcuts\n"
        "- Favor premium, commercially credible layouts over generic student-project structure\n"
        "- Recommendations must be specific enough that two parallel builders can execute different scopes cleanly\n\n"
        f"{game_research_rule}"
        "Output MUST use the exact XML tags below so downstream nodes can parse them:\n"
        "<reference_sites>\n"
        "- each visited URL + what it is useful for\n"
        "</reference_sites>\n"
        "<design_direction>\n"
        "- color system\n"
        "- typography direction\n"
        "- layout rhythm\n"
        "- motion principles\n"
        "</design_direction>\n"
        "<non_negotiables>\n"
        "- concrete quality bar and hard constraints\n"
        "</non_negotiables>\n"
        "<deliverables_contract>\n"
        "- exactly what artifacts / sections / interactions must exist before this task counts as done\n"
        "</deliverables_contract>\n"
        "<risk_register>\n"
        "- likely failure points, hidden risks, and what the downstream nodes should watch carefully\n"
        "</risk_register>\n"
        "<builder_1_handoff>\n"
        "- scope, priorities, must-build sections, visual rules, implementation hints\n"
        "</builder_1_handoff>\n"
        "<builder_2_handoff>\n"
        "- scope, priorities, must-build sections, visual rules, implementation hints\n"
        "</builder_2_handoff>\n"
        "<reviewer_handoff>\n"
        "- what quality issues to be strict about\n"
        "</reviewer_handoff>\n"
        "<tester_handoff>\n"
        "- what interactions and edge cases must be verified\n"
        "</tester_handoff>\n"
        "<debugger_handoff>\n"
        "- likely failure points and how to repair them quickly\n"
        "</debugger_handoff>\n\n"
        f"Goal: {goal}"
    )


def tester_description() -> str:
    """Generate tester task description based on task type — called with goal at plan time."""
    # Returns a callable-like pattern; actual goal is injected at plan shape time
    return ""  # placeholder, actual usage is via profile.tester_hint


def design_requirements(goal: str) -> str:
    """Generate CONCISE design requirements (used in retries — keep short to avoid prompt bloat)."""
    profile = classify(goal)
    return f"Quality bar ({profile.task_type}): {profile.quality}"
