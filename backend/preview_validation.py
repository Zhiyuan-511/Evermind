"""
Preview validation utilities for Evermind.

Design goals:
1. Fast, deterministic structural checks for generated HTML.
2. Optional deep smoke check via Playwright (if runtime/browser is available).
3. Safe path resolution from preview URLs to local output artifacts.
"""

from __future__ import annotations

import asyncio
from html import unescape
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from hashlib import sha1
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote

from release_doctor import build_release_doctor_report
from runtime_paths import resolve_output_dir, resolve_state_dir

try:
    from PIL import Image, ImageChops, ImageOps, ImageStat
except Exception:  # pragma: no cover - graceful fallback when Pillow is unavailable
    Image = None
    ImageChops = None
    ImageOps = None
    ImageStat = None


OUTPUT_DIR = resolve_output_dir()
DEFAULT_PORT = int(os.getenv("PORT", "8765"))
MIN_HTML_BYTES = int(os.getenv("EVERMIND_MIN_HTML_BYTES", "1200"))
MIN_CSS_RULES = int(os.getenv("EVERMIND_MIN_CSS_RULES", "10"))
MIN_SEMANTIC_BLOCKS = int(os.getenv("EVERMIND_MIN_SEMANTIC_BLOCKS", "4"))
PLAYWRIGHT_STATUS_CACHE_TTL_SEC = int(os.getenv("EVERMIND_PLAYWRIGHT_STATUS_TTL_SEC", "30"))
VISUAL_BASELINE_DIR = resolve_state_dir() / "visual_baselines"
VISUAL_CAPTURE_DIRNAME = "_visual_regression"
VISUAL_DIFF_PIXEL_THRESHOLD = int(os.getenv("EVERMIND_VISUAL_DIFF_PIXEL_THRESHOLD", "18"))
VISUAL_DIFF_WARN_RATIO = float(os.getenv("EVERMIND_VISUAL_DIFF_WARN_RATIO", "0.18"))
VISUAL_DIFF_FAIL_RATIO = float(os.getenv("EVERMIND_VISUAL_DIFF_FAIL_RATIO", "0.42"))
VISUAL_DIFF_WARN_AREA_RATIO = float(os.getenv("EVERMIND_VISUAL_DIFF_WARN_AREA_RATIO", "0.22"))
VISUAL_DIFF_FAIL_AREA_RATIO = float(os.getenv("EVERMIND_VISUAL_DIFF_FAIL_AREA_RATIO", "0.48"))
SMOKE_BLANK_GAP_MIN_PX = int(os.getenv("EVERMIND_SMOKE_BLANK_GAP_MIN_PX", "720"))
_PLAYWRIGHT_STATUS_CACHE: Dict[str, object] = {"ts": 0.0, "value": {"available": False, "reason": "not_checked"}}
_PARTIAL_HTML_RE = re.compile(r"^(index_part\d+|_partial_builder)\.html?$", re.IGNORECASE)
_BOOTSTRAP_HTML_RE = re.compile(
    r'<meta[^>]+name=["\']evermind-bootstrap["\'][^>]+content=["\']pending["\']'
    r'|<meta[^>]+content=["\']pending["\'][^>]+name=["\']evermind-bootstrap["\']',
    re.IGNORECASE,
)
_TRUNCATION_MARKER_RE = re.compile(r"(?:\.\.\.\s*)?\[TRUNCATED\]", re.IGNORECASE)
_SCRIPT_OPEN_TAG_RE = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
_SCRIPT_CLOSE_TAG_RE = re.compile(r"</script\s*>", re.IGNORECASE)
_INLINE_HANDLER_ATTR_RE = re.compile(r"\s(on[a-z]+)\s*=\s*([\"'])(.*?)\2", re.IGNORECASE | re.DOTALL)
_VISUAL_CAPTURE_SPECS: List[Dict[str, Any]] = [
    {"name": "desktop_fold", "width": 1440, "height": 960, "full_page": False},
    {"name": "desktop_full", "width": 1440, "height": 960, "full_page": True},
    {"name": "mobile_fold", "width": 390, "height": 844, "full_page": False},
]


def _normalize_preview_rel_path(preview_url: str) -> Optional[str]:
    """
    Extract relative preview path from a URL like:
      http://127.0.0.1:8765/preview/task_1/index.html
    Returns:
      task_1/index.html
    """
    if not preview_url or not isinstance(preview_url, str):
        return None
    try:
        parsed = urlparse(preview_url)
    except Exception:
        return None
    path = parsed.path or ""
    if "/preview/" not in path:
        return None
    rel = path.split("/preview/", 1)[1].lstrip("/")
    rel = unquote(rel)
    if not rel:
        return None
    return rel


def resolve_preview_file(preview_url: str, output_dir: Optional[Path] = None) -> Optional[Path]:
    out = output_dir or OUTPUT_DIR
    rel = _normalize_preview_rel_path(preview_url)
    if not rel:
        return None
    candidate = (out / rel).resolve()
    try:
        candidate.relative_to(out.resolve())
    except Exception:
        return None
    return candidate


def build_preview_url_for_file(html_file: Path, output_dir: Optional[Path] = None, port: Optional[int] = None) -> str:
    out = (output_dir or OUTPUT_DIR).resolve()
    html_file = html_file.resolve()
    rel = html_file.relative_to(out).as_posix()
    return f"http://127.0.0.1:{port or DEFAULT_PORT}/preview/{rel}"


def is_partial_html_artifact(artifact: Path | str) -> bool:
    path = artifact if isinstance(artifact, Path) else Path(str(artifact))
    return bool(_PARTIAL_HTML_RE.match(path.name))


def is_bootstrap_html_content(html: str) -> bool:
    text = str(html or "")
    return bool(_BOOTSTRAP_HTML_RE.search(text)) or "<!-- evermind-bootstrap scaffold -->" in text.lower()


def is_bootstrap_html_artifact(artifact: Path | str) -> bool:
    path = artifact if isinstance(artifact, Path) else Path(str(artifact))
    if path.suffix.lower() not in (".html", ".htm"):
        return False
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            sample = fh.read(8192)
    except Exception:
        return False
    return is_bootstrap_html_content(sample)


def has_truncation_marker(text: str) -> bool:
    return bool(_TRUNCATION_MARKER_RE.search(str(text or "")))


def inspect_html_integrity(html: str) -> Dict[str, Any]:
    lower = str(html or "").lower()
    errors: List[str] = []
    if has_truncation_marker(html):
        errors.append("Contains literal truncation marker")
    if is_bootstrap_html_content(html):
        errors.append("Still contains Evermind bootstrap scaffold marker")
    for token, message in (
        ("<html", "Missing <html> tag"),
        ("<head", "Missing <head> tag"),
        ("<body", "Missing <body> tag"),
        ("</html>", "Missing </html> closing tag"),
    ):
        if token not in lower:
            errors.append(message)
    script_integrity = inspect_script_tag_integrity(html)
    errors.extend(script_integrity.get("errors", []))
    handler_errors = _inspect_orphan_inline_handlers(html)
    errors.extend(handler_errors)
    truncation_errors = _inspect_trailing_truncation(html)
    errors.extend(truncation_errors)
    interactive_errors = _inspect_interactive_without_script(html)
    errors.extend(interactive_errors)
    return {"ok": not errors, "errors": errors}


# v7.7 (2026-04-27): catch builder finish=stop truncation that leaves the
# DOM with an unclosed tag right before </body>. Postprocess auto-appends
# </body></html> to "fix" markup, masking the actual truncation point —
# so by the time we see the artifact, the page looks well-formed but
# everything after the cut is missing (entire <script> block, etc).
# Pattern observed in real runs:
#   <span class\n</body>          (incomplete attribute on open tag)
#   <kbd>.</\n</body>             (incomplete closing tag)
# Both leave a dangling `<` in the last ~120 chars before `</body>`.
_TRAILING_OPEN_TAG_RE = re.compile(
    r"<\s*[a-zA-Z][a-zA-Z0-9-]*(?:\s+[a-zA-Z-]+(?:\s*=\s*(?:\"[^\"]*\"?|'[^']*'?|[^\s>]*))?)*\s*$"
)
_TRAILING_PARTIAL_CLOSE_RE = re.compile(r"<\s*/\s*[a-zA-Z]*\s*$")


def _inspect_trailing_truncation(html: str) -> List[str]:
    text = str(html or "")
    if not text:
        return []
    # Find the last </body> position. Postprocess auto-appends one even on
    # truncated streams, so we slice the content BEFORE it.
    body_close_match = re.search(r"</body\s*>", text, re.IGNORECASE)
    candidate = text[:body_close_match.start()] if body_close_match else text
    # Strip trailing whitespace, look at the final non-whitespace segment.
    tail = candidate.rstrip()
    if not tail:
        return []
    last_chunk = tail[-200:]
    # If the last chunk ends with an unclosed `<...` open tag → truncation.
    if _TRAILING_OPEN_TAG_RE.search(last_chunk):
        return [
            "HTML appears truncated mid-tag near </body> "
            "(unclosed opening tag in trailing content). "
            "Likely a builder finish=stop before completing the page; "
            "postprocess auto-appended </body></html> but content after "
            "the cut (often the entire <script> block) is missing."
        ]
    if _TRAILING_PARTIAL_CLOSE_RE.search(last_chunk):
        return [
            "HTML appears truncated mid-closing-tag near </body> "
            "(incomplete `</tag` pattern). "
            "Likely a builder finish=stop before completing the page."
        ]
    return []


# v7.7 (2026-04-27): if the page has interactive surface (data-action
# buttons, form inputs with IDs that imply JS wiring, etc.) but NO inline
# <script> at all, it's a non-functional skeleton. Calculator/todo/timer
# style utilities all rely on inline JS — without it every click is dead.
# This covers the case where builder finish=stop happened BEFORE the
# <script> opening tag was even written, so orphan-handler detection
# (which needs `onclick=` references) doesn't fire.
_INTERACTIVE_DATA_HOOK_RE = re.compile(
    r'\bdata-(?:action|key|op|value)\s*=\s*["\'][^"\']+["\']',
    re.IGNORECASE,
)


def _inspect_interactive_without_script(html: str) -> List[str]:
    text = str(html or "")
    lower = text.lower()
    # A page with <button> AND data-action/key/op hooks declares an
    # interactive contract — every click must fire JS. Bare static pages
    # (article/landing/marketing) rarely use data-action.
    if not _INTERACTIVE_DATA_HOOK_RE.search(text):
        return []
    if "<button" not in lower:
        return []
    # Concatenate inline-script bodies; ignore externals.
    inline_js_parts: List[str] = []
    for match in _INLINE_SCRIPT_BODY_RE.finditer(text):
        attrs = str(match.group("attrs") or "")
        if "src=" in attrs.lower():
            continue
        inline_js_parts.append(str(match.group("body") or ""))
    inline_js = "\n".join(inline_js_parts).strip()
    # Treat <50 chars of JS as effectively empty — typical truncation
    # leaves either zero bytes or a stub like `"use strict";` only.
    if len(inline_js) < 50:
        return [
            "Page declares interactive controls (data-action buttons) but no "
            "substantive inline <script> defines the handlers — clicks are "
            "dead. Likely a builder truncation before the JS block."
        ]
    return []


