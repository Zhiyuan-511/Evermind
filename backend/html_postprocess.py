"""
HTML Post-Processor — Auto-fix common quality issues after builder generates HTML.
Applied after every builder write to ensure baseline quality.
"""

import re
import logging
from pathlib import Path

logger = logging.getLogger("evermind.postprocess")

# Google Font import tag
_FONT_IMPORT = "<link href=\"https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap\" rel=\"stylesheet\">"
_VIEWPORT_META = '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
_CHARSET_META = '<meta charset="UTF-8">'


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

    original = html

    # 1. Ensure charset meta
    if "<meta charset" not in html.lower():
        html = html.replace("<head>", f"<head>\n    {_CHARSET_META}", 1)

    # 2. Ensure viewport meta
    if "viewport" not in html.lower():
        if "<head>" in html:
            html = html.replace("<head>", f"<head>\n    {_VIEWPORT_META}", 1)

    # 3. Ensure Google Font import (Inter)
    if "fonts.googleapis.com" not in html and "Inter" not in html:
        if "<head>" in html:
            # Insert before </head>
            html = html.replace("</head>", f"    {_FONT_IMPORT}\n</head>", 1)

    # 4. Ensure lang attribute on <html>
    if re.search(r'<html(?:\s|>)', html) and 'lang=' not in html[:200]:
        # Detect language from content
        lang = "zh" if re.search(r'[\u4e00-\u9fff]', html[:2000]) else "en"
        html = re.sub(r'<html(\s|>)', f'<html lang="{lang}"\\1', html, count=1)

    # 5. Ensure prefers-reduced-motion
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

    # 6. For presentations: ensure print styles
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

    # 7. Remove emoji glyphs used as icons (should use SVG)
    # Only strip common UI emoji, not content emoji
    ui_emoji_pattern = re.compile(r'[\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF]')
    # Don't be too aggressive — only in elements that look like buttons/nav
    # Skip this for now to avoid breaking content

    if html != original:
        logger.info(f"Post-processed HTML: applied fixes for {task_type}")

    return html
