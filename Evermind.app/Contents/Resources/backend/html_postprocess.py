"""
HTML Post-Processor — Auto-fix common quality issues after builder generates HTML.
Applied after every builder write to ensure baseline quality.
"""

import re
import logging
from pathlib import Path
from typing import Callable

logger = logging.getLogger("evermind.postprocess")

_VIEWPORT_META = '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
_CHARSET_META = '<meta charset="UTF-8">'
_REMOTE_FONT_LINK_RE = re.compile(
    r"\s*<link\b[^>]*href=[\"']https://fonts\.(?:googleapis|gstatic)\.com[^\"']*[\"'][^>]*>\s*",
    re.IGNORECASE,
)
_REMOTE_FONT_IMPORT_RE = re.compile(
    r"^\s*@import\s+url\((?:[\"'])?https://fonts\.googleapis\.com/[^)]+\)\s*;?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_remote_font_resources(text: str) -> str:
    if not text:
        return text
    stripped = _REMOTE_FONT_LINK_RE.sub("\n", text)
    stripped = _REMOTE_FONT_IMPORT_RE.sub("", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped


def _detect_html_lang(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", str(text or "")[:4000]) else "en"


def repair_html_structure(html: str) -> str:
    """Repair common truncated or misordered HTML structure defects."""
    repaired = str(html or "").strip()
    if not repaired:
        return repaired

    lower = repaired.lower()
    if not any(token in lower for token in ("<!doctype", "<html", "<head", "<body")):
        return repaired

    if "<!doctype" not in lower:
        repaired = "<!DOCTYPE html>\n" + repaired
        lower = repaired.lower()

    if "<html" not in lower:
        repaired = repaired.replace(
            "<!DOCTYPE html>",
            f'<!DOCTYPE html>\n<html lang="{_detect_html_lang(repaired)}">',
            1,
        )
        lower = repaired.lower()

    if "<html" in lower and "</html>" not in lower:
        repaired = repaired.rstrip() + "\n</html>"
        lower = repaired.lower()

    if re.search(r"<html(?:\s|>)", repaired, re.IGNORECASE) and "lang=" not in repaired[:200]:
        repaired = re.sub(
            r"<html(\s|>)",
            f'<html lang="{_detect_html_lang(repaired)}"\\1',
            repaired,
            count=1,
            flags=re.IGNORECASE,
        )
        lower = repaired.lower()

    def _first_index(tokens: tuple[str, ...], start: int = 0, default: int | None = None) -> int | None:
        matches = [lower.find(token, start) for token in tokens]
        matches = [idx for idx in matches if idx >= 0]
        if matches:
            return min(matches)
        return default

    def _close_unterminated_block(tag_name: str) -> None:
        nonlocal repaired, lower
        open_matches = list(re.finditer(rf"<{tag_name}\b[^>]*>", repaired, re.IGNORECASE))
        close_matches = list(re.finditer(rf"</{tag_name}\s*>", repaired, re.IGNORECASE))
        missing = max(0, len(open_matches) - len(close_matches))
        if missing <= 0:
            return
        insert_at = _first_index(("</head>", "</body>", "</html>"), default=len(repaired))
        closing = "".join(f"\n</{tag_name}>" for _ in range(missing))
        repaired = repaired[:insert_at].rstrip() + closing + "\n" + repaired[insert_at:].lstrip()
        lower = repaired.lower()

    html_open = re.search(r"<html\b[^>]*>", repaired, re.IGNORECASE)
    head_open = re.search(r"<head\b[^>]*>", repaired, re.IGNORECASE)
    body_open = re.search(r"<body\b[^>]*>", repaired, re.IGNORECASE)
    head_close = re.search(r"</head\s*>", repaired, re.IGNORECASE)

    if head_open and body_open and head_open.start() > body_open.start():
        if head_close and head_close.start() > head_open.start():
            head_end = head_close.end()
        else:
            head_end = _first_index(("</body>", "</html>"), start=head_open.end(), default=len(repaired))
        head_block = repaired[head_open.start():head_end].strip()
        repaired = (repaired[:head_open.start()] + repaired[head_end:]).strip()
        lower = repaired.lower()
        html_open = re.search(r"<html\b[^>]*>", repaired, re.IGNORECASE)
        body_open = re.search(r"<body\b[^>]*>", repaired, re.IGNORECASE)
        insert_at = body_open.start() if body_open else (html_open.end() if html_open else 0)
        prefix = repaired[:insert_at].rstrip()
        suffix = repaired[insert_at:].lstrip()
        repaired = f"{prefix}\n{head_block}\n{suffix}".strip()
        lower = repaired.lower()
        head_open = re.search(r"<head\b[^>]*>", repaired, re.IGNORECASE)
        head_close = re.search(r"</head\s*>", repaired, re.IGNORECASE)

    if not head_open:
        html_open = re.search(r"<html\b[^>]*>", repaired, re.IGNORECASE)
        body_open = re.search(r"<body\b[^>]*>", repaired, re.IGNORECASE)
        insert_at = body_open.start() if body_open else (html_open.end() if html_open else 0)
        head_block = "\n<head>\n</head>\n"
        repaired = repaired[:insert_at] + head_block + repaired[insert_at:]
        lower = repaired.lower()
        head_open = re.search(r"<head\b[^>]*>", repaired, re.IGNORECASE)
        head_close = re.search(r"</head\s*>", repaired, re.IGNORECASE)
        body_open = re.search(r"<body\b[^>]*>", repaired, re.IGNORECASE)

    if head_open and (not head_close or (body_open and head_close.start() > body_open.start())):
        insert_at = body_open.start() if body_open else _first_index(("</html>",), default=len(repaired))
        repaired = repaired[:insert_at].rstrip() + "\n</head>\n" + repaired[insert_at:].lstrip()
        lower = repaired.lower()
        head_close = re.search(r"</head\s*>", repaired, re.IGNORECASE)

    if "<body" not in lower:
        html_open = re.search(r"<html\b[^>]*>", repaired, re.IGNORECASE)
        head_close = re.search(r"</head\s*>", repaired, re.IGNORECASE)
        insert_at = head_close.end() if head_close else (html_open.end() if html_open else len(repaired))
        repaired = repaired[:insert_at] + "\n<body>\n" + repaired[insert_at:]
        lower = repaired.lower()

    _close_unterminated_block("style")
    _close_unterminated_block("script")

    body_close_idx = lower.rfind("</body>")
    html_close_idx = lower.rfind("</html>")
    if body_close_idx < 0:
        insert_at = html_close_idx if html_close_idx >= 0 else len(repaired)
        repaired = repaired[:insert_at].rstrip() + "\n</body>\n" + repaired[insert_at:].lstrip()
        lower = repaired.lower()
        html_close_idx = lower.rfind("</html>")
    if html_close_idx < 0:
        repaired = repaired.rstrip() + "\n</html>"

    return repaired


def _add_class_aliases(html: str, required_token: str, alias_tokens: list[str]) -> str:
    token_re = re.compile(
        r'class\s*=\s*(["\'])([^"\']*\b' + re.escape(required_token) + r'\b[^"\']*)\1',
        re.IGNORECASE,
    )

    def _repl(match: re.Match[str]) -> str:
        quote = match.group(1)
        classes = str(match.group(2) or "").split()
        changed = False
        for alias in alias_tokens:
            if alias and alias not in classes:
                classes.append(alias)
                changed = True
        if not changed:
            return match.group(0)
        return f'class={quote}{" ".join(classes)}{quote}'

    return token_re.sub(_repl, html)


def _apply_selector_fallback(js: str, needle: str, replacement: str) -> str:
    if needle not in js or replacement in js:
        return js
    return js.replace(needle, replacement)


def _guard_optional_js_hook_lines(js: str, identifier: str) -> str:
    if not js or not identifier:
        return js
    guarded_lines: list[str] = []
    changed = False
    prefix_patterns = (
        f"{identifier}.classList.",
        f"{identifier}.addEventListener(",
        f"{identifier}.removeEventListener(",
        f"{identifier}.style.",
        f"{identifier}.querySelector(",
        f"{identifier}.querySelectorAll(",
        f"{identifier}.getBoundingClientRect(",
        f"{identifier}.scrollIntoView(",
        f"{identifier}.focus(",
        f"{identifier}.setAttribute(",
        f"{identifier}.removeAttribute(",
        f"{identifier}.append(",
        f"{identifier}.appendChild(",
        f"{identifier}.remove(",
        f"{identifier}.matches(",
        f"{identifier}.textContent",
        f"{identifier}.innerHTML",
        f"{identifier}.dataset.",
        f"{identifier}.value",
        f"{identifier}.checked",
        f"{identifier}.animate(",
    )
    for line in js.splitlines():
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith(prefix_patterns) and not stripped.startswith(f"if ({identifier}) "):
            guarded_lines.append(f"{indent}if ({identifier}) {stripped}")
            changed = True
        else:
            guarded_lines.append(line)
    if not changed:
        return js
    return "\n".join(guarded_lines) + ("\n" if js.endswith("\n") else "")


def postprocess_javascript(js: str) -> str:
    if not js or not js.strip():
        return js

    original = js
    replacements = [
        (
            "document.getElementById('nav')",
            "(document.getElementById('nav') || document.getElementById('mainNav') || document.querySelector('.main-nav'))",
        ),
        (
            'document.getElementById("nav")',
            "(document.getElementById('nav') || document.getElementById('mainNav') || document.querySelector('.main-nav'))",
        ),
        (
            "document.getElementById('navMenu')",
            "(document.getElementById('navMenu') || document.getElementById('navLinks') || document.querySelector('#nav .nav-links, #mainNav .nav-links, .main-nav .nav-links'))",
        ),
        (
            'document.getElementById("navMenu")',
            "(document.getElementById('navMenu') || document.getElementById('navLinks') || document.querySelector('#nav .nav-links, #mainNav .nav-links, .main-nav .nav-links'))",
        ),
        (
            "document.querySelector('.nav-toggle')",
            "document.querySelector('.nav-toggle, #navToggle, .mobile-menu-toggle, #mobileMenuToggle')",
        ),
        (
            'document.querySelector(".nav-toggle")',
            "document.querySelector('.nav-toggle, #navToggle, .mobile-menu-toggle, #mobileMenuToggle')",
        ),
        (
            "document.querySelector('.page-transition-overlay')",
            "document.querySelector('.page-transition-overlay, .page-transition')",
        ),
        (
            'document.querySelector(".page-transition-overlay")',
            "document.querySelector('.page-transition-overlay, .page-transition')",
        ),
        (
            "document.querySelector('.page-transition')",
            "document.querySelector('.page-transition, .page-transition-overlay')",
        ),
        (
            'document.querySelector(".page-transition")',
            "document.querySelector('.page-transition, .page-transition-overlay')",
        ),
        (
            "$('#nav')",
            "($('#nav') || $('#mainNav') || $('.main-nav'))",
        ),
        (
            '$("#nav")',
            "($('#nav') || $('#mainNav') || $('.main-nav'))",
        ),
        (
            "$('#navMenu')",
            "($('#navMenu') || $('#navLinks') || $('#nav .nav-links') || $('#mainNav .nav-links') || $('.main-nav .nav-links'))",
        ),
        (
            '$("#navMenu")',
            "($('#navMenu') || $('#navLinks') || $('#nav .nav-links') || $('#mainNav .nav-links') || $('.main-nav .nav-links'))",
        ),
        (
            "$('.nav-toggle')",
            "$('.nav-toggle, #navToggle, .mobile-menu-toggle, #mobileMenuToggle')",
        ),
        (
            '$(".nav-toggle")',
            "$('.nav-toggle, #navToggle, .mobile-menu-toggle, #mobileMenuToggle')",
        ),
        (
            "$('.page-transition-overlay')",
            "$('.page-transition-overlay, .page-transition')",
        ),
        (
            '$(".page-transition-overlay")',
            "$('.page-transition-overlay, .page-transition')",
        ),
        (
            "$('.page-transition')",
            "$('.page-transition, .page-transition-overlay')",
        ),
        (
            '$(".page-transition")',
            "$('.page-transition, .page-transition-overlay')",
        ),
    ]
    for needle, replacement in replacements:
        js = _apply_selector_fallback(js, needle, replacement)

    for identifier in ("nav", "overlay"):
        js = _guard_optional_js_hook_lines(js, identifier)

    if js != original:
        logger.info("Post-processed JavaScript: normalized shared selector fallbacks")
    return js


def postprocess_generated_text(text: str, filename: str = "", task_type: str = "website") -> str:
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix in {".html", ".htm"}:
        return postprocess_html(text, task_type=task_type)
    if suffix == ".css":
        return postprocess_stylesheet(text)
    if suffix == ".js":
        return postprocess_javascript(text)
    return text


def postprocess_stylesheet(css: str) -> str:
    if not css or not css.strip():
        return css

    original = css
    css = _strip_remote_font_resources(css)

    if css != original:
        logger.info("Post-processed stylesheet: removed remote font imports")
    return css


def postprocess_html(html: str, task_type: str = "website") -> str:
    """Apply automatic quality fixes to generated HTML.
    
    Args:
        html: Raw HTML string from builder
        task_type: One of website/game/dashboard/tool/presentation/creative
    
    Returns:
        Fixed HTML string
    """
    if not html or not html.strip():
        return html

    html = _strip_remote_font_resources(html)
    original = html
    html = repair_html_structure(html)


    # 1. Ensure charset meta
    if "<meta charset" not in html.lower():
        html = html.replace("<head>", f"<head>\n    {_CHARSET_META}", 1)

    # 2. Ensure viewport meta
    if "viewport" not in html.lower():
        if "<head>" in html:
            html = html.replace("<head>", f"<head>\n    {_VIEWPORT_META}", 1)

    # 3. Ensure lang attribute on <html>
    if re.search(r'<html(?:\s|>)', html) and 'lang=' not in html[:200]:
        # Detect language from content
        lang = "zh" if re.search(r'[\u4e00-\u9fff]', html[:2000]) else "en"
        html = re.sub(r'<html(\s|>)', f'<html lang="{lang}"\\1', html, count=1)

    # 4. Ensure prefers-reduced-motion
    if "prefers-reduced-motion" not in html and "animation" in html.lower():
        motion_css = (
            "\n    @media (prefers-reduced-motion: reduce) {\n"
            "      *, *::before, *::after { animation-duration: 0.01ms !important; "
            "transition-duration: 0.01ms !important; }\n"
            "    }\n"
        )
        # Insert before </style>
        if "</style>" in html:
            html = html.replace("</style>", f"{motion_css}  </style>", 1)

    # 5. For presentations: ensure print styles
    if task_type == "presentation" and "@media print" not in html:
        print_css = (
            "\n    @media print {\n"
            "      .slide { position: relative !important; break-after: page; "
            "height: auto; min-height: 100vh; opacity: 1 !important; transform: none !important; }\n"
            "      .nav-controls, .progress-bar, .dots, .pdf-btn, .fullscreen-btn, "
            ".slide-counter { display: none !important; }\n"
            "      * { print-color-adjust: exact !important; -webkit-print-color-adjust: exact !important; }\n"
            "      body { overflow: visible; }\n"
            "    }\n"
        )
        if "</style>" in html:
            html = html.replace("</style>", f"{print_css}  </style>", 1)

    # 6. Remove emoji glyphs used as icons (should use SVG)
    # Only strip common UI emoji, not content emoji
    ui_emoji_pattern = re.compile(r'[\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF]')
    # Don't be too aggressive — only in elements that look like buttons/nav
    # Skip this for now to avoid breaking content

    # 7. Normalize common multi-page hook class aliases so shared assets can bind safely.
    html = _add_class_aliases(html, "page-transition", ["page-transition-overlay"])
    html = _add_class_aliases(html, "page-transition-overlay", ["page-transition"])
    html = _add_class_aliases(html, "mobile-menu-toggle", ["nav-toggle"])
    # 8. Fix unconstrained SVG: add width/height to inline SVGs that only have viewBox
    def _fix_svg_sizing(match: re.Match[str]) -> str:
        tag = match.group(0)
        # Skip if already has width or height
        if re.search(r'\bwidth\s*=', tag, re.IGNORECASE):
            return tag
        if re.search(r'\bheight\s*=', tag, re.IGNORECASE):
            return tag
        # Extract viewBox to determine appropriate size
        vb_match = re.search(r'viewBox\s*=\s*["\'][\d\s.]+["\']', tag, re.IGNORECASE)
        size = '48'  # default icon size
        if vb_match:
            vb_val = vb_match.group(0)
            nums = re.findall(r'[\d.]+', vb_val)
            if len(nums) >= 4:
                vb_w = float(nums[2])
                if vb_w <= 24:
                    size = '24'
                elif vb_w <= 48:
                    size = '48'
                else:
                    size = '64'
        # Insert width and height right after <svg
        return tag.replace('<svg', f'<svg width="{size}" height="{size}"', 1).replace(
            '<SVG', f'<SVG width="{size}" height="{size}"', 1
        )

    svg_open_re = re.compile(r'<svg\b[^>]*>', re.IGNORECASE)
    html = svg_open_re.sub(_fix_svg_sizing, html)

    if html != original:
        logger.info(f"Post-processed HTML: applied fixes for {task_type}")

    return html
