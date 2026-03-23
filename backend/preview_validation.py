"""
Preview validation utilities for Evermind.

Design goals:
1. Fast, deterministic structural checks for generated HTML.
2. Optional deep smoke check via Playwright (if runtime/browser is available).
3. Safe path resolution from preview URLs to local output artifacts.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote


OUTPUT_DIR = Path(os.getenv("EVERMIND_OUTPUT_DIR", "/tmp/evermind_output"))
DEFAULT_PORT = int(os.getenv("PORT", "8765"))
MIN_HTML_BYTES = int(os.getenv("EVERMIND_MIN_HTML_BYTES", "1200"))
MIN_CSS_RULES = int(os.getenv("EVERMIND_MIN_CSS_RULES", "10"))
MIN_SEMANTIC_BLOCKS = int(os.getenv("EVERMIND_MIN_SEMANTIC_BLOCKS", "4"))
PLAYWRIGHT_STATUS_CACHE_TTL_SEC = int(os.getenv("EVERMIND_PLAYWRIGHT_STATUS_TTL_SEC", "30"))
_PLAYWRIGHT_STATUS_CACHE: Dict[str, object] = {"ts": 0.0, "value": {"available": False, "reason": "not_checked"}}
_PARTIAL_HTML_RE = re.compile(r"^index_part\d+\.html?$", re.IGNORECASE)


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


def validate_html_content(html: str) -> Dict:
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
            score -= 16

    html_bytes = len((html or "").encode("utf-8"))
    if html_bytes < MIN_HTML_BYTES:
        errors.append(f"HTML output too small ({html_bytes} bytes), likely truncated or low-quality")
        score -= 24

    style_blocks = re.findall(r"<style[^>]*>(.*?)</style>", html or "", re.IGNORECASE | re.DOTALL)
    css_text = "\n".join(style_blocks)
    css_rules = css_text.count("{")
    if css_rules < MIN_CSS_RULES:
        warnings.append(f"Too few CSS rules ({css_rules}); design may look basic")
        score -= 10

    semantic_hits = sum(1 for t in ("<header", "<main", "<section", "<footer", "<nav") if t in lower)
    if semantic_hits < MIN_SEMANTIC_BLOCKS:
        warnings.append(f"Low semantic structure ({semantic_hits} sections)")
        score -= 8

    if "@media" not in lower:
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

    checks = validate_html_content(html)
    result["checks"] = checks
    result["errors"] = checks.get("errors", [])
    result["warnings"] = checks.get("warnings", [])
    result["ok"] = bool(checks.get("pass"))
    result["preview_url"] = build_preview_url_for_file(html_file)
    return result


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

    try:
        await asyncio.wait_for(_probe_launch(), timeout=8.0)
        result = {"available": True, "reason": None}
    except Exception as exc:
        result = {"available": False, "reason": f"playwright runtime unavailable: {str(exc)[:140]}"}

    _PLAYWRIGHT_STATUS_CACHE["ts"] = now
    _PLAYWRIGHT_STATUS_CACHE["value"] = result
    return result


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
            await page.wait_for_timeout(350)
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

        if body_text_len <= 20 and interactive_count == 0 and image_count == 0 and canvas_count == 0:
            render_errors.append("Preview appears blank or near-empty: almost no visible content rendered")
        if text_candidate_count > 0 and readable_text_count == 0 and canvas_count == 0:
            render_errors.append("No readable visible text detected: page may be white-on-white or fully hidden")
        if scroll_height <= max(240, viewport_height // 2) and heading_count == 0 and interactive_count == 0 and image_count == 0 and canvas_count == 0:
            render_errors.append("Rendered page is too thin and lacks visible structure")
        if landmark_count == 0 and body_text_len < 60 and image_count == 0 and canvas_count == 0:
            render_errors.append("Rendered page lacks meaningful sections or visible content blocks")
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


async def validate_preview(preview_url: str, run_smoke: bool = False) -> Dict:
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

    # Strict: if smoke is requested, only PASS is acceptable.
    if run_smoke and smoke.get("status") != "pass":
        result["ok"] = False
        reason = smoke.get("reason") or "unknown"
        if smoke.get("status") == "fail":
            result.setdefault("errors", []).append("Browser smoke test failed")
        else:
            result.setdefault("errors", []).append(f"Browser smoke test unavailable: {reason}")

    result["smoke"] = smoke
    return result


def latest_preview_artifact(output_dir: Optional[Path] = None) -> Tuple[Optional[str], Optional[Path]]:
    """
    Find the most recent HTML artifact.
    Priority is newest mtime across:
    1) task_xxx/*.html artifacts
    2) output root *.html artifacts (fallback when builder writes to /tmp/evermind_output/index.html directly)
    Parallel-builder partials like index_part1.html are ignored because they are not
    directly previewable final artifacts.
    Returns (task_id, html_file), where task_id can be "root".
    """
    out = output_dir or OUTPUT_DIR
    if not out.exists():
        return None, None
    candidates: List[Tuple[float, str, Path]] = []

    # task_xxx artifacts
    for task_dir in out.iterdir():
        if not task_dir.is_dir() or not task_dir.name.startswith("task_"):
            continue
        html_files = sorted([p for p in task_dir.iterdir() if p.suffix.lower() in (".html", ".htm")])
        if not html_files:
            continue
        html = next((p for p in html_files if not is_partial_html_artifact(p)), None)
        if html is None:
            continue
        try:
            mtime = html.stat().st_mtime
        except Exception:
            mtime = 0
        candidates.append((mtime, task_dir.name, html))

    # root-level artifacts (builder/file_ops direct writes)
    for html in out.iterdir():
        if not html.is_file() or html.suffix.lower() not in (".html", ".htm"):
            continue
        if is_partial_html_artifact(html):
            continue
        try:
            mtime = html.stat().st_mtime
        except Exception:
            mtime = 0
        candidates.append((mtime, "root", html))

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

    latest_task_id, latest_html = latest_preview_artifact(OUTPUT_DIR)
    latest_preview_url = build_preview_url_for_file(latest_html, OUTPUT_DIR) if latest_html else None

    # Lightweight system hints
    try:
        load1, load5, load15 = os.getloadavg()
        load_avg = {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)}
    except Exception:
        load_avg = {"1m": None, "5m": None, "15m": None}
    playwright = await _playwright_runtime_status()

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
            "latest_task_id": latest_task_id,
            "latest_preview_url": latest_preview_url,
        },
        "runtime": {
            "load_avg": load_avg,
            "clients_connected": None,
            "playwright_available": bool(playwright.get("available")),
            "playwright_reason": playwright.get("reason"),
        },
    }