# v7.7 (2026-04-27): orphan inline-handler detector. Catches the
# "builder finish=stop mid-content" failure mode where the model emits
# `<button onclick="calc.appendNum('7')">` but never streams the
# `<script>const calc = {...}</script>` block. Postprocess auto-closes
# `</body></html>` so basic tag checks pass — but every button is dead
# because the namespace `calc` is undefined. inspect_html_integrity
# now flags this as a hard failure so the builder retry path kicks in
# instead of saving a non-functional artifact.
_INLINE_HANDLER_RE = re.compile(
    r'\bon(?:click|change|input|submit|keydown|keyup|keypress|mousedown|mouseup|mouseover|mouseout|mousemove|focus|blur|load|unload|dblclick|touchstart|touchend|touchmove)\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_HANDLER_ROOT_IDENT_RE = re.compile(r"^\s*(?:return\s+|new\s+|await\s+|!\s*)?([A-Za-z_$][A-Za-z0-9_$]*)")
_INLINE_SCRIPT_BODY_RE = re.compile(
    r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>", re.DOTALL | re.IGNORECASE,
)
# JS keywords that look like identifiers but aren't user-defined namespaces.
_JS_KEYWORD_ROOTS = {
    "this", "window", "document", "self", "globalThis", "navigator", "location",
    "console", "alert", "confirm", "prompt", "void", "typeof", "instanceof",
    "true", "false", "null", "undefined", "NaN", "Infinity",
    "Math", "Date", "Object", "Array", "String", "Number", "Boolean", "Symbol",
    "JSON", "RegExp", "Error", "Promise", "Map", "Set", "WeakMap", "WeakSet",
    "Function", "Reflect", "Proxy", "ArrayBuffer", "DataView",
    "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame",
    "fetch", "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
    "parseInt", "parseFloat", "isNaN", "isFinite",
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "break", "continue", "return", "throw", "try", "catch", "finally",
    "var", "let", "const", "function", "class", "import", "export",
    "event", "e", "ev",
}


def _inspect_orphan_inline_handlers(html: str) -> List[str]:
    text = str(html or "")
    handlers = list(_INLINE_HANDLER_RE.finditer(text))
    if not handlers:
        return []

    # Collect the union of identifier roots referenced from inline handlers.
    referenced_roots: Dict[str, int] = {}
    for match in handlers:
        expr = str(match.group(1) or "").strip()
        if not expr:
            continue
        # Split on `;` so multi-statement handlers contribute every root.
        for stmt in expr.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            ident_match = _HANDLER_ROOT_IDENT_RE.match(stmt)
            if not ident_match:
                continue
            root = str(ident_match.group(1) or "").strip()
            if not root or root in _JS_KEYWORD_ROOTS:
                continue
            referenced_roots[root] = referenced_roots.get(root, 0) + 1

    if not referenced_roots:
        return []

    # Concatenate all inline <script> bodies (skip src= externals — we can't
    # resolve those statically, give them benefit of doubt).
    inline_js_parts: List[str] = []
    for match in _INLINE_SCRIPT_BODY_RE.finditer(text):
        attrs = str(match.group("attrs") or "")
        if "src=" in attrs.lower():
            continue
        inline_js_parts.append(str(match.group("body") or ""))
    inline_js = "\n".join(inline_js_parts)

    if not inline_js.strip() and referenced_roots:
        return [
            "Inline handlers reference identifiers ({roots}) but no inline <script> defines them — "
            "page is non-functional (clicks will throw 'X is not defined').".format(
                roots=", ".join(sorted(referenced_roots)[:5]),
            )
        ]

    missing: List[str] = []
    for root in sorted(referenced_roots):
        # Look for a definition: const|let|var|function|class root = ... OR
        # `root =` at line start OR `window.root =`. We accept any of those.
        patterns = [
            rf"\b(?:const|let|var)\s+{re.escape(root)}\b",
            rf"\bfunction\s+{re.escape(root)}\b",
            rf"\bclass\s+{re.escape(root)}\b",
            rf"\bwindow\.{re.escape(root)}\s*=",
            rf"^\s*{re.escape(root)}\s*=",  # bare assignment at line start
        ]
        if any(re.search(p, inline_js, re.MULTILINE) for p in patterns):
            continue
        missing.append(root)

    if missing:
        return [
            "Inline handlers reference undefined identifiers ({names}) — "
            "page will throw 'X is not defined' on user interaction. "
            "Likely a builder truncation mid-stream.".format(names=", ".join(missing[:5]))
        ]
    return []


def inspect_script_tag_integrity(html: str) -> Dict[str, Any]:
    text = str(html or "")
    errors: List[str] = []
    warnings: List[str] = []

    depth = 0
    stray_closers = 0
    token_re = re.compile(r"<script\b[^>]*>|</script\s*>", re.IGNORECASE)
    for match in token_re.finditer(text):
        token = str(match.group(0) or "").lower()
        if token.startswith("</script"):
            if depth <= 0:
                stray_closers += 1
            else:
                depth -= 1
        else:
            depth += 1

    if stray_closers:
        noun = "</script> tag" if stray_closers == 1 else "</script> tags"
        errors.append(f"Found {stray_closers} stray {noun} before any matching <script> opener")

    if depth > 0:
        missing = depth
        noun = "script block" if missing == 1 else "script blocks"
        errors.append(f"Missing </script> closing tag for {missing} {noun}")
    elif stray_closers:
        extra = stray_closers
        noun = "</script> tag" if extra == 1 else "</script> tags"
        warnings.append(f"Found {extra} extra {noun} without a matching <script> opener")

    return {"errors": errors, "warnings": warnings}


def _preferred_preview_candidate(html_files: List[Path], *, bucket_root: Optional[Path] = None) -> Optional[Path]:
    eligible = [
        path for path in html_files
        if path.is_file() and not is_partial_html_artifact(path) and not is_bootstrap_html_artifact(path)
    ]
    if not eligible:
        return None

    root = None
    if bucket_root is not None:
        try:
            root = bucket_root.resolve()
        except Exception:
            root = bucket_root

    def _sort_key(path: Path) -> Tuple[int, int, str]:
        try:
            rel = path.resolve().relative_to(root) if root is not None else path
        except Exception:
            rel = path
        rel_str = rel.as_posix() if isinstance(rel, Path) else str(rel)
        depth = len(rel.parts) if isinstance(rel, Path) else len(Path(rel_str).parts)
        return (
            0 if path.name.lower() == "index.html" else 1,
            depth,
            rel_str.lower(),
        )

    eligible.sort(key=_sort_key)
    return eligible[0]


def _latest_mtime(paths: List[Path]) -> float:
    latest = 0.0
    for path in paths:
        try:
            latest = max(latest, path.stat().st_mtime)
        except Exception:
            continue
    return latest


def _extract_tag_attr(tag: str, attr: str) -> str:
    match = re.search(rf'{attr}\s*=\s*["\']([^"\']+)["\']', tag or "", re.IGNORECASE)
    return str(match.group(1) or "").strip() if match else ""


def _resolve_local_asset_href(href: str, source_file: Optional[Path]) -> Optional[Path]:
    value = str(href or "").strip()
    if not value or source_file is None:
        return None
    if value.startswith(("#", "data:", "javascript:", "mailto:", "tel:", "//")):
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme not in {"file"}:
        return None
    rel_path = unquote(parsed.path or value).strip()
    if not rel_path:
        return None
    path = Path(rel_path)
    if path.is_absolute():
        return path
    try:
        return (source_file.parent / path).resolve()
    except Exception:
        return source_file.parent / path


def collect_stylesheet_context(html: str, source_file: Optional[Path] = None) -> Dict[str, Any]:
    style_blocks = re.findall(r"<style[^>]*>(.*?)</style>", html or "", re.IGNORECASE | re.DOTALL)
    css_segments: List[str] = [segment for segment in style_blocks if str(segment).strip()]
    resolved_local_stylesheets: List[str] = []
    missing_local_stylesheets: List[str] = []

    for tag in re.findall(r"<link\b[^>]*>", html or "", re.IGNORECASE):
        rel_value = _extract_tag_attr(tag, "rel").lower()
        href_value = _extract_tag_attr(tag, "href")
        if "stylesheet" not in rel_value or not href_value:
            continue
        asset_path = _resolve_local_asset_href(href_value, source_file)
        if asset_path is None:
            continue
        if asset_path.exists() and asset_path.is_file() and asset_path.suffix.lower() == ".css":
            try:
                css_segments.append(asset_path.read_text(encoding="utf-8", errors="ignore"))
                resolved_local_stylesheets.append(str(asset_path))
            except Exception:
                missing_local_stylesheets.append(str(asset_path))
        else:
            missing_local_stylesheets.append(str(asset_path))

    return {
        "has_inline_style": bool(style_blocks),
        "has_local_stylesheet": bool(resolved_local_stylesheets),
        "css_text": "\n".join(segment for segment in css_segments if str(segment).strip()),
        "resolved_local_stylesheets": resolved_local_stylesheets,
        "missing_local_stylesheets": missing_local_stylesheets,
    }


def collect_script_context(html: str, source_file: Optional[Path] = None) -> Dict[str, Any]:
    resolved_local_scripts: List[str] = []
    missing_local_scripts: List[str] = []

    for tag in re.findall(r"<script\b[^>]*>", html or "", re.IGNORECASE):
        src_value = _extract_tag_attr(tag, "src")
        if not src_value:
            continue
        asset_path = _resolve_local_asset_href(src_value, source_file)
        if asset_path is None:
            continue
        if asset_path.exists() and asset_path.is_file() and asset_path.suffix.lower() == ".js":
            resolved_local_scripts.append(str(asset_path))
        else:
            missing_local_scripts.append(str(asset_path))

    return {
        "has_local_script": bool(resolved_local_scripts),
        "resolved_local_scripts": resolved_local_scripts,
        "missing_local_scripts": missing_local_scripts,
    }


def _script_type_is_executable_js(type_value: str) -> bool:
    normalized = str(type_value or "").strip().lower()
    if not normalized:
        return True
    normalized = normalized.split(";", 1)[0].strip()
    if normalized == "module":
        return True
    if normalized in {
        "text/javascript",
        "application/javascript",
        "text/ecmascript",
        "application/ecmascript",
        "text/jscript",
        "application/x-javascript",
        "text/x-javascript",
    }:
        return True
    return normalized.endswith("javascript") or normalized.endswith("ecmascript")


def _script_tag_is_module(tag: str) -> bool:
    normalized = str(_extract_tag_attr(tag, "type") or "").strip().lower()
    normalized = normalized.split(";", 1)[0].strip()
    return normalized == "module"


def _summarize_node_syntax_error(stderr: str) -> str:
    lines = [line.strip() for line in str(stderr or "").splitlines() if str(line).strip()]
    for line in reversed(lines):
        if "SyntaxError:" in line:
            detail = line.split("SyntaxError:", 1)[1].strip()
            return detail or line
    return lines[-1] if lines else "unknown syntax error"


def _resolve_node_binary() -> str:
    candidates = [
        str(os.getenv("EVERMIND_NODE_BINARY") or "").strip(),
        shutil.which("node") or "",
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
        str(Path.home() / ".volta" / "bin" / "node"),
    ]
    seen: set[str] = set()
    for raw in candidates:
        candidate = str(raw or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            if Path(candidate).exists() and os.access(candidate, os.X_OK):
                return candidate
        except Exception:
            continue
    return ""


def _heuristic_js_syntax_error(script_text: str, *, label: str) -> str:
    code = str(script_text or "")
    state = "normal"
    escaped = False
    stack: List[str] = []
    pairs = {"(": ")", "{": "}", "[": "]"}

    i = 0
    while i < len(code):
        ch = code[i]
        nxt = code[i + 1] if i + 1 < len(code) else ""

        if state == "line_comment":
            if ch in "\r\n":
                state = "normal"
            i += 1
            continue
        if state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "normal"
                i += 2
                continue
            i += 1
            continue
        if state in {"single", "double", "template"}:
            if escaped:
                escaped = False
                i += 1
                continue
            if ch == "\\":
                escaped = True
                i += 1
                continue
            if (
                (state == "single" and ch == "'")
                or (state == "double" and ch == '"')
                or (state == "template" and ch == "`")
            ):
                state = "normal"
            i += 1
            continue

        if ch == "/" and nxt == "/":
            state = "line_comment"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            state = "block_comment"
            i += 2
            continue
        if ch == "'":
            state = "single"
            i += 1
            continue
        if ch == '"':
            state = "double"
            i += 1
            continue
        if ch == "`":
            state = "template"
            i += 1
            continue
        if ch in pairs:
            stack.append(ch)
            i += 1
            continue
        if ch in ")}]":
            if not stack:
                return f"{label} appears to contain mismatched JavaScript delimiters near '{ch}'"
            opener = stack.pop()
            if pairs[opener] != ch:
                return f"{label} appears to contain mismatched JavaScript delimiters near '{ch}'"
        i += 1

    if state in {"single", "double", "template"}:
        return f"{label} appears truncated before a quoted JavaScript string or template literal closed"
    if state == "block_comment":
        return f"{label} appears truncated before a block comment closed"
    if stack:
        opener = stack[-1]
        return (
            f"{label} appears truncated or has unbalanced JavaScript delimiters "
            f"(missing closing token for '{opener}')"
        )

    tail = re.sub(r"\s+", " ", code).strip()
    if re.search(r"(?:[.=(,+\-*/?:]|\breturn\b|\bconst\b|\blet\b|\bvar\b)\s*$", tail):
        return f"{label} appears truncated near the end of the JavaScript payload"
    if re.search(r"\b(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*[^;]*$", tail):
        return f"{label} appears truncated near the end of the JavaScript payload"
    return ""


def _run_node_syntax_check(
    node_path: str,
    script_text: str,
    *,
    is_module: bool,
    label: str,
) -> Dict[str, Any]:
    suffix = ".mjs" if is_module else ".js"
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=suffix, delete=False) as handle:
            handle.write(str(script_text or ""))
            temp_path = Path(handle.name)
        completed = subprocess.run(
            [node_path, "--check", str(temp_path)],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "warning": f"JavaScript syntax validation timed out for {label}"}
    except Exception as exc:
        return {"ok": False, "warning": f"JavaScript syntax validation failed to run for {label}: {str(exc)[:160]}"}
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

    if completed.returncode == 0:
        return {"ok": True}

    detail = _summarize_node_syntax_error(completed.stderr)
    return {"ok": False, "error": f"{label} contains invalid JavaScript syntax: {detail}"}


def inspect_javascript_syntax(html: str, source_file: Optional[Path] = None) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    checked_scripts = 0
    text = str(html or "")
    if "<script" not in text.lower():
        return {"errors": errors, "warnings": warnings, "checked_scripts": checked_scripts}

    node_path = _resolve_node_binary()

    script_entries: List[Dict[str, Any]] = []
    inline_script_index = 0
    inline_pattern = re.compile(r"(<script\b[^>]*>)(.*?)(</script\s*>)", re.IGNORECASE | re.DOTALL)
    for match in inline_pattern.finditer(text):
        open_tag = str(match.group(1) or "")
        if _extract_tag_attr(open_tag, "src"):
            continue
        if not _script_type_is_executable_js(_extract_tag_attr(open_tag, "type")):
            continue
        code = str(match.group(2) or "")
        if not code.strip():
            continue
        inline_script_index += 1
        script_entries.append({
            "label": f"Inline script #{inline_script_index}",
            "code": code,
            "module": _script_tag_is_module(open_tag),
        })

    local_seen: set[str] = set()
    for tag_match in re.finditer(r"<script\b[^>]*>", text, re.IGNORECASE):
        open_tag = str(tag_match.group(0) or "")
        src_value = _extract_tag_attr(open_tag, "src")
        if not src_value:
            continue
        if not _script_type_is_executable_js(_extract_tag_attr(open_tag, "type")):
            continue
        asset_path = _resolve_local_asset_href(src_value, source_file)
        if asset_path is None:
            continue
        if asset_path.suffix.lower() != ".js" or not asset_path.exists() or not asset_path.is_file():
            continue
        try:
            resolved_path = asset_path.resolve()
        except Exception:
            resolved_path = asset_path
        path_key = str(resolved_path)
        if path_key in local_seen:
            continue
        local_seen.add(path_key)
        if "_evermind_runtime" in resolved_path.parts:
            continue
        try:
            code = resolved_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            warnings.append(
                f"Linked local script could not be read during JavaScript syntax validation: {resolved_path.name}"
            )
            continue
        script_entries.append({
            "label": f"Local script {resolved_path.name}",
            "code": code,
            "module": _script_tag_is_module(open_tag),
        })

    used_heuristic_only = not bool(node_path)
    for entry in script_entries:
        checked_scripts += 1
        label = str(entry.get("label") or "script")
        code = str(entry.get("code") or "")
        if node_path:
            check = _run_node_syntax_check(
                node_path,
                code,
                is_module=bool(entry.get("module")),
                label=label,
            )
            if check.get("error"):
                errors.append(str(check["error"]))
                continue
            if check.get("warning"):
                heuristic_error = _heuristic_js_syntax_error(code, label=label)
                if heuristic_error:
                    errors.append(heuristic_error)
                else:
                    warnings.append(str(check["warning"]))
                continue
        else:
            heuristic_error = _heuristic_js_syntax_error(code, label=label)
            if heuristic_error:
                errors.append(heuristic_error)
                continue

    if used_heuristic_only and checked_scripts:
        warnings.append("Node.js unavailable; used heuristic JavaScript syntax validation")

    return {
        "errors": errors,
        "warnings": warnings,
        "checked_scripts": checked_scripts,
    }


def _extract_inline_handler_calls(handler_code: str) -> Optional[List[str]]:
    code = re.sub(r"[\r\n]+", ";", unescape(str(handler_code or ""))).strip()
    if not code:
        return []

    calls: List[str] = []
    for raw_statement in code.split(";"):
        statement = raw_statement.strip()
        if not statement:
            continue
        normalized = re.sub(r"^\s*return\s+", "", statement, flags=re.IGNORECASE)
        if re.fullmatch(r"(?:true|false)", normalized, re.IGNORECASE):
            continue
        match = re.fullmatch(
            r"(?:(?:window|globalThis|self)\s*\.\s*)?([A-Za-z_$][\w$]*)\s*\((?:[\s\S]*)\)\s*",
            normalized,
            re.DOTALL,
        )
        if not match:
            return None
        calls.append(str(match.group(1) or "").strip())
    return calls


def _collect_defined_global_function_names(html: str, source_file: Optional[Path] = None) -> List[str]:
    js_chunks: List[str] = []
    inline_scripts = re.findall(
        r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>",
        html or "",
        re.IGNORECASE | re.DOTALL,
    )
    js_chunks.extend(chunk for chunk in inline_scripts if str(chunk).strip())

    script_ctx = collect_script_context(html, source_file)
    for raw_path in script_ctx.get("resolved_local_scripts", []) or []:
        try:
            js_chunks.append(Path(raw_path).read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue

    patterns = [
        re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", re.IGNORECASE),
        re.compile(
            r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
            r"(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:window|globalThis|self)\s*\.\s*([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
            r"(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)",
            re.IGNORECASE,
        ),
    ]

    names: List[str] = []
    seen = set()
    for chunk in js_chunks:
        for pattern in patterns:
            for match in pattern.finditer(chunk or ""):
                name = str(match.group(1) or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
    return names


_INLINE_HANDLER_BUILTIN_WHITELIST = {
    # Window APIs that work without `window.` prefix in inline handlers.
    "scrollto", "scrollby", "scroll", "alert", "confirm", "prompt",
    "open", "close", "focus", "blur", "print", "stop",
    "settimeout", "cleartimeout", "setinterval", "clearinterval",
    "requestanimationframe", "cancelanimationframe",
    "history.back", "history.forward", "history.go",
    # Document APIs
    "getelementbyid", "queryselector", "queryselectorall",
    # Common JS event helpers
    "preventdefault", "stoppropagation", "stopimmediatepropagation",
    # Number/Math/JSON globals (used in inline calculators)
    "parseint", "parsefloat", "isnan", "isfinite",
}


def inspect_inline_handler_contract(html: str, source_file: Optional[Path] = None) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    referenced_handlers: List[str] = []
    defined_handlers = _collect_defined_global_function_names(html, source_file)
    defined_lookup = {name.lower() for name in defined_handlers}
    seen_errors = set()

    for tag_match in re.finditer(r"<([A-Za-z][\w:-]*)\b[^>]*>", html or "", re.IGNORECASE | re.DOTALL):
        tag_text = str(tag_match.group(0) or "")
        for attr_match in _INLINE_HANDLER_ATTR_RE.finditer(tag_text):
            attr_name = str(attr_match.group(1) or "").lower()
            handler_code = str(attr_match.group(3) or "")
            calls = _extract_inline_handler_calls(handler_code)
            if calls is None:
                continue
            for fn_name in calls:
                if not fn_name:
                    continue
                referenced_handlers.append(fn_name)
                error_key = (attr_name, fn_name.lower())
                # v7.1k (maintainer 2026-04-26): whitelist Window/Document/timer
                # builtins that work without explicit `window.` prefix.
                # `scrollTo()` etc. were false-positive failing the validator
                # and forcing builder to retry the entire 30KB HTML write.
                if (
                    fn_name.lower() in defined_lookup
                    or fn_name.lower() in _INLINE_HANDLER_BUILTIN_WHITELIST
                    or error_key in seen_errors
                ):
                    continue
                seen_errors.add(error_key)
                errors.append(f"Inline event handler {attr_name} references undefined function {fn_name}()")

    return {
        "errors": errors,
        "warnings": warnings,
        "referenced_handlers": referenced_handlers,
        "defined_handlers": defined_handlers,
    }


def inspect_body_structure(html: str) -> Dict[str, Any]:
    text = str(html or "")
    lower = text.lower()
    errors: List[str] = []
    warnings: List[str] = []

    head_open = lower.find("<head")
    head_close = lower.find("</head>")
    body_open = lower.find("<body")
    body_close = lower.rfind("</body>")

    if head_open >= 0 and head_close < 0:
        errors.append("Missing </head> closing tag")
    if body_open >= 0 and head_open >= 0 and body_open < head_open:
        errors.append("Malformed document order: <body> appears before <head>")
    elif body_open >= 0 and head_close >= 0 and body_open < head_close:
        errors.append("Malformed document order: <body> starts before </head>")

    body_fragment = text
    body_match = re.search(r"<body\b[^>]*>", text, re.IGNORECASE)
    if body_match:
        start = body_match.end()
        end = body_close if body_close > start else len(text)
        body_fragment = text[start:end]

    cleaned = re.sub(r"<head\b.*?</head\s*>", " ", body_fragment, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<(style|script|noscript|template)\b.*?</\1\s*>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
    meaningful_tags = re.findall(
        r"<(?:canvas|main|section|article|header|footer|nav|aside|div|form|button|a|img|picture|video|svg|input|textarea|select|label|ul|ol|li|table|blockquote|h[1-6]|p)\b",
        cleaned,
        re.IGNORECASE,
    )
    visible_text = re.sub(r"<[^>]+>", " ", cleaned)
    visible_text = re.sub(r"\s+", " ", visible_text).strip()

    if len(meaningful_tags) == 0 and len(visible_text) < 40:
        errors.append("Body lacks meaningful visible content; artifact is effectively blank or style-only")
    elif len(meaningful_tags) < 2 and len(visible_text) < 12:
        warnings.append("Body contains very little visible content; artifact may render as a sparse or blank screen")

    return {
        "errors": errors,
        "warnings": warnings,
        "meaningful_tag_count": len(meaningful_tags),
        "visible_text_len": len(visible_text),
    }


def _html_contains_simple_selector(html: str, selector: str) -> Optional[bool]:
    source = str(html or "")
    token = str(selector or "").strip()
    if not token:
        return None
    if token.startswith(".") and re.fullmatch(r"\.[A-Za-z0-9_-]+", token):
        cls = re.escape(token[1:])
        return bool(re.search(r'class\s*=\s*["\'][^"\']*\b' + cls + r'\b[^"\']*["\']', source, re.IGNORECASE))
    if token.startswith("#") and re.fullmatch(r"#[A-Za-z][A-Za-z0-9_-]*", token):
        ident = re.escape(token[1:])
        return bool(re.search(r'id\s*=\s*["\']' + ident + r'["\']', source, re.IGNORECASE))
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", token):
        return bool(re.search(r"<" + re.escape(token) + r"\b", source, re.IGNORECASE))
    return None


def _linked_html_pages_for_script(script_path: Path, source_file: Optional[Path]) -> List[Path]:
    pages: List[Path] = []
    if source_file is None:
        return pages
    try:
        source_dir = source_file.parent.resolve()
        resolved_script = script_path.resolve()
    except Exception:
        source_dir = source_file.parent
        resolved_script = script_path

    html_candidates = sorted(list(source_dir.glob("*.html")) + list(source_dir.glob("*.htm")))
    for page in html_candidates:
        if not page.is_file():
            continue
        try:
            page_html = page.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for tag in re.findall(r"<script\b[^>]*>", page_html or "", re.IGNORECASE):
            src_value = _extract_tag_attr(tag, "src")
            if not src_value:
                continue
            asset_path = _resolve_local_asset_href(src_value, page)
            if asset_path is None:
                continue
            try:
                matches = asset_path.resolve() == resolved_script
            except Exception:
                matches = asset_path == resolved_script
            if matches:
                pages.append(page)
                break
    return pages


def _ungarded_singleton_selector_uses(js_text: str) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    risky_member_pattern = (
        r"classList|addEventListener|removeEventListener|style|querySelector|querySelectorAll|"
        r"getBoundingClientRect|scrollIntoView|focus|textContent|innerHTML|dataset|"
        r"setAttribute|removeAttribute|append|appendChild|remove|matches|value|checked"
    )
    singleton_re = re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*document\."
        r"(querySelector|getElementById)\(\s*(['\"])(.*?)\3\s*\)",
        re.DOTALL,
    )

    for match in singleton_re.finditer(js_text or ""):
        var_name = str(match.group(1) or "").strip()
        method = str(match.group(2) or "").strip()
        raw_selector = str(match.group(4) or "").strip()
        if not var_name or not raw_selector:
            continue
        selector = f"#{raw_selector}" if method == "getElementById" else raw_selector
        tail = str(js_text or "")[match.end():]
        usage_re = re.compile(rf"\b{re.escape(var_name)}\s*\.\s*({risky_member_pattern})\b")
        usages = list(usage_re.finditer(tail))
        if not usages:
            continue
        # Treat a declaration-adjacent early return as a function-level null guard.
        # Example:
        #   const hero = document.getElementById('hero');
        #   if (!hero) return;
        initial_window = tail[: usages[0].start()]
        early_guard_patterns = [
            rf"if\s*\(\s*!{re.escape(var_name)}\s*\)\s*\{{?\s*return\b",
            rf"if\s*\(\s*{re.escape(var_name)}\s*==\s*null\s*\)\s*\{{?\s*return\b",
            rf"if\s*\(\s*{re.escape(var_name)}\s*===\s*null\s*\)\s*\{{?\s*return\b",
            rf"if\s*\(\s*{re.escape(var_name)}\s*==\s*undefined\s*\)\s*\{{?\s*return\b",
            rf"if\s*\(\s*{re.escape(var_name)}\s*===\s*undefined\s*\)\s*\{{?\s*return\b",
            rf"if\s*\(\s*!{re.escape(var_name)}\s*\|\|\s*{re.escape(var_name)}\s*==\s*null\s*\)\s*\{{?\s*return\b",
        ]
        repair_patterns = [
            rf"if\s*\(\s*!{re.escape(var_name)}\s*\)\s*\{{?[\s\S]{{0,240}}?\b{re.escape(var_name)}\s*=",
            rf"if\s*\(\s*{re.escape(var_name)}\s*(?:==|===)\s*(?:null|undefined)\s*\)\s*\{{?[\s\S]{{0,240}}?\b{re.escape(var_name)}\s*=",
            rf"\b{re.escape(var_name)}\s*=\s*{re.escape(var_name)}\s*\|\|",
            rf"\b{re.escape(var_name)}\s*\?\?=",
        ]
        if any(re.search(pattern, initial_window, re.IGNORECASE | re.DOTALL) for pattern in early_guard_patterns):
            continue
        if any(re.search(pattern, initial_window, re.IGNORECASE | re.DOTALL) for pattern in repair_patterns):
            continue

        for usage in usages:
            before_window = tail[max(0, usage.start() - 240):usage.start()]
            line_start = tail.rfind("\n", 0, usage.start()) + 1
            line_end = tail.find("\n", usage.end())
            if line_end < 0:
                line_end = len(tail)
            same_line = tail[line_start:line_end]
            guard_patterns = [
                rf"\b{re.escape(var_name)}\s*\?\.\s*{risky_member_pattern}\b",
                rf"\b{re.escape(var_name)}\s*&&\s*{re.escape(var_name)}\s*\.\s*{risky_member_pattern}\b",
                rf"if\s*\([\s\S]{{0,240}}\b{re.escape(var_name)}\b[\s\S]{{0,240}}\)",
                rf"if\s*\(\s*!{re.escape(var_name)}\s*\)\s*\{{?\s*return\b",
            ]
            if any(re.search(pattern, same_line, re.IGNORECASE) for pattern in guard_patterns[:2]):
                continue
            if any(re.search(pattern, before_window, re.IGNORECASE | re.DOTALL) for pattern in guard_patterns[2:]):
                continue
            if any(re.search(pattern, before_window, re.IGNORECASE | re.DOTALL) for pattern in repair_patterns):
                continue
            findings.append(
                {
                    "variable": var_name,
                    "selector": selector,
                    "member": str(usage.group(1) or "").strip(),
                }
            )
            break
    return findings


def inspect_shared_local_script_safety(html: str, source_file: Optional[Path] = None) -> Dict[str, Any]:
    script_ctx = collect_script_context(html, source_file)
    errors: List[str] = []
    warnings: List[str] = []
    seen_error_keys = set()

    for raw_path in script_ctx.get("resolved_local_scripts", []) or []:
        script_path = Path(raw_path)
        try:
            js_text = script_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            warnings.append(f"Linked local script could not be read during validation: {script_path.name}")
            continue
        findings = _ungarded_singleton_selector_uses(js_text)
        if not findings:
            continue
        linked_pages = _linked_html_pages_for_script(script_path, source_file)
        if source_file is not None and source_file not in linked_pages:
            linked_pages.append(source_file)
        linked_pages = list(dict.fromkeys(linked_pages))
        page_html_cache: Dict[Path, str] = {}
        for finding in findings:
            selector_presence: List[Path] = []
            selector_unknown = False
            for page in linked_pages:
                try:
                    page_html = page_html_cache.setdefault(
                        page,
                        page.read_text(encoding="utf-8", errors="ignore"),
                    )
                except Exception:
                    continue
                present = _html_contains_simple_selector(page_html, finding.get("selector", ""))
                if present is None:
                    selector_unknown = True
                    break
                if not present:
                    selector_presence.append(page)
            if selector_unknown or not selector_presence:
                continue
            error_key = (script_path.name, finding.get("selector", ""))
            if error_key in seen_error_keys:
                continue
            seen_error_keys.add(error_key)
            missing_pages = ", ".join(page.name for page in selector_presence[:4])
            errors.append(
                f"Shared local script {script_path.name} dereferences selector "
                f"{finding.get('selector')} without a null guard, but linked page(s) are missing it: {missing_pages}"
            )

    if script_ctx.get("missing_local_scripts"):
        warnings.append("Linked local script could not be resolved during validation")

    return {
        "errors": errors,
        "warnings": warnings,
        "resolved_local_scripts": script_ctx.get("resolved_local_scripts", []),
        "missing_local_scripts": script_ctx.get("missing_local_scripts", []),
    }


def validate_html_content(html: str, source_file: Optional[Path] = None) -> Dict:
    lower = (html or "").lower()
    errors: List[str] = []
    warnings: List[str] = []
    score = 100
    stylesheet_ctx = collect_stylesheet_context(html, source_file)
    script_ctx = collect_script_context(html, source_file)
    script_integrity = inspect_script_tag_integrity(html)
    javascript_syntax = inspect_javascript_syntax(html, source_file)
    script_safety = inspect_shared_local_script_safety(html, source_file)
    inline_handler_contract = (
        inspect_inline_handler_contract(html, source_file)
        if not script_integrity.get("errors")
        else {"errors": [], "warnings": []}
    )
    body_structure = inspect_body_structure(html)

    if has_truncation_marker(html):
        errors.append("HTML contains a literal truncation marker, so the page is corrupted")
        score -= 30

    if is_bootstrap_html_content(html):
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
            score -= 16

    if not stylesheet_ctx.get("has_inline_style") and not stylesheet_ctx.get("has_local_stylesheet"):
        errors.append("Missing inline <style> block or local linked stylesheet")
        score -= 16
    elif stylesheet_ctx.get("missing_local_stylesheets") and not stylesheet_ctx.get("has_inline_style"):
        warnings.append("Linked local stylesheet could not be resolved during validation")
        score -= 6

    for err in script_integrity.get("errors", []) or []:
        errors.append(err)
        score -= 24
    for warn in script_integrity.get("warnings", []) or []:
        warnings.append(warn)
        score -= 6

    for err in javascript_syntax.get("errors", []) or []:
        errors.append(err)
        score -= 28
    for warn in javascript_syntax.get("warnings", []) or []:
        warnings.append(warn)
        score -= 4

    for err in script_safety.get("errors", []) or []:
        errors.append(err)
        score -= 20
    for warn in script_safety.get("warnings", []) or []:
        warnings.append(warn)
        score -= 6

    for err in inline_handler_contract.get("errors", []) or []:
        errors.append(err)
        score -= 20
    for warn in inline_handler_contract.get("warnings", []) or []:
        warnings.append(warn)
        score -= 6

    for err in body_structure.get("errors", []) or []:
        errors.append(err)
        score -= 20
    for warn in body_structure.get("warnings", []) or []:
        warnings.append(warn)
        score -= 8

    html_bytes = len((html or "").encode("utf-8"))
    if html_bytes < MIN_HTML_BYTES:
        errors.append(f"HTML output too small ({html_bytes} bytes), likely truncated or low-quality")
        score -= 24

    css_text = str(stylesheet_ctx.get("css_text") or "")
    css_rules = css_text.count("{")
    css_lower = css_text.lower()
    if css_rules < MIN_CSS_RULES:
        warnings.append(f"Too few CSS rules ({css_rules}); design may look basic")
        score -= 10

    semantic_hits = sum(1 for t in ("<header", "<main", "<section", "<footer", "<nav") if t in lower)
    if semantic_hits < MIN_SEMANTIC_BLOCKS:
        warnings.append(f"Low semantic structure ({semantic_hits} sections)")
        score -= 8

    if "@media" not in lower and "@media" not in css_lower:
        warnings.append("No media query detected (weak responsive support)")
        score -= 8

    if "<script" not in lower:
        warnings.append("No JavaScript detected (limited interactivity)")
        score -= 6

    if "<img" in lower and "alt=" not in lower:
        warnings.append("Image tag without alt text detected")
        score -= 6

    return {
        "pass": len(errors) == 0 and score >= 70,
        "score": max(score, 0),
        "errors": errors,
        "warnings": warnings,
        "bytes": html_bytes,
        "css_rules": css_rules,
        "semantic_blocks": semantic_hits,
        "javascript_syntax_checks": javascript_syntax.get("checked_scripts", 0),
        "meaningful_body_tags": body_structure.get("meaningful_tag_count", 0),
        "visible_body_text_len": body_structure.get("visible_text_len", 0),
    }


def validate_html_file(html_file: Path) -> Dict:
    result: Dict = {
        "ok": False,
        "errors": [],
        "warnings": [],
        "file": str(html_file),
        "preview_url": None,
        "checks": {},
    }
    if not html_file.exists() or not html_file.is_file():
        result["errors"] = [f"HTML file not found: {html_file}"]
        return result
    try:
        html = html_file.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        result["errors"] = [f"Failed to read HTML file: {exc}"]
        return result

    checks = validate_html_content(html, source_file=html_file)
    result["checks"] = checks
    result["errors"] = checks.get("errors", [])
    result["warnings"] = checks.get("warnings", [])
    result["ok"] = bool(checks.get("pass"))
    try:
        result["preview_url"] = build_preview_url_for_file(html_file)
    except ValueError:
        result["preview_url"] = None
        result["warnings"].append("Preview file is outside the active output directory; preview URL unavailable.")
    return result


def summarize_vertical_content_gaps(
    content_blocks: List[Dict[str, Any]],
    viewport_height: int,
    scroll_height: int,
) -> Dict[str, Any]:
    viewport = max(int(viewport_height or 0), 0)
    page_height = max(int(scroll_height or 0), 0)
    min_gap_px = max(SMOKE_BLANK_GAP_MIN_PX, int(viewport * 0.85) if viewport > 0 else 0)
    if viewport <= 0 or page_height <= int(viewport * 1.4):
        return {
            "blank_gap_count": 0,
            "largest_blank_gap": 0,
            "blank_gap_threshold": min_gap_px,
            "blank_gap_samples": [],
        }

    intervals: List[Tuple[int, int]] = []
    for item in content_blocks or []:
        if not isinstance(item, dict):
            continue
        try:
            top = max(int(float(item.get("top", 0) or 0)), 0)
            bottom = max(int(float(item.get("bottom", 0) or 0)), 0)
        except Exception:
            continue
        if bottom - top < 6:
            continue
        intervals.append((top, min(bottom, page_height)))

    if len(intervals) < 2:
        return {
            "blank_gap_count": 0,
            "largest_blank_gap": 0,
            "blank_gap_threshold": min_gap_px,
            "blank_gap_samples": [],
        }

    intervals.sort(key=lambda item: (item[0], item[1]))
    merged: List[List[int]] = []
    merge_slack = 36
    for top, bottom in intervals:
        if not merged or top > merged[-1][1] + merge_slack:
            merged.append([top, bottom])
        else:
            merged[-1][1] = max(merged[-1][1], bottom)

    samples: List[Dict[str, int]] = []
    largest_gap = 0
    for idx in range(len(merged) - 1):
        current_bottom = merged[idx][1]
        next_top = merged[idx + 1][0]
        gap = max(next_top - current_bottom, 0)
        if gap < min_gap_px:
            continue
        if current_bottom < max(120, int(viewport * 0.25)):
            continue
        if next_top > page_height - max(120, int(viewport * 0.25)):
            continue
        largest_gap = max(largest_gap, gap)
        samples.append({
            "start": int(current_bottom),
            "end": int(next_top),
            "gap": int(gap),
        })
        if len(samples) >= 4:
            break

    return {
        "blank_gap_count": len(samples),
        "largest_blank_gap": largest_gap,
        "blank_gap_threshold": min_gap_px,
        "blank_gap_samples": samples,
    }


def detect_loading_overlay_risk(render_summary: Dict[str, Any]) -> Optional[str]:
    overlays = render_summary.get("loading_overlays", []) if isinstance(render_summary, dict) else []
    if not isinstance(overlays, list):
        return None

    blocking: List[Dict[str, Any]] = []
    for item in overlays:
        if not isinstance(item, dict):
            continue
        if item.get("blocking"):
            blocking.append(item)
    if not blocking:
        return None

    def _rank(item: Dict[str, Any]) -> Tuple[float, int, int]:
        try:
            coverage = float(item.get("coverage_ratio", 0) or 0)
        except Exception:
            coverage = 0.0
        return (
            coverage,
            1 if item.get("aria_busy") else 0,
            1 if str(item.get("position") or "").lower() == "fixed" else 0,
        )

    top = sorted(blocking, key=_rank, reverse=True)[0]
    raw_label = str(top.get("label") or top.get("text") or "loading overlay").strip()
    label = re.sub(r"\s+", " ", raw_label).strip() or "loading overlay"
    if len(label) > 80:
        label = label[:77].rstrip() + "..."
    return (
        "Persistent loading overlay/spinner is still visible after boot "
        f"({label}); preview appears stuck before the playable scene becomes usable."
    )


def _is_port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


async def _playwright_runtime_status() -> Dict[str, Optional[str]]:
    # Cache runtime probe to avoid launching Chromium repeatedly in diagnostics.
    now = time.time()
    cached = _PLAYWRIGHT_STATUS_CACHE.get("value")
    cached_ts = float(_PLAYWRIGHT_STATUS_CACHE.get("ts", 0.0))
    if isinstance(cached, dict) and (now - cached_ts) < PLAYWRIGHT_STATUS_CACHE_TTL_SEC:
        return cached  # type: ignore[return-value]

    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as exc:
        result = {"available": False, "reason": f"playwright import failed: {str(exc)[:160]}"}
        _PLAYWRIGHT_STATUS_CACHE["ts"] = now
        _PLAYWRIGHT_STATUS_CACHE["value"] = result
        return result

    async def _probe_launch() -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            await browser.close()

    def _classify_launch_failure(exc: Exception) -> Dict[str, Optional[str]]:
        raw = str(exc or "").strip()
        lowered = raw.lower()
        if "bootstrap_check_in" in raw and "permission denied (1100)" in lowered:
            return {
                "available": False,
                "reason": "playwright launch blocked by macOS sandbox/permission constraints",
                "category": "sandbox_denied",
            }
        if "executable doesn't exist" in lowered or "browser has not been found" in lowered:
            return {
                "available": False,
                "reason": "playwright browser binaries are missing",
                "category": "browser_missing",
            }
        return {
            "available": False,
            "reason": f"playwright runtime unavailable: {raw[:140]}",
            "category": "launch_failed",
        }

    try:
        await asyncio.wait_for(_probe_launch(), timeout=8.0)
        result = {"available": True, "reason": None, "category": "ok"}
    except Exception as exc:
        result = _classify_launch_failure(exc)

    _PLAYWRIGHT_STATUS_CACHE["ts"] = now
    _PLAYWRIGHT_STATUS_CACHE["value"] = result
    return result


def _safe_visual_key(value: str, *, fallback: str = "default", limit: int = 80) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return (text or fallback)[:limit]


def _visual_scope_key(preview_url: str, scope_hint: str = "") -> str:
    if scope_hint:
        return _safe_visual_key(scope_hint, fallback="scope")
    rel = _normalize_preview_rel_path(preview_url) or "index.html"
    digest = sha1(rel.encode("utf-8")).hexdigest()[:10]
    return _safe_visual_key(f"preview_{digest}", fallback="preview")


def _visual_page_key(preview_url: str) -> str:
    rel = _normalize_preview_rel_path(preview_url) or "index.html"
    stem = rel.replace("/", "__")
    return _safe_visual_key(stem, fallback="index")


def _visual_current_dir(scope_key: str, page_key: str) -> Path:
    return OUTPUT_DIR / VISUAL_CAPTURE_DIRNAME / scope_key / page_key


def _visual_baseline_dir(scope_key: str, page_key: str) -> Path:
    return VISUAL_BASELINE_DIR / scope_key / page_key


def _visual_manifest_path(scope_key: str, page_key: str) -> Path:
    return _visual_baseline_dir(scope_key, page_key) / "manifest.json"


def _load_visual_manifest(scope_key: str, page_key: str) -> Dict[str, Any]:
    path = _visual_manifest_path(scope_key, page_key)
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass
    return {}


def _image_basic_metrics(image_path: Path) -> Dict[str, Any]:
    if Image is None or ImageOps is None or ImageStat is None:
        return {"width": 0, "height": 0, "mean_brightness": 0.0}
    with Image.open(image_path) as img:
        rgba = img.convert("RGBA")
        gray = ImageOps.grayscale(rgba)
        brightness = float(ImageStat.Stat(gray).mean[0])
        return {
            "width": int(rgba.width),
            "height": int(rgba.height),
            "mean_brightness": round(brightness, 2),
        }


def _visual_region_label(bbox: Optional[Tuple[int, int, int, int]], width: int, height: int) -> str:
    if not bbox or width <= 0 or height <= 0:
        return "none"
    x1, y1, x2, y2 = bbox
    box_height_ratio = max(0.0, min(1.0, (y2 - y1) / max(height, 1)))
    center_ratio = ((y1 + y2) / 2.0) / max(height, 1)
    if box_height_ratio >= 0.72:
        return "whole_page"
    if center_ratio < 0.3:
        return "hero_upper"
    if center_ratio > 0.68:
        return "lower_page"
    return "middle_page"


def compare_visual_capture(baseline_path: Path, current_path: Path, diff_path: Path) -> Dict[str, Any]:
    if Image is None or ImageChops is None or ImageOps is None or ImageStat is None:
        return {
            "ok": False,
            "reason": "Pillow unavailable",
            "baseline_path": str(baseline_path),
            "current_path": str(current_path),
            "diff_path": str(diff_path),
        }

    with Image.open(baseline_path) as baseline_img, Image.open(current_path) as current_img:
        baseline = baseline_img.convert("RGBA")
        current = current_img.convert("RGBA")
        common_width = min(baseline.width, current.width)
        common_height = min(baseline.height, current.height)
        if common_width <= 0 or common_height <= 0:
            return {
                "ok": False,
                "reason": "Invalid image dimensions",
                "baseline_path": str(baseline_path),
                "current_path": str(current_path),
                "diff_path": str(diff_path),
            }

        baseline_crop = baseline.crop((0, 0, common_width, common_height))
        current_crop = current.crop((0, 0, common_width, common_height))
        diff = ImageChops.difference(current_crop, baseline_crop)
        gray = ImageOps.grayscale(diff)
        mask = gray.point(lambda px: 255 if px > VISUAL_DIFF_PIXEL_THRESHOLD else 0)
        histogram = mask.histogram()
        changed_pixels = int(histogram[255] if len(histogram) > 255 else 0)
        total_pixels = max(common_width * common_height, 1)
        changed_ratio = changed_pixels / total_pixels
        bbox = mask.getbbox()
        diff_area_ratio = 0.0
        if bbox:
            x1, y1, x2, y2 = bbox
            diff_area_ratio = ((x2 - x1) * (y2 - y1)) / total_pixels

        mean_abs_diff = float(ImageStat.Stat(gray).mean[0]) / 255.0
        baseline_brightness = float(ImageStat.Stat(ImageOps.grayscale(baseline_crop)).mean[0])
        current_brightness = float(ImageStat.Stat(ImageOps.grayscale(current_crop)).mean[0])
        brightness_delta = current_brightness - baseline_brightness

        red_mask = ImageOps.colorize(mask, black="#000000", white="#ff3b30").convert("RGBA")
        diff_overlay = Image.blend(current_crop, red_mask, 0.35)
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_overlay.save(diff_path)

    return {
        "ok": True,
        "baseline_path": str(baseline_path),
        "current_path": str(current_path),
        "diff_path": str(diff_path),
        "width": int(common_width),
        "height": int(common_height),
        "baseline_width": int(baseline.width),
        "baseline_height": int(baseline.height),
        "current_width": int(current.width),
        "current_height": int(current.height),
        "changed_ratio": round(changed_ratio, 4),
        "diff_area_ratio": round(diff_area_ratio, 4),
        "mean_abs_diff": round(mean_abs_diff, 4),
        "brightness_delta": round(brightness_delta, 2),
        "height_change_ratio": round((current.height - baseline.height) / max(baseline.height, 1), 4),
        "width_change_ratio": round((current.width - baseline.width) / max(baseline.width, 1), 4),
        "diff_region": _visual_region_label(bbox, common_width, common_height),
    }


def summarize_visual_regression(comparisons: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not comparisons:
        return {
            "status": "skipped",
            "summary": "No previous approved visual baseline is available yet.",
            "issues": [],
            "suggestions": [],
            "captures": [],
            "baseline_exists": False,
        }

    issues: List[str] = []
    suggestions: List[str] = []
    severe = 0
    warned = 0
    structural = 0

    for comp in comparisons:
        name = str(comp.get("name") or "capture")
        changed_ratio = float(comp.get("changed_ratio", 0.0) or 0.0)
        diff_area_ratio = float(comp.get("diff_area_ratio", 0.0) or 0.0)
        height_change_ratio = float(comp.get("height_change_ratio", 0.0) or 0.0)
        diff_region = str(comp.get("diff_region") or "whole_page")

        if "desktop_full" in name and height_change_ratio <= -0.35:
            structural += 1
            issues.append(
                f"The current full-page layout is {abs(height_change_ratio):.0%} shorter than the last approved baseline; lower sections may be missing or collapsed."
            )
            suggestions.append(
                "Restore the missing lower sections and page depth before re-review; compare the full-page content stack against the last approved version."
            )

        if changed_ratio >= VISUAL_DIFF_FAIL_RATIO and diff_area_ratio >= VISUAL_DIFF_FAIL_AREA_RATIO:
            severe += 1
            if "desktop_fold" in name:
                issues.append(
                    f"The desktop first viewport diverged sharply from the last approved baseline (changed {changed_ratio:.0%}, affected area {diff_area_ratio:.0%}) in the {diff_region}."
                )
                suggestions.append(
                    "Compare hero hierarchy, spacing, CTA prominence, and trust cues against the last approved desktop fold before resubmitting."
                )
            elif "mobile_fold" in name:
                issues.append(
                    f"The mobile first viewport diverged sharply from the last approved baseline (changed {changed_ratio:.0%}, affected area {diff_area_ratio:.0%})."
                )
                suggestions.append(
                    "Re-check mobile stacking, text wrapping, tap targets, and overflow clipping against the last approved mobile layout."
                )
            else:
                issues.append(
                    f"The full-page screenshot diverged sharply from the last approved baseline (changed {changed_ratio:.0%}, affected area {diff_area_ratio:.0%})."
                )
                suggestions.append(
                    "Compare the vertical rhythm and section sequence against the last approved full-page baseline; large unexpected movement usually means sections were removed or collapsed."
                )
        elif changed_ratio >= VISUAL_DIFF_WARN_RATIO and diff_area_ratio >= VISUAL_DIFF_WARN_AREA_RATIO:
            warned += 1
            if "desktop_fold" in name:
                issues.append(
                    f"The desktop hero/first viewport changed noticeably versus the last approved baseline (changed {changed_ratio:.0%}, affected area {diff_area_ratio:.0%})."
                )
                suggestions.append(
                    "Verify hero typography scale, CTA styling, and above-the-fold spacing did not regress while implementing the latest changes."
                )
            elif "mobile_fold" in name:
                issues.append(
                    f"The mobile first viewport changed noticeably versus the last approved baseline (changed {changed_ratio:.0%}, affected area {diff_area_ratio:.0%})."
                )
                suggestions.append(
                    "Check that mobile spacing, line breaks, and touch affordances still match the intended responsive design."
                )
            else:
                issues.append(
                    f"The overall page composition changed noticeably versus the last approved baseline (changed {changed_ratio:.0%}, affected area {diff_area_ratio:.0%})."
                )
                suggestions.append(
                    "Review section ordering, height, and spacing against the last approved layout to confirm the change was intentional."
                )

    # De-duplicate while preserving order.
    dedup_issues = list(dict.fromkeys(issues))[:6]
    dedup_suggestions = list(dict.fromkeys(suggestions))[:6]

    if structural > 0 or severe >= 2:
        status = "fail"
        summary = (
            f"Visual regression gate failed: {severe + structural} capture(s) diverged sharply from the last approved baseline."
        )
    elif severe > 0 or warned > 0:
        status = "warn"
        summary = (
            f"Visual regression warning: {severe + warned} capture(s) changed noticeably versus the last approved baseline."
        )
    else:
        status = "pass"
        summary = "Visual regression check passed: current screenshots remain close to the last approved baseline."

    return {
        "status": status,
        "summary": summary,
        "issues": dedup_issues,
        "suggestions": dedup_suggestions,
        "captures": comparisons,
        "baseline_exists": True,
    }


async def _capture_visual_bundle(preview_url: str, capture_dir: Path, timeout_ms: int = 15000) -> Dict[str, Any]:
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as exc:
        return {
            "status": "skipped",
            "reason": f"playwright import failed: {exc}",
            "captures": [],
        }

    capture_dir.mkdir(parents=True, exist_ok=True)
    captures: List[Dict[str, Any]] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                for spec in _VISUAL_CAPTURE_SPECS:
                    page = await browser.new_page(viewport={"width": int(spec["width"]), "height": int(spec["height"])})
                    try:
                        await page.goto(preview_url, wait_until="domcontentloaded", timeout=timeout_ms)
                        await page.wait_for_timeout(350)
                        image_path = capture_dir / f"{spec['name']}.png"
                        await page.screenshot(
                            path=str(image_path),
                            full_page=bool(spec.get("full_page")),
                            animations="disabled",
                        )
                        metrics = _image_basic_metrics(image_path)
                        captures.append({
                            "name": str(spec["name"]),
                            "path": str(image_path),
                            "width": metrics.get("width", 0),
                            "height": metrics.get("height", 0),
                            "mean_brightness": metrics.get("mean_brightness", 0.0),
                        })
                    finally:
                        await page.close()
            finally:
                await browser.close()
        return {"status": "ok", "captures": captures}
    except Exception as exc:
        return {
            "status": "skipped",
            "reason": f"visual capture unavailable: {str(exc)[:180]}",
            "captures": [],
        }


async def run_visual_regression(preview_url: str, scope_hint: str = "") -> Dict[str, Any]:
    runtime = await _playwright_runtime_status()
    if not runtime.get("available"):
        return {
            "status": "skipped",
            "summary": str(runtime.get("reason") or "playwright unavailable"),
            "issues": [],
            "suggestions": [],
            "captures": [],
            "baseline_exists": False,
        }
    if Image is None or ImageChops is None or ImageOps is None or ImageStat is None:
        return {
            "status": "skipped",
            "summary": "Pillow unavailable for visual diffing.",
            "issues": [],
            "suggestions": [],
            "captures": [],
            "baseline_exists": False,
        }

    scope_key = _visual_scope_key(preview_url, scope_hint)
    page_key = _visual_page_key(preview_url)
    baseline_dir = _visual_baseline_dir(scope_key, page_key)
    manifest = _load_visual_manifest(scope_key, page_key)
    baseline_exists = baseline_dir.exists() and any((baseline_dir / f"{spec['name']}.png").exists() for spec in _VISUAL_CAPTURE_SPECS)
    if not baseline_exists:
        return {
            "status": "skipped",
            "summary": "No previous approved visual baseline is available yet.",
            "issues": [],
            "suggestions": [],
            "captures": [],
            "baseline_exists": False,
            "scope_key": scope_key,
            "page_key": page_key,
            "manifest": manifest,
        }

    current_dir = _visual_current_dir(scope_key, page_key)
    capture_result = await _capture_visual_bundle(preview_url, current_dir)
    if capture_result.get("status") != "ok":
        return {
            "status": "skipped",
            "summary": str(capture_result.get("reason") or "visual capture unavailable"),
            "issues": [],
            "suggestions": [],
            "captures": [],
            "baseline_exists": True,
            "scope_key": scope_key,
            "page_key": page_key,
            "manifest": manifest,
        }

    comparisons: List[Dict[str, Any]] = []
    for capture in capture_result.get("captures", []) or []:
        name = str(capture.get("name") or "")
        current_path = Path(str(capture.get("path") or ""))
        baseline_path = baseline_dir / f"{name}.png"
        if not baseline_path.exists() or not current_path.exists():
            continue
        diff_path = current_dir / f"{name}__diff.png"
        comparison = compare_visual_capture(baseline_path, current_path, diff_path)
        comparison["name"] = name
        comparisons.append(comparison)

    summary = summarize_visual_regression(comparisons)
    summary["scope_key"] = scope_key
    summary["page_key"] = page_key
    summary["manifest"] = manifest
    return summary


async def update_visual_baseline(preview_url: str, scope_hint: str = "", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    runtime = await _playwright_runtime_status()
    if not runtime.get("available"):
        return {"updated": False, "status": "skipped", "reason": runtime.get("reason")}

    scope_key = _visual_scope_key(preview_url, scope_hint)
    page_key = _visual_page_key(preview_url)
    current_dir = _visual_current_dir(scope_key, page_key)
    capture_result = await _capture_visual_bundle(preview_url, current_dir)
    if capture_result.get("status") != "ok":
        return {
            "updated": False,
            "status": "skipped",
            "reason": capture_result.get("reason"),
            "scope_key": scope_key,
            "page_key": page_key,
        }

    baseline_dir = _visual_baseline_dir(scope_key, page_key)
    baseline_dir.mkdir(parents=True, exist_ok=True)
    manifest_captures: List[Dict[str, Any]] = []
    for capture in capture_result.get("captures", []) or []:
        current_path = Path(str(capture.get("path") or ""))
        if not current_path.exists():
            continue
        target_path = baseline_dir / f"{capture.get('name')}.png"
        target_path.write_bytes(current_path.read_bytes())
        manifest_captures.append({
            "name": str(capture.get("name") or ""),
            "file": target_path.name,
            "width": int(capture.get("width", 0) or 0),
            "height": int(capture.get("height", 0) or 0),
            "mean_brightness": float(capture.get("mean_brightness", 0.0) or 0.0),
        })

    manifest = {
        "scope_key": scope_key,
        "page_key": page_key,
        "preview_rel_path": _normalize_preview_rel_path(preview_url) or "index.html",
        "updated_at": time.time(),
        "captures": manifest_captures,
        "metadata": metadata or {},
    }
    _visual_manifest_path(scope_key, page_key).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "updated": True,
        "status": "updated",
        "scope_key": scope_key,
        "page_key": page_key,
        "captures": manifest_captures,
        "baseline_dir": str(baseline_dir),
        "manifest": manifest,
    }


async def run_playwright_smoke(preview_url: str, timeout_ms: int = 12000) -> Dict:
    """
    Optional deep smoke test. Safe fallback to skipped when playwright runtime is unavailable.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as exc:
        return {
            "status": "skipped",
            "engine": "playwright",
            "reason": f"playwright import failed: {exc}",
        }

    page_errors: List[str] = []
    console_errors: List[str] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1366, "height": 860})
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on(
                "console",
                lambda m: console_errors.append(m.text)
                if m.type in ("error", "warning")
                else None,
            )
            response = await page.goto(preview_url, wait_until="domcontentloaded", timeout=timeout_ms)
            stability_wait_ms = min(1400, max(700, int(timeout_ms * 0.1)))
            await page.wait_for_timeout(stability_wait_ms)
            title = await page.title()
            has_head = await page.evaluate("Boolean(document.head)")
            has_body = await page.evaluate("Boolean(document.body)")
            body_text_len = await page.evaluate("document.body ? document.body.innerText.length : 0")
            render_summary = await page.evaluate(
                """
() => {
  const isVisible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden'
      && style.display !== 'none'
      && Number(style.opacity || '1') > 0.02
      && rect.width > 1
      && rect.height > 1;
  };
  const parseColor = (value) => {
    const raw = String(value || '').trim().toLowerCase();
    if (!raw) return null;
    const m = raw.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
    if (!m) return null;
    return {
      r: Number(m[1]),
      g: Number(m[2]),
      b: Number(m[3]),
      a: m[4] == null ? 1 : Number(m[4]),
    };
  };
  const luminance = (rgb) => {
    const convert = (channel) => {
      const c = channel / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * convert(rgb.r) + 0.7152 * convert(rgb.g) + 0.0722 * convert(rgb.b);
  };
  const contrast = (fg, bg) => {
    if (!fg || !bg) return 1;
    const l1 = luminance(fg);
    const l2 = luminance(bg);
    const bright = Math.max(l1, l2);
    const dark = Math.min(l1, l2);
    return Number(((bright + 0.05) / (dark + 0.05)).toFixed(2));
  };
  const backgroundFor = (el) => {
    let node = el;
    while (node) {
      const bg = parseColor(window.getComputedStyle(node).backgroundColor);
      if (bg && bg.a > 0.03) return bg;
      node = node.parentElement;
    }
    const bodyBg = parseColor(window.getComputedStyle(document.body || document.documentElement).backgroundColor);
    if (bodyBg && bodyBg.a > 0.03) return bodyBg;
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const textCandidates = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,a,button,label,span,div'))
    .filter((el) => isVisible(el) && String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().length >= 3)
    .slice(0, 140)
    .map((el) => {
      const style = window.getComputedStyle(el);
      const fg = parseColor(style.color);
      const bg = backgroundFor(el);
      return {
        text: String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
        contrast: contrast(fg, bg),
      };
    });
  const readableTextCount = textCandidates.filter((item) => item.contrast >= 2.4).length;
  const veryLowContrastTextCount = textCandidates.filter((item) => item.contrast < 1.35).length;
  const scrollY = Number(window.scrollY || window.pageYOffset || 0);
  const viewportArea = Math.max(1, (window.innerWidth || 0) * (window.innerHeight || 0));
  const contentBlocks = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,img,picture,svg,video,canvas,button,a,input,textarea,select,[role="button"],[role="link"],[role="tab"],[data-evermind-content]'))
    .filter((el) => isVisible(el))
    .map((el) => {
      const rect = el.getBoundingClientRect();
      return {
        top: Math.max(0, Math.round(rect.top + scrollY)),
        bottom: Math.max(0, Math.round(rect.bottom + scrollY)),
      };
    })
    .filter((item) => item.bottom - item.top >= 6)
    .sort((a, b) => a.top - b.top)
    .slice(0, 240);
  const loadingOverlays = Array.from(document.querySelectorAll('[id],[class],[aria-busy],[role],[data-state]'))
    .filter((el) => isVisible(el))
    .map((el) => {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      const className = typeof el.className === 'string'
        ? el.className
        : (el.className && typeof el.className.baseVal === 'string' ? el.className.baseVal : '');
      const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
      const label = [
        String(el.id || '').trim(),
        String(className || '').trim(),
        String(el.getAttribute('aria-label') || '').trim(),
        String(el.getAttribute('role') || '').trim(),
        text,
      ].filter(Boolean).join(' ').trim().slice(0, 140);
      const normalized = label.toLowerCase();
      const coverageRatio = Number((((rect.width * rect.height) / viewportArea) || 0).toFixed(3));
      const role = String(el.getAttribute('role') || '').trim().toLowerCase();
      const ariaBusy = String(el.getAttribute('aria-busy') || '').trim().toLowerCase() === 'true';
      const clickPrompt = /\\b(click|tap|press|start|play|continue|resume)\\b/.test(normalized);
      const loadingHint = /\\b(loading|loader|spinner|boot|splash|initiali[sz]ing|preparing|generating|please wait)\\b/.test(normalized)
        || ariaBusy
        || role === 'progressbar';
      const position = String(style.position || '').trim().toLowerCase();
      const fixedLike = position === 'fixed' || position === 'sticky';
      const zIndex = Number(style.zIndex || '0') || 0;
      const blocking = loadingHint && !clickPrompt && (
        ariaBusy
        || role === 'progressbar'
        || coverageRatio >= 0.18
        || (fixedLike && coverageRatio >= 0.06)
        || (zIndex >= 20 && coverageRatio >= 0.08)
      );
      return {
        label,
        text: text.slice(0, 100),
        coverage_ratio: coverageRatio,
        position,
        aria_busy: ariaBusy,
        blocking,
      };
    })
    .filter((item) => item.label)
    .filter((item) => item.blocking || item.coverage_ratio >= 0.04)
    .sort((a, b) => {
      if (Number(b.blocking) !== Number(a.blocking)) return Number(b.blocking) - Number(a.blocking);
      return b.coverage_ratio - a.coverage_ratio;
    })
    .slice(0, 6);
  return {
    body_child_count: document.body ? document.body.children.length : 0,
    heading_count: document.querySelectorAll('h1,h2,h3').length,
    interactive_count: document.querySelectorAll('button,a,input,textarea,select,summary,[role="button"],[role="link"],[role="tab"]').length,
    image_count: document.querySelectorAll('img,picture,svg').length,
    canvas_count: document.querySelectorAll('canvas').length,
    landmark_count: document.querySelectorAll('main,section,header,footer,nav,article').length,
    viewport_height: window.innerHeight || 0,
    scroll_height: Math.max(
      document.body ? document.body.scrollHeight : 0,
      document.documentElement ? document.documentElement.scrollHeight : 0
    ),
    text_candidate_count: textCandidates.length,
    readable_text_count: readableTextCount,
    very_low_contrast_text_count: veryLowContrastTextCount,
    sample_text: textCandidates.slice(0, 6).map((item) => item.text),
    content_blocks: contentBlocks,
    loading_overlays: loadingOverlays,
  };
}
                """
            )
            status = response.status if response else None
            await browser.close()

        render_errors: List[str] = []
        if not isinstance(render_summary, dict):
            render_summary = {}

        readable_text_count = int(render_summary.get("readable_text_count", 0) or 0)
        text_candidate_count = int(render_summary.get("text_candidate_count", 0) or 0)
        heading_count = int(render_summary.get("heading_count", 0) or 0)
        interactive_count = int(render_summary.get("interactive_count", 0) or 0)
        image_count = int(render_summary.get("image_count", 0) or 0)
        canvas_count = int(render_summary.get("canvas_count", 0) or 0)
        landmark_count = int(render_summary.get("landmark_count", 0) or 0)
        scroll_height = int(render_summary.get("scroll_height", 0) or 0)
        viewport_height = int(render_summary.get("viewport_height", 0) or 0)
        gap_summary = summarize_vertical_content_gaps(
            render_summary.get("content_blocks", []) if isinstance(render_summary.get("content_blocks"), list) else [],
            viewport_height,
            scroll_height,
        )
        loading_overlay_error = detect_loading_overlay_risk(render_summary)
        loading_overlays = render_summary.get("loading_overlays", []) if isinstance(render_summary.get("loading_overlays"), list) else []
        if loading_overlays:
            render_summary["loading_overlay_count"] = len(loading_overlays)
            render_summary["blocking_loading_overlay_count"] = sum(
                1 for item in loading_overlays
                if isinstance(item, dict) and item.get("blocking")
            )
            render_summary["loading_overlay_labels"] = [
                str(item.get("label") or "").strip()
                for item in loading_overlays[:4]
                if isinstance(item, dict) and str(item.get("label") or "").strip()
            ]
        render_summary.pop("content_blocks", None)
        render_summary.pop("loading_overlays", None)
        render_summary.update(gap_summary)
        blank_gap_count = int(gap_summary.get("blank_gap_count", 0) or 0)
        largest_blank_gap = int(gap_summary.get("largest_blank_gap", 0) or 0)

        if body_text_len <= 20 and interactive_count == 0 and image_count == 0 and canvas_count == 0:
            render_errors.append("Preview appears blank or near-empty: almost no visible content rendered")
        if text_candidate_count > 0 and readable_text_count == 0 and canvas_count == 0:
            render_errors.append("No readable visible text detected: page may be white-on-white or fully hidden")
        if scroll_height <= max(240, viewport_height // 2) and heading_count == 0 and interactive_count == 0 and image_count == 0 and canvas_count == 0:
            render_errors.append("Rendered page is too thin and lacks visible structure")
        if landmark_count == 0 and body_text_len < 60 and image_count == 0 and canvas_count == 0:
            render_errors.append("Rendered page lacks meaningful sections or visible content blocks")
        if blank_gap_count > 0 and largest_blank_gap >= max(SMOKE_BLANK_GAP_MIN_PX, int(viewport_height * 0.85) if viewport_height > 0 else 0):
            render_errors.append(
                f"Large blank vertical gap detected: content disappears for about {largest_blank_gap}px between upper and lower sections"
            )
        if loading_overlay_error:
            render_errors.append(loading_overlay_error)
        if page_errors:
            render_errors.append("Browser runtime errors detected during preview render")

        ok = bool(
            status
            and 200 <= status < 400
            and has_head
            and has_body
            and body_text_len > 20
            and not render_errors
        )
        return {
            "status": "pass" if ok else "fail",
            "engine": "playwright",
            "http_status": status,
            "title": title,
            "has_head": bool(has_head),
            "has_body": bool(has_body),
            "body_text_len": int(body_text_len),
            "render_errors": render_errors[:6],
            "render_summary": render_summary,
            "page_errors": page_errors[:6],
            "console_errors": console_errors[:8],
        }
    except Exception as exc:
        return {
            "status": "skipped",
            "engine": "playwright",
            "reason": f"playwright runtime unavailable: {exc}",
        }


async def validate_preview(preview_url: str, run_smoke: bool = False, visual_scope: str = "") -> Dict:
    """
    Validate a preview URL end-to-end:
    - Path resolution + traversal safety
    - Artifact existence
    - HTML structure/quality gate
    - Optional Playwright smoke check
    """
    html_file = resolve_preview_file(preview_url, OUTPUT_DIR)
    if html_file is None:
        return {
            "ok": False,
            "preview_url": preview_url,
            "errors": ["Invalid preview URL or path"],
            "warnings": [],
            "checks": {},
            "smoke": {"status": "skipped", "reason": "invalid_preview_url"},
        }

    result = validate_html_file(html_file)
    # report runtime hints
    port_open = _is_port_open("127.0.0.1", DEFAULT_PORT)
    result["runtime"] = {"backend_port": DEFAULT_PORT, "port_open": port_open}

    smoke = {"status": "skipped", "reason": "not_requested"}
    if run_smoke:
        smoke = await run_playwright_smoke(result.get("preview_url") or preview_url)

    # Strict on real render failures, but infrastructure unavailability is a warning.
    if run_smoke and smoke.get("status") != "pass":
        reason = smoke.get("reason") or "unknown"
        if smoke.get("status") == "fail":
            result["ok"] = False
            result.setdefault("errors", []).append("Browser smoke test failed")
        else:
            result.setdefault("warnings", []).append(f"Browser smoke test unavailable: {reason}")

    visual_regression = await run_visual_regression(result.get("preview_url") or preview_url, visual_scope)
    result["smoke"] = smoke
    result["visual_regression"] = visual_regression
    vr_status = str(visual_regression.get("status", "") or "").strip().lower()
    vr_summary = str(visual_regression.get("summary", "") or "").strip()
    if vr_status == "fail":
        result["ok"] = False
        if vr_summary:
            result.setdefault("errors", []).append(vr_summary)
        for issue in visual_regression.get("issues", []) or []:
            if len(result["errors"]) >= 8:
                break
            result.setdefault("errors", []).append(str(issue)[:260])
    elif vr_status == "warn":
        if vr_summary:
            result.setdefault("warnings", []).append(vr_summary)
        for issue in visual_regression.get("issues", []) or []:
            if len(result["warnings"]) >= 8:
                break
            result.setdefault("warnings", []).append(str(issue)[:260])
    return result


def latest_preview_artifact(output_dir: Optional[Path] = None) -> Tuple[Optional[str], Optional[Path]]:
    """
    Find the canonical previewable HTML artifact.

    Root-level deliverables are treated as the live preview source when present.
    Task-local `task_x/...` HTML files are builder-local artifacts and should not
    outrank the promoted root preview just because they were written later.

    Fallback selection is bucket-based only when no eligible root preview exists.
    Returns (task_id, html_file), where task_id can be "root".
    """
    out = output_dir or OUTPUT_DIR
    if not out.exists():
        return None, None

    # root-level artifacts (builder/file_ops direct writes or promoted preview)
    root_html_files = [
        html for html in out.iterdir()
        if html.is_file() and html.suffix.lower() in (".html", ".htm")
    ]
    root_html = _preferred_preview_candidate(root_html_files, bucket_root=out)
    if root_html is not None and validate_html_file(root_html).get("ok"):
        return "root", root_html

    candidates: List[Tuple[float, str, Path]] = []

    # task_xxx artifacts
    for task_dir in out.iterdir():
        if not task_dir.is_dir() or not task_dir.name.startswith("task_"):
            continue
        if task_dir.name.startswith("task_partial"):
            continue
        html_files = sorted([p for p in task_dir.iterdir() if p.suffix.lower() in (".html", ".htm")])
        if not html_files:
            continue
        html = _preferred_preview_candidate(html_files, bucket_root=task_dir)
        if html is None or not validate_html_file(html).get("ok"):
            continue
        candidates.append((_latest_mtime(html_files), task_dir.name, html))

    if not candidates:
        return None, None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, task_id, html = candidates[0]
    return task_id, html


def latest_stable_preview_artifact(output_dir: Optional[Path] = None) -> Tuple[Optional[str], Optional[Path]]:
    """
    Find the newest persisted stable preview snapshot under _stable_previews/.
    This is used for rollback-safe diagnostics so failed in-progress artifacts do
    not replace the last known good preview.
    """
    out = output_dir or OUTPUT_DIR
    stable_root = out / "_stable_previews"
    if not stable_root.exists():
        return None, None

    candidates: List[Tuple[float, str, Path]] = []
    for run_dir in stable_root.iterdir():
        if not run_dir.is_dir():
            continue
        for snapshot_dir in run_dir.iterdir():
            if not snapshot_dir.is_dir():
                continue
            html_files = [
                html for html in snapshot_dir.rglob("*")
                if html.is_file() and html.suffix.lower() in (".html", ".htm")
            ]
            preview_html = _preferred_preview_candidate(html_files, bucket_root=snapshot_dir)
            if preview_html is None or not validate_html_file(preview_html).get("ok"):
                continue
            candidates.append((_latest_mtime(html_files), run_dir.name or "stable", preview_html))

    if not candidates:
        return None, None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, task_id, html = candidates[0]
    return task_id, html


async def diagnostics_snapshot() -> Dict:
    """
    Build a compact diagnostics payload for frontend operations panel.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    key_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "kimi": "KIMI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "qwen": "QWEN_API_KEY",
    }
    keys = {provider: bool(os.getenv(env_var)) for provider, env_var in key_map.items()}

    task_dirs = []
    for item in OUTPUT_DIR.iterdir():
        if item.is_dir() and item.name.startswith("task_"):
            task_dirs.append(item)
    task_dirs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

    stable_task_id, stable_html = latest_stable_preview_artifact(OUTPUT_DIR)
    latest_task_id, latest_html = latest_preview_artifact(OUTPUT_DIR)
    chosen_task_id = stable_task_id or latest_task_id
    chosen_html = stable_html or latest_html
    latest_preview_url = build_preview_url_for_file(chosen_html, OUTPUT_DIR) if chosen_html else None

    # Lightweight system hints
    try:
        load1, load5, load15 = os.getloadavg()
        load_avg = {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)}
    except Exception:
        load_avg = {"1m": None, "5m": None, "15m": None}
    playwright = await _playwright_runtime_status()
    release = build_release_doctor_report(playwright_status=playwright, current_backend_dir=Path(__file__).resolve().parent)

    return {
        "status": "ok",
        "output_dir": str(OUTPUT_DIR),
        "ports": {
            "backend_8765": _is_port_open("127.0.0.1", DEFAULT_PORT),
            "frontend_3000": _is_port_open("127.0.0.1", 3000),
        },
        "api_keys": keys,
        "tasks": {
            "count": len(task_dirs),
            "latest_task_id": chosen_task_id,
            "latest_preview_url": latest_preview_url,
        },
        "runtime": {
            "load_avg": load_avg,
            "clients_connected": None,
            "playwright_available": bool(playwright.get("available")),
            "playwright_reason": playwright.get("reason"),
        },
        "release": release,
    }
