"""
Evermind Knowledge Base — Compact Design & Engineering Knowledge

Injected into builder prompts to improve output quality and reduce rework.
Keep this file LEAN — every character costs model processing time.
"""

# ─────────────────────────────────────────────────────────────────
# Core Design Knowledge (compact version)
# ─────────────────────────────────────────────────────────────────

DESIGN_KNOWLEDGE = """
DESIGN RULES (apply these):
- Colors: never pure #000/#fff; use #1a1a2e/#fafafa. Limit to 1 primary + 1 accent + 3 neutrals. All as CSS vars.
- Typography: Inter 400/600/700; scale 14/16/20/24/32/48px; line-height body 1.6, headings 1.2; max-width 680px on text.
- Spacing: 8px grid (8/16/24/32/48/64px); sections 64-96px vertical; cards 24-32px padding.
- Shadows: subtle 0 1px 3px, hover 0 4px 12px, modal 0 8px 32px; transition on hover.
- Animation: 150-200ms micro, 300-500ms reveals; cubic-bezier(0.4,0,0.2,1); stagger 50-100ms; prefers-reduced-motion.
- Modern CSS: glassmorphism (backdrop-filter:blur), gradient text (background-clip:text), glow buttons, hover lift.
- Responsive: breakpoints 480/768/1024px; mobile-first; touch targets ≥44px; stack on mobile.
- Accessibility: :focus-visible, semantic HTML, aria-labels, skip-link, contrast ≥4.5:1.
- AVOID: px font-size (use rem), horizontal scroll, text over images without overlay, placeholder text, !important.
""".strip()

CODING_KNOWLEDGE = """
CODE RULES:
- HTML: semantic elements, proper nesting. CSS: variables for all values, no inline styles. JS: const/let, no var.
- Events: addEventListener, event delegation. Performance: will-change, debounce scroll/resize.
- Single self-contained index.html, no external dependencies.
""".strip()

# ─────────────────────────────────────────────────────────────────
# Presentation-specific (only for PPT tasks)
# ─────────────────────────────────────────────────────────────────

PRESENTATION_KNOWLEDGE = """
PPT RULES:
- Each slide: 100vw×100vh, no scroll. Nav: arrow keys + clickable prev/next + dots. Progress bar at top.
- @media print { .slide{break-after:page;height:auto;min-height:100vh} .nav-controls,.progress-bar,.dots{display:none!important} *{print-color-adjust:exact} }
- Include "Download PDF" button (window.print). Show all slides stacked in print. Slide counter in corner.
- One key message per slide. Title slide: name+subtitle+date. Large headings clamp(2rem,4vw,4rem).
""".strip()


def get_knowledge_for_task(task_type: str) -> str:
    """Return combined knowledge string for the given task type."""
    parts = [DESIGN_KNOWLEDGE, CODING_KNOWLEDGE]
    if task_type == "presentation":
        parts.append(PRESENTATION_KNOWLEDGE)
    return "\n\n".join(parts)
