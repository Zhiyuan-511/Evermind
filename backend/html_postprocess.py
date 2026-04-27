"""
HTML Post-Processor — Auto-fix common quality issues after builder generates HTML.
Applied after every builder write to ensure baseline quality.
"""

import re
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

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
_GAME_LOW_HEIGHT_SAFETY_CSS = (
    "\n    @media (max-height: 820px) {\n"
    "      html, body { height: auto !important; min-height: 100%; overflow: auto !important; }\n"
    "      body { overflow: auto !important; padding-block: max(12px, env(safe-area-inset-top)) max(12px, env(safe-area-inset-bottom)) !important; }\n"
    "      .app, .app-shell, .game-shell, .layout-root { min-height: 100dvh !important; height: auto !important; align-items: start !important; padding-block: 12px !important; overflow: visible !important; }\n"
    "      .frame, .layout-frame, .game-frame, .shell-frame { grid-template-columns: 1fr !important; height: auto !important; max-height: none !important; min-height: calc(100dvh - 24px) !important; align-content: start !important; }\n"
    "      .sidebar, .side-panel, .info-panel { order: 2 !important; overflow: auto !important; max-height: none !important; }\n"
    "      .stage-shell, .stage, .canvas-wrap, .game-stage, .preview-stage { min-height: 0 !important; overflow: visible !important; }\n"
    "      .overlay, .modal-overlay, .dialog-overlay, .menu-overlay { overflow: auto !important; align-items: flex-start !important; padding-block: 12px !important; }\n"
    "      .modal, dialog, .dialog, .menu-panel, .menu-modal { max-height: calc(100dvh - 40px) !important; width: min(760px, 100%) !important; overflow: auto !important; }\n"
    "      .feature-row, .result-grid, .cta-grid { grid-template-columns: 1fr !important; }\n"
    "    }\n"
)

_GAME_COMPACT_HUD_SAFETY_CSS = (
    "\n    @media (max-height: 860px) {\n"
    "      .hud { padding: 12px !important; }\n"
    "      .hud-top { gap: 10px !important; }\n"
    "      .hud-cluster { gap: 8px !important; max-width: 100% !important; }\n"
    "      .hud-card { padding: 10px 12px !important; min-width: 112px !important; }\n"
    "      .hud-value { font-size: clamp(22px, 4.2vh, 28px) !important; }\n"
    "      .hud-sub { font-size: 11px !important; line-height: 1.35 !important; }\n"
    "      .weapon-panel { min-width: 0 !important; max-width: 100% !important; padding: 12px 14px !important; }\n"
    "      .weapon-name { font-size: clamp(22px, 4vh, 28px) !important; }\n"
    "      .ammo-big { font-size: clamp(22px, 4.2vh, 28px) !important; }\n"
    "      .ammo-small, .reticle-tip { font-size: 11px !important; line-height: 1.35 !important; }\n"
    "      .weapon-meta, .ammo-row, .mission-row, .legend-row { gap: 6px !important; margin-top: 6px !important; }\n"
    "      .mini-tag { padding: 6px 9px !important; font-size: 11px !important; line-height: 1.2 !important; white-space: normal !important; }\n"
    "    }\n"
    "    @media (max-height: 860px) and (min-width: 860px) {\n"
    "      .hud-bottom { flex-direction: row !important; align-items: flex-end !important; gap: 10px !important; }\n"
    "      .hud-bottom > .weapon-panel { flex: 1 1 auto !important; }\n"
    "      .hud-bottom > .hud-card { flex: 0 0 min(280px, 30vw) !important; min-width: 0 !important; }\n"
    "    }\n"
    "    @media (max-height: 720px) {\n"
    "      .hud { padding: 10px !important; }\n"
    "      .hud-card { min-width: calc(50% - 4px) !important; }\n"
    "      .hud-top, .hud-bottom { gap: 8px !important; }\n"
    "    }\n"
)
_GAME_OVERLAY_SAFE_AREA_CSS = (
    "\n    @media (max-height: 860px) {\n"
    "      .menu-screen, .briefing-screen, .pause-screen, .game-over-screen {\n"
    "        overflow: auto !important;\n"
    "        display: flex !important;\n"
    "        align-items: flex-start !important;\n"
    "        justify-content: center !important;\n"
    "        padding: 12px !important;\n"
    "      }\n"
    "      .menu-screen > *, .briefing-screen > *, .pause-screen > *, .game-over-screen > * {\n"
    "        width: min(920px, 100%) !important;\n"
    "        max-height: calc(100dvh - 28px) !important;\n"
    "        overflow: auto !important;\n"
    "        margin: auto !important;\n"
    "      }\n"
    "      canvas, #gameCanvas { touch-action: none !important; }\n"
    "      .touch-pad, .touch-look, .touch-fire, .touch-weapon { touch-action: none !important; }\n"
    "    }\n"
)
_GAME_HIDDEN_SCREEN_STRICT_CSS = (
    "\n    /* Evermind QA: ensure hidden game overlays are not still counted as visible text */\n"
    "    .screen.hidden,\n"
    "    .menu-screen.hidden,\n"
    "    .briefing-screen.hidden,\n"
    "    .pause-screen.hidden,\n"
    "    .game-over-screen.hidden,\n"
    "    .victory-screen.hidden,\n"
    "    .result-screen.hidden,\n"
    "    #startScreen.hidden,\n"
    "    #gameOverScreen.hidden,\n"
    "    #victoryScreen.hidden,\n"
    "    #pauseScreen.hidden,\n"
    "    #missionComplete.hidden,\n"
    "    #gameOver.hidden {\n"
    "      display: none !important;\n"
    "      visibility: hidden !important;\n"
    "      opacity: 0 !important;\n"
    "      pointer-events: none !important;\n"
    "    }\n"
)
_RUNTIME_OUTPUT_DIRNAME = "_evermind_runtime"
_RUNTIME_VENDOR_ROOT = Path(__file__).parent / "runtime_vendor"
_LOCAL_THREE_CLASSIC_PATH = f"./{_RUNTIME_OUTPUT_DIRNAME}/three/three.min.js"
_LOCAL_THREE_MODULE_PATH = f"./{_RUNTIME_OUTPUT_DIRNAME}/three/three.module.js"
_LOCAL_PHASER_PATH = f"./{_RUNTIME_OUTPUT_DIRNAME}/phaser/phaser.min.js"
_LOCAL_HOWLER_PATH = f"./{_RUNTIME_OUTPUT_DIRNAME}/howler/howler.min.js"


@dataclass(frozen=True)
class _RuntimeFile:
    token: str
    vendor_source: Path


_LOCAL_RUNTIME_FILES = (
    _RuntimeFile(
        token=f"{_RUNTIME_OUTPUT_DIRNAME}/three/three.min.js",
        vendor_source=_RUNTIME_VENDOR_ROOT / "three" / "three.min.js",
    ),
    _RuntimeFile(
        token=f"{_RUNTIME_OUTPUT_DIRNAME}/three/three.module.js",
        vendor_source=_RUNTIME_VENDOR_ROOT / "three" / "three.module.js",
    ),
    _RuntimeFile(
        token=f"{_RUNTIME_OUTPUT_DIRNAME}/phaser/phaser.min.js",
        vendor_source=_RUNTIME_VENDOR_ROOT / "phaser" / "phaser.min.js",
    ),
    _RuntimeFile(
        token=f"{_RUNTIME_OUTPUT_DIRNAME}/howler/howler.min.js",
        vendor_source=_RUNTIME_VENDOR_ROOT / "howler" / "howler.min.js",
    ),
)
_THREE_MODULE_REMOTE_RE = re.compile(
    r"https?://(?:cdn\.jsdelivr\.net|unpkg\.com)/(?:npm/)?three@[^\"'>\s]+/build/three\.module(?:\.min)?\.js",
    re.IGNORECASE,
)
_THREE_CLASSIC_REMOTE_RE = re.compile(
    r"https?://(?:cdn\.jsdelivr\.net|unpkg\.com|cdnjs\.cloudflare\.com)/[^\"'>\s]*three(?:@[^\"'>\s]+)?/build/three(?:\.min)?\.js",
    re.IGNORECASE,
)
_PHASER_REMOTE_RE = re.compile(
    r"https?://(?:cdn\.jsdelivr\.net|unpkg\.com)/(?:npm/)?phaser@[^\"'>\s]+/dist/phaser(?:\.min)?\.js",
    re.IGNORECASE,
)
_HOWLER_REMOTE_RE = re.compile(
    r"https?://(?:cdn\.jsdelivr\.net|unpkg\.com)/(?:npm/)?howler@[^\"'>\s]+/dist/howler(?:\.min)?\.js",
    re.IGNORECASE,
)
_LOCAL_THREE_CLASSIC_SCRIPT_TAG_RE = re.compile(
    r"\s*<script\b[^>]*src=[\"']\./_evermind_runtime/three/three\.min\.js[\"'][^>]*>\s*</script>\s*",
    re.IGNORECASE,
)
_LOCAL_THREE_CLASSIC_SCRIPT_INSERT_RE = re.compile(
    r"(<script\b[^>]*src=[\"'](?:\./)?_evermind_runtime/three/three\.min\.js[\"'][^>]*>\s*</script>)",
    re.IGNORECASE,
)
_LOCAL_THREE_MODULE_IMPORT_RE = re.compile(
    r"import\s+\*\s+as\s+THREE\s+from\s+[\"']\./_evermind_runtime/three/three\.module\.js[\"']",
    re.IGNORECASE,
)
# v7.5: was `\b(?:new\s+)?THREE\.` — matched ANY THREE. reference, including
# defensive probes like `if (window.THREE) {...}` (the v7.4 perf shim).
# That mis-triggered Three.js auto-injection into 2D Canvas games (e.g. PvZ),
# loading 600KB+ of unused library and producing a deprecation warning.
# Now require call/construction form: `new THREE.X(` or `THREE.X.method(`.
# Reference-only guards (`window.THREE`, `if (THREE)`, `typeof THREE`) no
# longer match.
_THREE_USAGE_RE = re.compile(
    r"\bnew\s+THREE\.[A-Z]\w*\s*\(|"
    r"\bTHREE\.[A-Z]\w*\.[a-zA-Z_]\w*\s*\(",
    re.IGNORECASE,
)
_PHASER_USAGE_RE = re.compile(r"\bPhaser\.(?:Game|AUTO|WEBGL|CANVAS)\b", re.IGNORECASE)
_HOWLER_USAGE_RE = re.compile(r"\b(?:new\s+Howl\s*\(|Howler\.)", re.IGNORECASE)
_INLINE_SCRIPT_BLOCK_RE = re.compile(
    r"(<script\b(?P<attrs>[^>]*)>)(?P<body>[\s\S]*?)(</script\s*>)",
    re.IGNORECASE,
)
_POINTER_LOCK_CALL_RE = re.compile(
    r"(?P<target>\b[A-Za-z_$][\w$]*)\.requestPointerLock\((?P<args>[^)]*)\);"
)
_POINTER_LOCK_SHIM_BLOCK_RE = re.compile(
    r"\s*<script\b[^>]*data-evermind-runtime-shim=[\"']pointer-lock[\"'][^>]*>[\s\S]*?</script>\s*",
    re.IGNORECASE,
)
_POINTER_LOCK_HELPER_METHOD_CALL_RE = re.compile(
    r"(?P<object>\b[A-Za-z_$][\w$.]*)\._evermindSafeRequestPointerLock\((?P<args>[^)]*)\);"
)
_GAME_RUNTIME_PERF_SHIM_BLOCK_RE = re.compile(
    r"\s*<script\b[^>]*data-evermind-runtime-shim=[\"']game-perf[\"'][^>]*>[\s\S]*?</script>\s*",
    re.IGNORECASE,
)
_THREE_CAPSULE_GEOMETRY_SHIM = (
    "    <script data-evermind-runtime-shim=\"three-capsule\">\n"
    "      if (window.THREE && typeof THREE.CapsuleGeometry !== 'function') {\n"
    "        THREE.CapsuleGeometry = function(radius, length, capSegments, radialSegments) {\n"
    "          const safeRadius = Math.max(0.05, Number(radius) || 0.5);\n"
    "          const safeLength = Math.max(safeRadius * 2, Number(length) || (safeRadius * 2));\n"
    "          const safeRadialSegments = Math.max(6, Math.trunc(Number(radialSegments) || 12));\n"
    "          return new THREE.CylinderGeometry(safeRadius, safeRadius, safeLength + safeRadius, safeRadialSegments);\n"
    "        };\n"
    "      }\n"
    "    </script>\n"
)
_POINTER_LOCK_SHIM = (
    "    <script data-evermind-runtime-shim=\"pointer-lock\">\n"
    "      function _evermindSafeRequestPointerLock(target, options) {\n"
    "        try {\n"
    "          if (!target || typeof target.requestPointerLock !== 'function') return Promise.resolve(false);\n"
    "          const result = typeof options === 'undefined'\n"
    "            ? target.requestPointerLock()\n"
    "            : target.requestPointerLock(options);\n"
    "          if (result && typeof result.then === 'function') {\n"
    "            return result.then(() => true).catch(() => false);\n"
    "          }\n"
    "          return Promise.resolve(true);\n"
    "        } catch (_evermindPointerLockError) {\n"
    "          return Promise.resolve(false);\n"
    "        }\n"
    "      }\n"
    "    </script>\n"
)
_GAME_RUNTIME_PERF_SHIM = (
    "    <script data-evermind-runtime-shim=\"game-perf\">\n"
    "      (() => {\n"
    "        if (window.__evermindGamePerfShimInstalled) return;\n"
    "        window.__evermindGamePerfShimInstalled = true;\n"
    "        window.__EVERMIND_SAFE_RENDER = window.__EVERMIND_SAFE_RENDER || function(renderer, scene, camera) {\n"
    "          try {\n"
    "            if (!renderer || !scene || !camera || typeof renderer.render !== 'function') return false;\n"
    "            renderer.render(scene, camera);\n"
    "            return true;\n"
    "          } catch (_evermindRenderError) {\n"
    "            return false;\n"
    "          }\n"
    "        };\n"
    "        const syncVisibility = () => { window.__EVERMIND_PAGE_HIDDEN__ = !!document.hidden; };\n"
    "        syncVisibility();\n"
    "        document.addEventListener('visibilitychange', () => {\n"
    "          syncVisibility();\n"
    "          try {\n"
    "            if (window.Howler && typeof window.Howler.mute === 'function') {\n"
    "              window.Howler.mute(!!document.hidden);\n"
    "            }\n"
    "          } catch (_evermindHowlerMuteError) {}\n"
    "        }, { passive: true });\n"
    "        window.addEventListener('blur', syncVisibility, { passive: true });\n"
    "        window.addEventListener('focus', syncVisibility, { passive: true });\n"
    "        const patchThree = () => {\n"
    "          if (!window.THREE || window.THREE.__evermindPerfShim) return;\n"
    "          window.THREE.__evermindPerfShim = true;\n"
    "          const rendererProto = window.THREE.WebGLRenderer && window.THREE.WebGLRenderer.prototype;\n"
    "          const originalSetPixelRatio = rendererProto && rendererProto.setPixelRatio;\n"
    "          if (originalSetPixelRatio) {\n"
    "            rendererProto.setPixelRatio = function(value) {\n"
    "              const requested = Number.isFinite(Number(value)) ? Number(value) : (window.devicePixelRatio || 1);\n"
    "              return originalSetPixelRatio.call(this, Math.min(requested || 1, 1.5));\n"
    "            };\n"
    "          }\n"
    "          const clockProto = window.THREE.Clock && window.THREE.Clock.prototype;\n"
    "          const originalGetDelta = clockProto && clockProto.getDelta;\n"
    "          if (originalGetDelta) {\n"
    "            clockProto.getDelta = function() {\n"
    "              const delta = Number(originalGetDelta.call(this)) || 0;\n"
    "              return Math.min(delta, 0.05);\n"
    "            };\n"
    "          }\n"
    "        };\n"
    "        patchThree();\n"
    "        let attempts = 0;\n"
    "        const timer = window.setInterval(() => {\n"
    "          attempts += 1;\n"
    "          patchThree();\n"
    "          if (window.THREE || attempts >= 20) window.clearInterval(timer);\n"
    "        }, 250);\n"
    "      })();\n"
    "    </script>\n"
)
_THREE_RENDER_CALL_RE = re.compile(
    r"(?<!__EVERMIND_SAFE_RENDER\()"
    r"(?P<renderer>(?:\b(?:this|[A-Za-z_$][\w$]*)(?:\.[A-Za-z_$][\w$]*)*\.renderer\b|\brenderer\b))"
    r"\.render\(\s*"
    r"(?P<scene>(?:\b(?:this|[A-Za-z_$][\w$]*)(?:\.[A-Za-z_$][\w$]*)*\.scene\b|\bscene\b))\s*,\s*"
    r"(?P<camera>(?:\b(?:this|[A-Za-z_$][\w$]*)(?:\.[A-Za-z_$][\w$]*)*\.(?:camera|threeCamera)\b|\bcamera\b|\bthreeCamera\b))\s*"
    r"\);",
    re.IGNORECASE,
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


def _inject_runtime_script_tag(html: str, src: str) -> str:
    snippet = f'    <script src="{src}"></script>\n'
    if src in html:
        return html
    lower = html.lower()
    first_script = lower.find("<script")
    if first_script >= 0:
        return html[:first_script] + snippet + html[first_script:]
    head_close = lower.find("</head>")
    if head_close >= 0:
        return html[:head_close] + snippet + html[head_close:]
    body_close = lower.find("</body>")
    if body_close >= 0:
        return html[:body_close] + snippet + html[body_close:]
    return html.rstrip() + "\n" + snippet


def _inject_before_first_script_or_close(html: str, snippet: str) -> str:
    if not html or not snippet:
        return html
    lower = html.lower()
    first_script = lower.find("<script")
    if first_script >= 0:
        return html[:first_script] + snippet + html[first_script:]
    head_close = lower.find("</head>")
    if head_close >= 0:
        return html[:head_close] + snippet + html[head_close:]
    body_close = lower.find("</body>")
    if body_close >= 0:
        return html[:body_close] + snippet + html[body_close:]
    return html.rstrip() + "\n" + snippet


def _localize_runtime_dependencies(html: str) -> str:
    if not html:
        return html

    localized = html
    localized = _THREE_MODULE_REMOTE_RE.sub(_LOCAL_THREE_MODULE_PATH, localized)
    localized = _THREE_CLASSIC_REMOTE_RE.sub(_LOCAL_THREE_CLASSIC_PATH, localized)
    localized = _PHASER_REMOTE_RE.sub(_LOCAL_PHASER_PATH, localized)
    localized = _HOWLER_REMOTE_RE.sub(_LOCAL_HOWLER_PATH, localized)

    has_three_runtime = (
        _LOCAL_THREE_CLASSIC_PATH in localized
        or _LOCAL_THREE_MODULE_PATH in localized
    )
    if _THREE_USAGE_RE.search(localized) and not has_three_runtime:
        localized = _inject_runtime_script_tag(localized, _LOCAL_THREE_CLASSIC_PATH)
    elif _LOCAL_THREE_MODULE_IMPORT_RE.search(localized):
        localized = _LOCAL_THREE_CLASSIC_SCRIPT_TAG_RE.sub("\n", localized)
        localized = re.sub(r"\n{3,}", "\n\n", localized)

    if _PHASER_USAGE_RE.search(localized) and _LOCAL_PHASER_PATH not in localized:
        localized = _inject_runtime_script_tag(localized, _LOCAL_PHASER_PATH)

    if _HOWLER_USAGE_RE.search(localized) and _LOCAL_HOWLER_PATH not in localized:
        localized = _inject_runtime_script_tag(localized, _LOCAL_HOWLER_PATH)

    return localized


def materialize_local_runtime_assets(path: str | Path, task_type: str = "website") -> list[Path]:
    candidate = Path(path)
    if candidate.suffix.lower() not in {".html", ".htm"}:
        return []
    if not candidate.exists():
        return []

    try:
        html = candidate.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        logger.warning("Failed to inspect runtime assets for %s: %s", candidate, exc)
        return []

    materialized: list[Path] = []
    for spec in _LOCAL_RUNTIME_FILES:
        if spec.token not in html:
            continue
        if not spec.vendor_source.exists():
            logger.warning("Missing bundled runtime asset: %s", spec.vendor_source)
            continue
        destination = candidate.parent / spec.token
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(spec.vendor_source, destination)
            materialized.append(destination)
        except Exception as exc:
            logger.warning("Failed to materialize runtime asset %s for %s: %s", spec.vendor_source, candidate, exc)
    return materialized


def _trim_to_first_html_document(text: str) -> str:
    html = str(text or "").strip()
    if not html:
        return html
    lower = html.lower()
    starts = [idx for idx in (lower.find("<!doctype"), lower.find("<html")) if idx >= 0]
    if not starts:
        return html
    start = min(starts)
    if start <= 0:
        return html
    return html[start:].lstrip()


def _repair_script_tag_balance(html: str) -> str:
    repaired = str(html or "")
    if not repaired:
        return repaired

    token_re = re.compile(r"<script\b[^>]*>|</script\s*>", re.IGNORECASE)
    if not token_re.search(repaired):
        return repaired

    fragments: list[str] = []
    last_index = 0
    depth = 0
    removed_stray_closers = 0

    for match in token_re.finditer(repaired):
        token = str(match.group(0) or "").lower()
        is_closer = token.startswith("</script")
        if is_closer and depth <= 0:
            fragments.append(repaired[last_index:match.start()])
            last_index = match.end()
            removed_stray_closers += 1
            continue

        fragments.append(repaired[last_index:match.end()])
        last_index = match.end()
        if is_closer:
            depth -= 1
        else:
            depth += 1

    fragments.append(repaired[last_index:])
    repaired = "".join(fragments)

    if depth > 0:
        closing = "".join("\n</script>" for _ in range(depth))
        insert_match = re.search(r"</body\s*>|</html\s*>", repaired, re.IGNORECASE)
        if insert_match:
            repaired = repaired[:insert_match.start()].rstrip() + closing + "\n" + repaired[insert_match.start():].lstrip()
        else:
            repaired = repaired.rstrip() + closing

    if removed_stray_closers:
        logger.info("Post-processed HTML: removed %s stray </script> tag(s)", removed_stray_closers)
    return repaired


def repair_html_structure(html: str) -> str:
    """Repair common truncated or misordered HTML structure defects."""
    repaired = _trim_to_first_html_document(html)
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

    repaired = _repair_script_tag_balance(repaired)
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
            # P0 FIX 2026-04-04: Compressed / minified JS may pack multiple
            # statements on a single line.  A bare `if (id) stmt1;stmt2;` would
            # leave stmt2 unconditionally executed (or break syntax).
            # → Wrap in braces `if (id) { ... }` so the guard covers the
            #   entire line regardless of how many statements it contains.
            guarded_lines.append(f"{indent}if ({identifier}) {{ {stripped} }}")
            changed = True
        else:
            guarded_lines.append(line)
    if not changed:
        return js
    return "\n".join(guarded_lines) + ("\n" if js.endswith("\n") else "")


def _browser_global_export_lines(export_expr: str) -> list[str]:
    expr = str(export_expr or "").strip()
    if not expr:
        return []
    if re.fullmatch(r"[A-Za-z_$][\w$]*", expr):
        return [f"window.{expr} = {expr};"]

    object_match = re.fullmatch(r"\{\s*([\s\S]*?)\s*\}", expr)
    if not object_match:
        return []

    lines: list[str] = []
    for chunk in str(object_match.group(1) or "").split(","):
        part = str(chunk or "").strip()
        if not part:
            continue
        if ":" in part:
            alias, ref = [piece.strip().strip("'\"") for piece in part.split(":", 1)]
        else:
            alias = ref = part
        if alias == "default":
            alias = ref
        if not re.fullmatch(r"[A-Za-z_$][\w$]*", alias or ""):
            continue
        if not re.fullmatch(r"[A-Za-z_$][\w$]*", ref or ""):
            continue
        lines.append(f"window.{alias} = {ref};")
    return lines


def _rewrite_commonjs_export_footer_for_browser(js: str) -> str:
    if "module.exports" not in js and "exports." not in js:
        return js

    footer_patterns = [
        re.compile(
            r"\n?\s*if\s*\(\s*typeof\s+module\s*!==?\s*['\"]undefined['\"]\s*&&\s*module\.exports\s*\)\s*\{\s*module\.exports\s*=\s*(?P<expr>[\s\S]*?)\s*;\s*\}\s*$",
            re.IGNORECASE,
        ),
        re.compile(
            r"\n?\s*if\s*\(\s*typeof\s+module\s*!==?\s*['\"]undefined['\"]\s*\)\s*\{\s*module\.exports\s*=\s*(?P<expr>[\s\S]*?)\s*;\s*\}\s*$",
            re.IGNORECASE,
        ),
        re.compile(
            r"\n?\s*module\.exports\s*=\s*(?P<expr>[^;]+?)\s*;\s*$",
            re.IGNORECASE,
        ),
        re.compile(
            r"\n?\s*exports\.default\s*=\s*(?P<expr>[^;]+?)\s*;\s*$",
            re.IGNORECASE,
        ),
    ]

    updated = js
    rewrites = 0
    while True:
        matched = False
        for pattern in footer_patterns:
            match = pattern.search(updated)
            if not match:
                continue
            export_lines = _browser_global_export_lines(match.group("expr") or "")
            replacement = "\n"
            if export_lines:
                replacement = "\n" + "\n".join(export_lines) + "\n"
            updated = updated[:match.start()] + replacement
            rewrites += 1
            matched = True
            break
        if not matched:
            break

    if rewrites:
        logger.info(
            "Post-processed JavaScript: rewrote %s CommonJS export footer(s) for browser delivery",
            rewrites,
        )
    return updated


def _find_matching_js_brace(text: str, start_index: int) -> int:
    if start_index < 0 or start_index >= len(text) or text[start_index] != "{":
        return -1

    depth = 0
    state = "code"
    escape = False
    i = start_index
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if state == "code":
            if ch == "'":
                state = "single"
            elif ch == '"':
                state = "double"
            elif ch == "`":
                state = "template"
            elif ch == "/" and nxt == "/":
                state = "line_comment"
                i += 1
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                i += 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        elif state == "single":
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "'":
                state = "code"
        elif state == "double":
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                state = "code"
        elif state == "template":
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "`":
                state = "code"
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 1
        i += 1
    return -1


def _rewrite_invalid_three_basic_materials(js: str) -> str:
    if "MeshBasicMaterial" not in js:
        return js

    invalid_props = ("emissive", "emissiveIntensity", "metalness", "roughness")
    rewritten: list[str] = []
    cursor = 0
    rewrite_count = 0

    while True:
        match = re.search(r"\bMeshBasicMaterial\s*\(", js[cursor:])
        if not match:
            rewritten.append(js[cursor:])
            break

        abs_start = cursor + match.start()
        abs_end = cursor + match.end()
        body_start = js.find("{", abs_end - 1)
        if body_start < 0:
            rewritten.append(js[cursor:])
            break
        body_end = _find_matching_js_brace(js, body_start)
        if body_end < 0:
            rewritten.append(js[cursor:])
            break

        body = js[body_start:body_end + 1]
        has_invalid_props = any(
            re.search(rf"\b{re.escape(prop)}\s*:", body, re.IGNORECASE)
            for prop in invalid_props
        )

        rewritten.append(js[cursor:abs_start])
        if has_invalid_props:
            rewritten.append("MeshStandardMaterial")
            rewritten.append(js[abs_start + len("MeshBasicMaterial"):body_end + 1])
            rewrite_count += 1
        else:
            rewritten.append(js[abs_start:body_end + 1])
        cursor = body_end + 1

    updated = "".join(rewritten)
    if rewrite_count:
        logger.info(
            "Post-processed JavaScript: upgraded %s invalid Three.js MeshBasicMaterial call(s)",
            rewrite_count,
        )
    return updated


_TPS_OFFSET_PITCH_RE = re.compile(
    r"applyAxisAngle\(\s*new\s+THREE\.Vector3\(\s*1\s*,\s*0\s*,\s*0\s*\)\s*,\s*(?P<expr>[A-Za-z_$][\w$.]*)\s*\)",
    re.IGNORECASE,
)
_TPS_OFFSET_YAW_RE = re.compile(
    r"applyAxisAngle\(\s*new\s+THREE\.Vector3\(\s*0\s*,\s*1\s*,\s*0\s*\)\s*,\s*(?P<expr>[A-Za-z_$][\w$.]*)\s*\)",
    re.IGNORECASE,
)
_TPS_TRIG_ORBIT_RE = re.compile(
    r"Math\.sin\(\s*(?P<yaw>[A-Za-z_$][\w$.]*)\s*\)\s*\*\s*Math\.cos\(\s*(?P<pitch>[A-Za-z_$][\w$.]*)\s*\)[\s\S]{0,220}?"
    r"Math\.sin\(\s*(?P=pitch)\s*\)[\s\S]{0,220}?"
    r"Math\.cos\(\s*(?P=yaw)\s*\)\s*\*\s*Math\.cos\(\s*(?P=pitch)\s*\)",
    re.IGNORECASE,
)
_TPS_TRIG_ORBIT_CACHED_COS_RE = re.compile(
    r"(?:const|let|var)\s+(?P<cos_pitch>[A-Za-z_$][\w$]*)\s*=\s*Math\.cos\(\s*(?P<pitch>[A-Za-z_$][\w$.]*)\s*\)\s*;[\s\S]{0,420}?"
    r"Math\.sin\(\s*(?P<yaw>[A-Za-z_$][\w$.]*)\s*\)\s*\*\s*(?P=cos_pitch)[\s\S]{0,220}?"
    r"Math\.sin\(\s*(?P=pitch)\s*\)[\s\S]{0,220}?"
    r"Math\.cos\(\s*(?P=yaw)\s*\)\s*\*\s*(?P=cos_pitch)",
    re.IGNORECASE,
)
_TPS_EULER_ORBIT_RE = re.compile(
    r"(?:const|let|var)\s+(?P<offset>[A-Za-z_$][\w$]*)\s*=\s*new\s+THREE\.Vector3\(\s*0\s*,\s*0\s*,\s*[^)]+\)"
    r"[\s\S]{0,320}?\b(?P=offset)\.applyEuler\(\s*new\s+THREE\.Euler\(\s*(?P<pitch>[A-Za-z_$][\w$.]*)\s*,\s*(?P<yaw>[A-Za-z_$][\w$.]*)\s*,\s*[^)]*\)\s*\)",
    re.IGNORECASE,
)
_TPS_ROTATED_OFFSET_DECL_RE = re.compile(
    r"(?P<prefix>(?:const|let|var)\s+(?P<offset>[A-Za-z_$][\w$]*)\s*=\s*new\s+THREE\.Vector3\(\s*0\s*,\s*0\s*,\s*)"
    r"(?P<distance>[A-Za-z_$][\w$.]*)"
    r"(?P<suffix>\s*\)\s*;)",
    re.IGNORECASE,
)
_TPS_CAMERA_ORBIT_PLUS_PLUS_RE = re.compile(
    r"player\.position\.x\s*\+\s*Math\.sin\(\s*(?:camera(?:Azimuth|Yaw)|targetCameraAzimuth|yaw)\s*\)"
    r"[\s\S]{0,220}?"
    r"player\.position\.z\s*\+\s*Math\.cos\(\s*(?:camera(?:Azimuth|Yaw)|targetCameraAzimuth|yaw)\s*\)",
    re.IGNORECASE,
)
_TPS_CAMERA_ORBIT_MINUS_MINUS_RE = re.compile(
    r"player\.position\.x\s*-\s*Math\.sin\(\s*(?:camera(?:Azimuth|Yaw)|targetCameraAzimuth|yaw)\s*\)"
    r"[\s\S]{0,220}?"
    r"player\.position\.z\s*-\s*Math\.cos\(\s*(?:camera(?:Azimuth|Yaw)|targetCameraAzimuth|yaw)\s*\)",
    re.IGNORECASE,
)
_TPS_AXIS_REF_RE = re.compile(r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\b")
_TPS_DIRECT_PITCH_TARGET_RE = re.compile(
    r"(?<![\w$])(?P<expr>(?:[A-Za-z_$][\w$]*\.)*(?:targetPitch|cameraPitch|lookPitch))(?![\w$])",
    re.IGNORECASE,
)
_TPS_MIRRORED_RIGHT_VECTOR_CTOR_RE = re.compile(
    r"new\s+THREE\.Vector3\(\s*-\s*(?P<forward>[A-Za-z_$][\w$.]*)\.z\s*,\s*0\s*,\s*(?P=forward)\.x\s*\)",
    re.IGNORECASE,
)
_TPS_MIRRORED_RIGHT_VECTOR_SET_RE = re.compile(
    r"(?P<prefix>\b[A-Za-z_$][\w$.]*\.set\(\s*)-\s*(?P<forward>[A-Za-z_$][\w$.]*)\.z\s*,\s*0\s*,\s*(?P=forward)\.x\s*(?P<suffix>\))",
    re.IGNORECASE,
)
_TPS_CANONICAL_RIGHT_VECTOR_CTOR_RE = re.compile(
    r"new\s+THREE\.Vector3\(\s*(?P<forward>[A-Za-z_$][\w$.]*)\.z\s*,\s*0\s*,\s*-\s*(?P=forward)\.x\s*\)",
    re.IGNORECASE,
)
_TPS_CANONICAL_RIGHT_VECTOR_SET_RE = re.compile(
    r"(?P<prefix>\b[A-Za-z_$][\w$.]*\.set\(\s*)(?P<forward>[A-Za-z_$][\w$.]*)\.z\s*,\s*0\s*,\s*-\s*(?P=forward)\.x\s*(?P<suffix>\))",
    re.IGNORECASE,
)
_TPS_LEFT_STRAFE_OP_RE = re.compile(
    r"(?P<prefix>if\s*\(\s*[^)\n]*(?:KeyA|ArrowLeft|input\.left|moveLeft)[^)\n]*\)\s*(?:\{\s*)?move\.)(?P<op>add|sub)(?P<suffix>\(\s*right\s*\)\s*;?(?:\s*\})?)",
    re.IGNORECASE,
)
_TPS_RIGHT_STRAFE_OP_RE = re.compile(
    r"(?P<prefix>if\s*\(\s*[^)\n]*(?:KeyD|ArrowRight|input\.right|moveRight)[^)\n]*\)\s*(?:\{\s*)?move\.)(?P<op>add|sub)(?P<suffix>\(\s*right\s*\)\s*;?(?:\s*\})?)",
    re.IGNORECASE,
)
_TPS_ROTATED_OFFSET_NEGATIVE_RE = re.compile(
    r"new\s+THREE\.Vector3\(\s*0\s*,\s*0\s*,\s*-\s*[A-Za-z_$][\w$.]*\s*\)[\s\S]{0,320}?\.(?:applyAxisAngle|applyEuler)\(",
    re.IGNORECASE,
)
_TPS_ROTATED_OFFSET_POSITIVE_RE = re.compile(
    r"new\s+THREE\.Vector3\(\s*0\s*,\s*0\s*,\s*[A-Za-z_$][\w$.]*\s*\)[\s\S]{0,320}?\.(?:applyAxisAngle|applyEuler)\(",
    re.IGNORECASE,
)


def _set_common_drag_axis_sign(
    js: str,
    axis_expr: str,
    deltas: tuple[str, ...],
    *,
    positive: bool,
) -> tuple[str, int]:
    updated = js
    replacements = 0
    escaped_axis = re.escape(str(axis_expr or "").strip())
    if not escaped_axis:
        return updated, replacements
    for delta in deltas:
        target_op = "+=" if positive else "-="
        target_assign_op = "+" if positive else "-"
        pattern = re.compile(
            rf"(?P<prefix>\b{escaped_axis}\s*)(?P<op>\+=|-=)\s*(?P<body>{delta}\s*(?:\*\s*[^;\n]+)?)",
            re.IGNORECASE,
        )
        def _replace_inplace(match: re.Match[str]) -> str:
            nonlocal replacements
            if str(match.group("op") or "") != target_op:
                replacements += 1
            return f"{match.group('prefix')}{target_op} {match.group('body')}"
        updated = pattern.sub(_replace_inplace, updated)
        assign_pattern = re.compile(
            rf"(?P<prefix>\b{escaped_axis}\s*=\s*[^;\n]*?\b{escaped_axis}\b\s*)(?P<op>[+-])\s*(?P<body>{delta}\s*(?:\*\s*[^;\n]+)?)",
            re.IGNORECASE,
        )
        def _replace_assign(match: re.Match[str]) -> str:
            nonlocal replacements
            if str(match.group("op") or "") != target_assign_op:
                replacements += 1
            return f"{match.group('prefix')}{target_assign_op} {match.group('body')}"
        updated = assign_pattern.sub(_replace_assign, updated)
    return updated, replacements


def _extract_tps_axis_refs(expr: str, axis_keywords: tuple[str, ...]) -> set[str]:
    refs: set[str] = set()
    for token in _TPS_AXIS_REF_RE.findall(str(expr or "")):
        lower = token.lower()
        if any(keyword in lower for keyword in axis_keywords):
            refs.add(token)
    return refs


def _expand_tps_axis_aliases(js: str, exprs: set[str], axis_keywords: tuple[str, ...]) -> set[str]:
    expanded = {str(expr or "").strip() for expr in exprs if str(expr or "").strip()}
    if not expanded:
        return expanded

    changed = True
    while changed:
        changed = False
        for expr in tuple(expanded):
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", expr):
                continue

            assign_pattern = re.compile(
                rf"\b(?:const|let|var)\s+{re.escape(expr)}\s*=\s*(?P<rhs>[^;\n]+)",
                re.IGNORECASE,
            )
            for match in assign_pattern.finditer(js):
                for ref in _extract_tps_axis_refs(match.group("rhs") or "", axis_keywords):
                    if ref not in expanded:
                        expanded.add(ref)
                        changed = True

            alias_pattern = re.compile(
                rf"\b(?:const|let|var)\s+(?P<lhs>[A-Za-z_$][\w$]*)\s*=\s*[^;\n]*\b{re.escape(expr)}\b[^;\n]*",
                re.IGNORECASE,
            )
            for match in alias_pattern.finditer(js):
                lhs = str(match.group("lhs") or "").strip()
                if lhs and any(keyword in lhs.lower() for keyword in axis_keywords) and lhs not in expanded:
                    expanded.add(lhs)
                    changed = True

    return expanded


def _expand_tps_member_axis_aliases(js: str, exprs: set[str], axis_keywords: tuple[str, ...]) -> set[str]:
    expanded = {str(expr or "").strip() for expr in exprs if str(expr or "").strip()}
    if not expanded:
        return expanded

    prefixes = {
        expr.rsplit(".", 1)[0] + "."
        for expr in expanded
        if "." in expr and str(expr or "").strip()
    }
    for prefix in prefixes:
        member_pattern = re.compile(
            rf"{re.escape(prefix)}(?P<name>[A-Za-z_$][\w$]*)",
            re.IGNORECASE,
        )
        for match in member_pattern.finditer(js):
            name = str(match.group("name") or "").strip()
            if not name or not any(keyword in name.lower() for keyword in axis_keywords):
                continue
            expanded.add(f"{prefix}{name}")
    return expanded


def _looks_like_tps_camera_rig(js: str) -> bool:
    blob = str(js or "")
    return bool(
        re.search(r"\b(?:deltaY|movementY|pointer(?:Delta)?Y|input\.lookDy)\b", blob, re.IGNORECASE)
        and re.search(
            r"lookAt\(|PerspectiveCamera|processInput|orbit|targetPitch|cameraPitch|lookPitch|TPS|third[- ]person",
            blob,
            re.IGNORECASE,
        )
    )


def _forward_uses_inverted_trig_convention(js: str, forward_expr: str) -> bool:
    forward = str(forward_expr or "").strip()
    if not forward or "." in forward:
        return False
    escaped = re.escape(forward)
    neg_patterns = (
        rf"\b{escaped}\b\s*=\s*new\s+THREE\.Vector3\(\s*-\s*Math\.sin\([^)]*\)\s*,\s*0\s*,\s*-\s*Math\.cos\([^)]*\)\s*\)\s*(?:\.normalize\(\s*\))?",
        rf"\b{escaped}\.set\(\s*-\s*Math\.sin\([^)]*\)\s*,\s*0\s*,\s*-\s*Math\.cos\([^)]*\)\s*\)",
    )
    pos_patterns = (
        rf"\b{escaped}\b\s*=\s*new\s+THREE\.Vector3\(\s*Math\.sin\([^)]*\)\s*,\s*0\s*,\s*Math\.cos\([^)]*\)\s*\)\s*(?:\.normalize\(\s*\))?",
        rf"\b{escaped}\.set\(\s*Math\.sin\([^)]*\)\s*,\s*0\s*,\s*Math\.cos\([^)]*\)\s*\)",
    )
    has_negative = any(re.search(pattern, js, re.IGNORECASE) for pattern in neg_patterns)
    has_positive = any(re.search(pattern, js, re.IGNORECASE) for pattern in pos_patterns)
    return bool(has_negative and not has_positive)


def _normalize_common_tps_follow_camera_frame(js: str) -> str:
    if "THREE.Vector3" not in js or "camera.lookAt" not in js:
        return js
    if not re.search(
        r"new\s+THREE\.Vector3\(\s*Math\.sin\(\s*[A-Za-z_$][\w$.]*\s*\)\s*,\s*0\s*,\s*Math\.cos\(\s*[A-Za-z_$][\w$.]*\s*\)\s*\)\.normalize\(\s*\)",
        js,
        re.IGNORECASE,
    ):
        return js
    if not re.search(r"move\.(?:add|sub)\(\s*forward\s*\)", js, re.IGNORECASE):
        return js

    replacements = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal replacements
        offset_name = str(match.group("offset") or "").strip()
        distance_expr = str(match.group("distance") or "").strip()
        if not offset_name or not distance_expr or distance_expr.startswith("-"):
            return match.group(0)
        window = js[match.start(): min(len(js), match.start() + 720)]
        rotated_offset = bool(
            re.search(
                rf"\b{re.escape(offset_name)}\.(?:applyAxisAngle|applyEuler)\(",
                window,
                re.IGNORECASE,
            )
        )
        copied_into_camera = bool(
            re.search(
                rf"(?:camera|threeCamera)\.position\.copy\([^;]+?\)\.add\(\s*{re.escape(offset_name)}\s*\)",
                window,
                re.IGNORECASE,
            )
        )
        if not (rotated_offset and copied_into_camera):
            return match.group(0)
        replacements += 1
        return f"{match.group('prefix')}-{distance_expr}{match.group('suffix')}"

    updated = _TPS_ROTATED_OFFSET_DECL_RE.sub(_replace, js)
    if replacements:
        logger.info(
            "Post-processed JavaScript: moved %s TPS orbit camera offset(s) behind the player to keep W-forward non-mirrored",
            replacements,
        )
    return updated


def _normalize_common_tps_orbit_drag_semantics(js: str) -> str:
    if "delta" not in js and "movement" not in js:
        return js

    pitch_exprs = {str(match.group("expr") or "").strip() for match in _TPS_OFFSET_PITCH_RE.finditer(js)}
    yaw_exprs = {str(match.group("expr") or "").strip() for match in _TPS_OFFSET_YAW_RE.finditer(js)}
    trig_match = _TPS_TRIG_ORBIT_RE.search(js)
    if trig_match:
        pitch_exprs.add(str(trig_match.group("pitch") or "").strip())
        yaw_exprs.add(str(trig_match.group("yaw") or "").strip())
    cached_trig_match = _TPS_TRIG_ORBIT_CACHED_COS_RE.search(js)
    if cached_trig_match:
        pitch_exprs.add(str(cached_trig_match.group("pitch") or "").strip())
        yaw_exprs.add(str(cached_trig_match.group("yaw") or "").strip())
    for euler_match in _TPS_EULER_ORBIT_RE.finditer(js):
        pitch_exprs.add(str(euler_match.group("pitch") or "").strip())
        yaw_exprs.add(str(euler_match.group("yaw") or "").strip())
    pitch_exprs = _expand_tps_axis_aliases(js, pitch_exprs, ("pitch", "anglex", "elevation"))
    yaw_exprs = _expand_tps_axis_aliases(js, yaw_exprs, ("yaw", "azimuth", "heading"))
    pitch_exprs = _expand_tps_member_axis_aliases(js, pitch_exprs, ("pitch", "anglex", "elevation"))
    yaw_exprs = _expand_tps_member_axis_aliases(js, yaw_exprs, ("yaw", "azimuth", "heading"))
    if not pitch_exprs and _looks_like_tps_camera_rig(js):
        pitch_exprs.update(
            str(match.group("expr") or "").strip()
            for match in _TPS_DIRECT_PITCH_TARGET_RE.finditer(js)
            if str(match.group("expr") or "").strip()
        )
    if not pitch_exprs and not yaw_exprs:
        return js

    plus_plus_orbit = bool(_TPS_CAMERA_ORBIT_PLUS_PLUS_RE.search(js))
    minus_minus_orbit = bool(_TPS_CAMERA_ORBIT_MINUS_MINUS_RE.search(js))
    rotated_offset_orbit = bool(
        _TPS_OFFSET_YAW_RE.search(js)
        or _TPS_EULER_ORBIT_RE.search(js)
    )
    rotated_offset_negative = bool(_TPS_ROTATED_OFFSET_NEGATIVE_RE.search(js))
    rotated_offset_positive = bool(_TPS_ROTATED_OFFSET_POSITIVE_RE.search(js))
    yaw_positive = True
    if minus_minus_orbit and not plus_plus_orbit:
        yaw_positive = False
    elif plus_plus_orbit:
        yaw_positive = True
    elif rotated_offset_orbit:
        if rotated_offset_negative and not rotated_offset_positive:
            yaw_positive = False
        elif rotated_offset_positive and not rotated_offset_negative:
            yaw_positive = True

    updated = js
    replacements = 0
    for yaw_expr in yaw_exprs:
        updated, count = _set_common_drag_axis_sign(
            updated,
            yaw_expr,
            ("input\\.lookDx", "deltaX", "pointer(?:Delta)?X", "dx", "e\\.movementX", "movementX"),
            positive=yaw_positive,
        )
        replacements += count
    for pitch_expr in pitch_exprs:
        updated, count = _set_common_drag_axis_sign(
            updated,
            pitch_expr,
            ("input\\.lookDy", "deltaY", "pointer(?:Delta)?Y", "dy", "e\\.movementY", "movementY"),
            positive=False,
        )
        replacements += count
    if replacements:
        logger.info(
            "Post-processed JavaScript: normalized %s TPS orbit drag control assignment(s)",
            replacements,
        )
    return updated


def _normalize_common_tps_strafe_semantics(js: str) -> str:
    if "right" not in js or "forward" not in js:
        return js

    updated = js
    replacements = 0

    def _replace_mirrored_right_ctor(match: re.Match[str]) -> str:
        nonlocal replacements
        forward = str(match.group("forward") or "").strip()
        if _forward_uses_inverted_trig_convention(updated, forward):
            return match.group(0)
        replacements += 1
        return f"new THREE.Vector3({forward}.z, 0, -{forward}.x)"

    def _replace_canonical_right_ctor(match: re.Match[str]) -> str:
        nonlocal replacements
        forward = str(match.group("forward") or "").strip()
        if not _forward_uses_inverted_trig_convention(updated, forward):
            return match.group(0)
        replacements += 1
        return f"new THREE.Vector3(-{forward}.z, 0, {forward}.x)"

    def _replace_mirrored_right_set(match: re.Match[str]) -> str:
        nonlocal replacements
        forward = str(match.group("forward") or "").strip()
        if _forward_uses_inverted_trig_convention(updated, forward):
            return match.group(0)
        replacements += 1
        return f"{match.group('prefix')}{forward}.z, 0, -{forward}.x{match.group('suffix')}"

    def _replace_canonical_right_set(match: re.Match[str]) -> str:
        nonlocal replacements
        forward = str(match.group("forward") or "").strip()
        if not _forward_uses_inverted_trig_convention(updated, forward):
            return match.group(0)
        replacements += 1
        return f"{match.group('prefix')}-{forward}.z, 0, {forward}.x{match.group('suffix')}"

    updated = _TPS_MIRRORED_RIGHT_VECTOR_CTOR_RE.sub(_replace_mirrored_right_ctor, updated)
    updated = _TPS_CANONICAL_RIGHT_VECTOR_CTOR_RE.sub(_replace_canonical_right_ctor, updated)
    updated = _TPS_MIRRORED_RIGHT_VECTOR_SET_RE.sub(_replace_mirrored_right_set, updated)
    updated = _TPS_CANONICAL_RIGHT_VECTOR_SET_RE.sub(_replace_canonical_right_set, updated)

    def _replace_left(match: re.Match[str]) -> str:
        nonlocal replacements
        if str(match.group("op") or "").lower() != "sub":
            replacements += 1
        return f"{match.group('prefix')}sub{match.group('suffix')}"

    def _replace_right(match: re.Match[str]) -> str:
        nonlocal replacements
        if str(match.group("op") or "").lower() != "add":
            replacements += 1
        return f"{match.group('prefix')}add{match.group('suffix')}"

    updated = _TPS_LEFT_STRAFE_OP_RE.sub(_replace_left, updated)
    updated = _TPS_RIGHT_STRAFE_OP_RE.sub(_replace_right, updated)

    if replacements:
        logger.info(
            "Post-processed JavaScript: normalized %s TPS strafe/right-vector control assignment(s)",
            replacements,
        )
    return updated


def _guard_common_three_render_calls(js: str) -> str:
    if ".render(" not in js:
        return js

    def _replace(match: re.Match[str]) -> str:
        renderer = str(match.group("renderer") or "").strip()
        scene = str(match.group("scene") or "").strip()
        camera = str(match.group("camera") or "").strip()
        if not renderer or not scene or not camera:
            return match.group(0)
        return f"window.__EVERMIND_SAFE_RENDER({renderer}, {scene}, {camera});"

    updated, count = _THREE_RENDER_CALL_RE.subn(_replace, js)
    if count:
        logger.info(
            "Post-processed JavaScript: guarded %s Three.js render call(s) against pre-init/null runtime state",
            count,
        )
    return updated


_OBJECT_LITERAL_BASE_PROP_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[A-Za-z_$][\w$]*)\s*:\s*(?P<expr>.+?)(?P<trailing>,?)\s*$"
)
_OBJECT_LITERAL_MEMBER_PROP_RE = re.compile(
    r"^(?P<indent>\s*)(?P<path>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\s*:\s*(?P<expr>.+?)(?P<trailing>,?)\s*$"
)


def _object_literal_seed_expr_is_repairable(expr: str) -> bool:
    candidate = str(expr or "").strip()
    if not candidate:
        return False
    if candidate.endswith(("{", "[", "(")):
        return False
    return (
        candidate.count("(") == candidate.count(")")
        and candidate.count("[") == candidate.count("]")
        and candidate.count("{") == candidate.count("}")
    )


def _repair_common_object_literal_member_mutations(js: str) -> str:
    if "." not in js or ":" not in js:
        return js

    lines = js.splitlines()
    rebuilt: list[str] = []
    index = 0
    repaired_groups = 0

    while index < len(lines):
        line = lines[index]
        base_match = _OBJECT_LITERAL_BASE_PROP_RE.match(line)
        if (
            not base_match
            or base_match.group("trailing") != ","
            or not _object_literal_seed_expr_is_repairable(base_match.group("expr") or "")
        ):
            rebuilt.append(line)
            index += 1
            continue

        indent = str(base_match.group("indent") or "")
        root = str(base_match.group("key") or "").strip()
        expr = str(base_match.group("expr") or "").strip()
        member_entries: list[tuple[str, str]] = []
        lookahead = index + 1

        while lookahead < len(lines):
            member_match = _OBJECT_LITERAL_MEMBER_PROP_RE.match(lines[lookahead])
            if not member_match or str(member_match.group("indent") or "") != indent:
                break
            member_path = str(member_match.group("path") or "").strip()
            if not member_path.startswith(f"{root}."):
                break
            member_entries.append((
                member_path[len(root) + 1:],
                str(member_match.group("expr") or "").strip(),
            ))
            lookahead += 1

        if not member_entries:
            rebuilt.append(line)
            index += 1
            continue

        repaired_groups += 1
        temp_name = f"__evermind_{re.sub(r'\\W+', '_', root).strip('_') or 'value'}_{repaired_groups}"
        rebuilt.append(f"{indent}{root}: (() => {{")
        rebuilt.append(f"{indent}  const {temp_name} = {expr};")
        for member_path, member_expr in member_entries:
            rebuilt.append(f"{indent}  {temp_name}.{member_path} = {member_expr};")
        rebuilt.append(f"{indent}  return {temp_name};")
        rebuilt.append(f"{indent}}})(),")
        index = lookahead

    updated = "\n".join(rebuilt)
    if js.endswith("\n"):
        updated += "\n"
    if repaired_groups:
        logger.info(
            "Post-processed JavaScript: repaired %s invalid object-literal member mutation block(s)",
            repaired_groups,
        )
    return updated


def _script_tag_contains_javascript(attrs: str) -> bool:
    attributes = str(attrs or "")
    if re.search(r"\bsrc\s*=", attributes, re.IGNORECASE):
        return False
    type_match = re.search(r'\btype\s*=\s*["\']([^"\']+)["\']', attributes, re.IGNORECASE)
    if not type_match:
        return True
    script_type = str(type_match.group(1) or "").strip().lower()
    return script_type in {
        "text/javascript",
        "application/javascript",
        "text/ecmascript",
        "application/ecmascript",
        "module",
    }


def _postprocess_inline_script_blocks(html: str) -> str:
    if "<script" not in str(html or "").lower():
        return html

    def _replace(match: re.Match[str]) -> str:
        attrs = str(match.group("attrs") or "")
        if re.search(r'\bdata-evermind-runtime-shim\s*=', attrs, re.IGNORECASE):
            return match.group(0)
        if not _script_tag_contains_javascript(attrs):
            return match.group(0)
        body = str(match.group("body") or "")
        processed = postprocess_javascript(body)
        return f"{match.group(1)}{processed}{match.group(4)}"

    return _INLINE_SCRIPT_BLOCK_RE.sub(_replace, html)


_SCRIPT_BLOCK_RE = re.compile(r"<script\b([^>]*)>([\s\S]*?)</script>", re.IGNORECASE)
_SCRIPT_SRC_ATTR_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_IIFE_HEAD_RE = re.compile(
    r"\A\s*[;!+~-]?\s*\(\s*(?:async\s+)?(?:function\s*\w*\s*\([^)]*\)|\([^)]*\)\s*=>|\w+\s*=>)\s*\{",
    re.IGNORECASE,
)
_IIFE_TAIL_RE = re.compile(r"\}\s*\)\s*\(\s*\)\s*;?\s*\Z")
_HANDLER_ATTR_RE = re.compile(
    r"""\bon[a-z]+\s*=\s*(?P<q>["'])(?P<expr>[^"']*)(?P=q)""",
    re.IGNORECASE,
)
_HANDLER_IDENT_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_FN_DECL_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
# Common browser globals — do NOT treat these as custom handlers.
_RESERVED_HANDLER_IDENTS = {
    "alert", "confirm", "prompt", "setTimeout", "setInterval", "requestAnimationFrame",
    "console", "window", "document", "event", "this", "return", "true", "false",
    "null", "undefined", "parseInt", "parseFloat", "String", "Number", "Boolean",
    "Array", "Object", "Date", "Math", "JSON", "Promise", "new", "typeof",
    "if", "else", "for", "while", "function", "void",
}
_LIBRARY_FINGERPRINTS = (
    ("three", re.compile(r"(?:^|/)three(?:\.min)?\.js\b|/three(?:@[^/]+)?/build/", re.IGNORECASE)),
    ("howler", re.compile(r"(?:^|/)howler(?:\.core)?(?:\.min)?\.js\b", re.IGNORECASE)),
    ("phaser", re.compile(r"(?:^|/)phaser(?:-[\w.]+)?(?:\.min)?\.js\b", re.IGNORECASE)),
    ("pixi", re.compile(r"(?:^|/)pixi(?:\.min)?\.js\b", re.IGNORECASE)),
    ("matter", re.compile(r"(?:^|/)matter(?:\.min)?\.js\b", re.IGNORECASE)),
    ("gsap", re.compile(r"(?:^|/)gsap(?:-[\w.]+)?(?:\.min)?\.js\b", re.IGNORECASE)),
    ("chart", re.compile(r"(?:^|/)chart(?:\.umd)?(?:\.min)?\.js\b", re.IGNORECASE)),
    ("d3", re.compile(r"(?:^|/)d3(?:\.v\d+)?(?:\.min)?\.js\b", re.IGNORECASE)),
    ("tone", re.compile(r"(?:^|/)Tone(?:\.min)?\.js\b", re.IGNORECASE)),
)


def _dedup_library_scripts(html: str) -> str:
    """v6.1.13 (maintainer 2026-04-20): merger sometimes concatenates script tags
    for the same library from both builders (local + CDN). Browser loads
    both, warns `WARNING: Multiple instances of Three.js being imported`,
    and game state corrupts. Keep the FIRST occurrence per library (prefer
    local `./_evermind_runtime/` path if one exists, otherwise first seen).
    Applies to every task type — not games-only — since websites can also
    double-load d3/chart/gsap.
    """
    text = str(html or "")
    if not text:
        return html

    matches = list(_SCRIPT_BLOCK_RE.finditer(text))
    if not matches:
        return text

    # lib -> index of keeper match
    keep_by_lib: dict[str, int] = {}
    drop_indices: set[int] = set()
    for idx, m in enumerate(matches):
        attrs = m.group(1) or ""
        body = m.group(2) or ""
        src_match = _SCRIPT_SRC_ATTR_RE.search(attrs)
        if not src_match or body.strip():
            continue
        src = src_match.group(1).strip()
        for lib, pat in _LIBRARY_FINGERPRINTS:
            if not pat.search(src):
                continue
            if lib not in keep_by_lib:
                keep_by_lib[lib] = idx
            else:
                prev = keep_by_lib[lib]
                prev_src = _SCRIPT_SRC_ATTR_RE.search(matches[prev].group(1) or "")
                prev_is_local = bool(prev_src and prev_src.group(1).startswith(("./_evermind_runtime/", "/_evermind_runtime/", "_evermind_runtime/")))
                cur_is_local = src.startswith(("./_evermind_runtime/", "/_evermind_runtime/", "_evermind_runtime/"))
                if cur_is_local and not prev_is_local:
                    drop_indices.add(prev)
                    keep_by_lib[lib] = idx
                else:
                    drop_indices.add(idx)
            break

    if not drop_indices:
        return text

    pieces: list[str] = []
    last = 0
    dropped = 0
    for idx, m in enumerate(matches):
        if idx in drop_indices:
            pieces.append(text[last:m.start()])
            trailing_nl = m.end()
            if trailing_nl < len(text) and text[trailing_nl] == "\n":
                trailing_nl += 1
            last = trailing_nl
            dropped += 1
        else:
            pieces.append(text[last:m.end()])
            last = m.end()
    pieces.append(text[last:])
    result = "".join(pieces)
    logger.info(
        "Post-processed HTML: removed %s duplicate <script src> tag(s) for library dedup",
        dropped,
    )
    return result


_IMG_TAG_RE = re.compile(r"<img\b([^>]*)>", re.IGNORECASE)
_IMG_SRC_ATTR_RE = re.compile(r"""\bsrc\s*=\s*(?P<q>["'])(?P<src>[^"']*)(?P=q)""", re.IGNORECASE)
_IMG_ONERROR_ATTR_RE = re.compile(r"""\bonerror\s*=\s*(["'])[^"']*\1""", re.IGNORECASE)
_IMG_STYLE_ATTR_RE = re.compile(r"""\bstyle\s*=\s*(?P<q>["'])(?P<val>[^"']*)(?P=q)""", re.IGNORECASE)


def _inject_image_fallback_guards(html: str) -> str:
    """v6.1.14g (maintainer 2026-04-20): reviewer keeps rejecting over broken
    images (naturalWidth===0, empty src, no fallback). Builder freely invents
    Unsplash URLs and most 404 on first load. Postprocess HARDENS every
    `<img>` tag:

    1. Empty `src=""` → replace with an SVG data-URI neutral placeholder so
       the browser NEVER resolves empty src to the page URL.
    2. Missing `onerror` → inject a handler that hides the img + reveals a
       gradient fallback on its parent (via CSS class toggle).
    3. Inject a global CSS block that shows a tasteful gradient placeholder
       on any `<img>` whose parent has `.img-loaded-fail` class.

    Applies to ALL task_types because every output may have images. Pure
    deterministic fix — no AI call.
    """
    text = str(html or "")
    if not text or "<img" not in text.lower():
        return html

    # 1. Inject the fallback CSS block once (idempotent)
    fallback_css = (
        "\n<style data-evermind-img-fallback=\"1\">\n"
        "  img[data-evermind-fallback-target]:is([src=''], [src$='/']) { opacity: 0; }\n"
        "  .img-loaded-fail {\n"
        "    background: linear-gradient(135deg, rgba(180,175,255,0.18), rgba(200,195,215,0.28));\n"
        "    position: relative;\n"
        "  }\n"
        "  .img-loaded-fail > img { opacity: 0 !important; }\n"
        "  .img-loaded-fail::after {\n"
        "    content: '';\n"
        "    position: absolute; inset: 0;\n"
        "    background-image:\n"
        "      radial-gradient(circle at 25% 35%, rgba(255,255,255,0.35) 2px, transparent 3px),\n"
        "      radial-gradient(circle at 75% 65%, rgba(255,255,255,0.20) 1px, transparent 2px),\n"
        "      linear-gradient(135deg, rgba(180,175,255,0.10) 0%, rgba(200,195,215,0.18) 100%);\n"
        "    background-size: 24px 24px, 18px 18px, 100% 100%;\n"
        "    pointer-events: none;\n"
        "  }\n"
        "</style>\n"
    )
    if 'data-evermind-img-fallback=\"1\"' not in text:
        if "</head>" in text:
            text = text.replace("</head>", fallback_css + "</head>", 1)
        else:
            text = fallback_css + text

    # 2. Walk every <img> tag
    edits: list[tuple[int, int, str]] = []
    fixed_src = 0
    injected_onerror = 0
    for match in _IMG_TAG_RE.finditer(text):
        attrs = match.group(1) or ""
        start, end = match.span()

        if "data-evermind-fallback-target" in attrs:
            continue  # already processed

        new_attrs = attrs

        # 2a. Fix empty src
        src_match = _IMG_SRC_ATTR_RE.search(new_attrs)
        if src_match:
            src_val = src_match.group("src").strip()
            if not src_val or src_val in ("#", "/"):
                # replace empty src with 1x1 transparent SVG data URI
                placeholder = (
                    "data:image/svg+xml;utf8,"
                    "%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%201%201%22%3E%3C/svg%3E"
                )
                new_attrs = (
                    new_attrs[: src_match.start("src")]
                    + placeholder
                    + new_attrs[src_match.end("src"):]
                )
                fixed_src += 1
        else:
            # no src attr at all — add placeholder to prevent browser weirdness
            placeholder = (
                "data:image/svg+xml;utf8,"
                "%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%201%201%22%3E%3C/svg%3E"
            )
            new_attrs = new_attrs + f' src="{placeholder}"'
            fixed_src += 1

        # 2b. Inject onerror if missing
        if not _IMG_ONERROR_ATTR_RE.search(new_attrs):
            # Mark parent with .img-loaded-fail class so CSS fallback shows;
            # also hide the img itself.
            onerror_handler = (
                "this.parentElement&&this.parentElement.classList.add('img-loaded-fail');"
                "this.style.display='none';"
            )
            new_attrs = new_attrs + f" onerror=\"{onerror_handler}\""
            injected_onerror += 1

        # 2c. Mark as processed
        new_attrs = new_attrs + ' data-evermind-fallback-target="1"'

        if new_attrs != attrs:
            edits.append((start, end, f"<img{new_attrs}>"))

    if not edits:
        return text

    out: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        out.append(text[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(text[cursor:])
    result = "".join(out)

    if fixed_src or injected_onerror:
        logger.info(
            "Post-processed HTML: image fallback guards — fixed_empty_src=%s, injected_onerror=%s",
            fixed_src, injected_onerror,
        )
    return result


def _inject_iife_handler_exports(html: str) -> str:
    """v6.1.13 (maintainer 2026-04-20): REAL INCIDENT — merger shipped a 3D
    shooter where `onclick="startGame()"` threw `ReferenceError: startGame
    is not defined` because the 47 KB game logic sat inside `(function() {
    ... })()` but the button called through inline HTML attribute into
    global scope. Auto-fix: inside every inline IIFE that declares a
    function whose name matches any HTML handler attribute, insert
    `window.NAME = NAME;` right before the IIFE closer so inline handlers
    can reach it.
    """
    text = str(html or "")
    if not text:
        return html

    # 1) collect handler identifiers referenced in HTML attributes
    handler_ids: set[str] = set()
    for attr_match in _HANDLER_ATTR_RE.finditer(text):
        expr = attr_match.group("expr") or ""
        for ident_match in _HANDLER_IDENT_RE.finditer(expr):
            name = ident_match.group(1)
            if name and name not in _RESERVED_HANDLER_IDENTS:
                handler_ids.add(name)
    if not handler_ids:
        return text

    # 2) walk inline script blocks; for each IIFE, export handler fns
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement)
    injected = 0
    for match in _SCRIPT_BLOCK_RE.finditer(text):
        attrs = match.group(1) or ""
        body = match.group(2) or ""
        if _SCRIPT_SRC_ATTR_RE.search(attrs):
            continue  # external script, skip
        if not body.strip():
            continue
        if not _IIFE_HEAD_RE.search(body):
            continue
        if not _IIFE_TAIL_RE.search(body):
            continue
        # Find function declarations inside the IIFE
        declared = {m.group(1) for m in _FN_DECL_RE.finditer(body)}
        needed = declared & handler_ids
        if not needed:
            continue
        # Skip identifiers already explicitly exported
        def _already_exported(name: str) -> bool:
            return bool(
                re.search(
                    rf"\b(?:window|globalThis|self)\s*\.\s*{re.escape(name)}\s*=",
                    body,
                )
            )
        needed = {n for n in needed if not _already_exported(n)}
        if not needed:
            continue
        # Inject export lines just before the IIFE closer
        tail_match = _IIFE_TAIL_RE.search(body)
        if not tail_match:
            continue
        insert_at = tail_match.start()
        exports = "\n/* evermind-postprocess: scope bridge */\n" + "".join(
            f"try {{ window.{n} = {n}; }} catch(_) {{}}\n" for n in sorted(needed)
        )
        new_body = body[:insert_at] + exports + body[insert_at:]
        block_start = match.start()
        block_end = match.end()
        new_block = f"<script{attrs}>{new_body}</script>"
        edits.append((block_start, block_end, new_block))
        injected += len(needed)

    if not edits:
        return text

    edits.sort()
    out: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        out.append(text[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(text[cursor:])
    result = "".join(out)
    logger.info(
        "Post-processed HTML: injected %s window.* scope-bridge export(s) for inline IIFE handler(s)",
        injected,
    )
    return result


def _has_global_function_definition(html: str, fn_name: str) -> bool:
    escaped = re.escape(str(fn_name or "").strip())
    if not escaped:
        return False
    patterns = (
        rf"\b(?:async\s+)?function\s+{escaped}\s*\(",
        rf"\b(?:const|let|var)\s+{escaped}\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)",
        rf"\b(?:window|globalThis|self)\s*\.\s*{escaped}\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)",
    )
    return any(re.search(pattern, html or "", re.IGNORECASE) for pattern in patterns)


def _inject_missing_game_menu_handler_shims(html: str) -> str:
    text = str(html or "")
    if not text:
        return html
    if 'data-evermind-runtime-shim="game-menu-handlers"' in text:
        return html

    needs_show = bool(re.search(r"\bon\w+\s*=\s*['\"][^'\"]*\bshowInstructions\s*\(", text, re.IGNORECASE)) and not _has_global_function_definition(text, "showInstructions")
    needs_hide = bool(re.search(r"\bon\w+\s*=\s*['\"][^'\"]*\bhideInstructions\s*\(", text, re.IGNORECASE)) and not _has_global_function_definition(text, "hideInstructions")
    if not needs_show and not needs_hide:
        return html

    lines = [
        '<script data-evermind-runtime-shim="game-menu-handlers">',
        "(() => {",
        "  const $ = (id) => document.getElementById(id);",
    ]
    if needs_show:
        lines.extend([
            "  if (typeof window.showInstructions !== 'function') {",
            "    window.showInstructions = function showInstructionsShim() {",
            "      const menu = $('main-menu');",
            "      const panel = $('instructions');",
            "      if (menu && panel) menu.classList.add('hidden');",
            "      if (panel) {",
            "        panel.classList.remove('hidden');",
            "        panel.classList.add('active');",
            "        panel.removeAttribute('hidden');",
            "      }",
            "    };",
            "  }",
        ])
    if needs_hide:
        lines.extend([
            "  if (typeof window.hideInstructions !== 'function') {",
            "    window.hideInstructions = function hideInstructionsShim() {",
            "      const menu = $('main-menu');",
            "      const panel = $('instructions');",
            "      if (panel) {",
            "        panel.classList.add('hidden');",
            "        panel.classList.remove('active');",
            "        panel.setAttribute('hidden', 'hidden');",
            "      }",
            "      if (menu) menu.classList.remove('hidden');",
            "    };",
            "  }",
        ])
    lines.extend([
        "})();",
        "</script>",
    ])
    shim = "\n".join(lines)
    if "</body>" in text:
        return text.replace("</body>", f"{shim}\n</body>", 1)
    return f"{text}\n{shim}"


def _auto_fix_common_js_typos(js: str) -> str:
    """v7.5: kill the most frequent kimi-emitted JS typos that bypass the
    reviewer/patcher loop and ship as runtime errors. Each pattern has been
    observed in the wild — `GameEngine.init(;` was the V7.4 PvZ bug that
    made the game uncontrollable. Keep this list narrow: only mechanical
    transformations that are unambiguous AND a syntax error otherwise.
    """
    if not js:
        return js
    out = js
    # 1. `func(;` → `func();`  (typo: extra ; inside empty arg list)
    out = re.sub(r"(\b\w+(?:\s*\.\s*\w+)*)\s*\(\s*;", r"\1();", out)
    # 2. `, , `  (double comma in arg/array list)
    out = re.sub(r",\s*,(?=\s*[\w'\"\[{(])", ", ", out)
    # 3. `obj.;` (dangling member access followed immediately by `;`)
    out = re.sub(r"(\w)\.\s*;", r"\1;", out)
    # 4. `()() ;` collapsed to `();` for accidentally doubled invocation
    #    that left a no-op trailing `()` — safe when the second pair is empty
    out = re.sub(r"\(\)\s*\(\)\s*;", "();", out)
    return out


def postprocess_javascript(js: str) -> str:
    if not js or not js.strip():
        return js

    js = _auto_fix_common_js_typos(js)

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

    js = _rewrite_invalid_three_basic_materials(js)
    js = _guard_common_three_render_calls(js)
    js = _normalize_common_tps_follow_camera_frame(js)
    js = _normalize_common_tps_strafe_semantics(js)
    js = _normalize_common_tps_orbit_drag_semantics(js)
    js = _repair_common_object_literal_member_mutations(js)
    js = _rewrite_commonjs_export_footer_for_browser(js)

    for identifier in ("nav", "overlay"):
        js = _guard_optional_js_hook_lines(js, identifier)

    # v6.4.18 (maintainer 2026-04-22): auto-balance parens/braces before handing
    # to preview_validation. Observed 2026-04-22 17:33: gpt-5.4/kimi both
    # emitted `forEach(...) => { }` with an orphan statement outside the
    # closure, leaving `)` > `(` by one or `}` > `{` by one. Node --check
    # flagged "missing ) after argument list" → 10-min builder retry. We
    # now silently trim/append 1-3 bracket delta so the site is at least
    # syntactically loadable; reviewer can still catch semantic issues.
    js = _auto_balance_js_brackets(js)

    if js != original:
        logger.info("Post-processed JavaScript: applied safety normalizations")
    return js


def _auto_balance_js_brackets(js: str) -> str:
    """v6.4.18 — conservative auto-balance for JavaScript bracket mismatches.

    Strategy: strip strings / comments / regex literals so bracket counting
    is accurate on real code. If the imbalance is ≤3 characters, append or
    trim the missing/extra bracket at the end of the source. Larger deltas
    are left alone — they indicate a deeper structural bug that should not
    be masked by mechanical balancing.
    """
    if not js or not js.strip():
        return js

    # Mask strings, template literals, line comments, block comments, and
    # regex literals with spaces so we count only real brackets.
    _masked_pattern = re.compile(
        r"/\*.*?\*/"                     # /* block comment */
        r"|//[^\n]*"                     # // line comment
        r"|'(?:\\.|[^'\\])*'"            # 'single quoted'
        r'|"(?:\\.|[^"\\])*"'            # "double quoted"
        r"|`(?:\\.|[^`\\])*`"            # `template literal`
        r"|/(?:\\.|[^/\\\n])+/[gimsuy]*",  # /regex/flags (naive; safe-ish for us)
        re.DOTALL,
    )
    masked = _masked_pattern.sub(lambda m: " " * len(m.group(0)), js)

    open_p = masked.count("(")
    close_p = masked.count(")")
    open_b = masked.count("{")
    close_b = masked.count("}")
    open_sq = masked.count("[")
    close_sq = masked.count("]")

    fixed = js

    # Parens
    if close_p > open_p:
        delta = close_p - open_p
        if delta <= 3:
            for _ in range(delta):
                idx = fixed.rfind(")")
                if idx < 0:
                    break
                fixed = fixed[:idx] + fixed[idx + 1:]
            logger.info("Post-processed JavaScript: trimmed %d orphan ')' chars", delta)
    elif open_p > close_p:
        delta = open_p - close_p
        if delta <= 3:
            fixed = fixed + (")" * delta)
            logger.info("Post-processed JavaScript: appended %d missing ')' chars", delta)

    # Braces
    if close_b > open_b:
        delta = close_b - open_b
        if delta <= 3:
            for _ in range(delta):
                idx = fixed.rfind("}")
                if idx < 0:
                    break
                fixed = fixed[:idx] + fixed[idx + 1:]
            logger.info("Post-processed JavaScript: trimmed %d orphan '}' chars", delta)
    elif open_b > close_b:
        delta = open_b - close_b
        if delta <= 3:
            fixed = fixed + ("}" * delta)
            logger.info("Post-processed JavaScript: appended %d missing '}' chars", delta)

    # Square brackets
    if close_sq > open_sq:
        delta = close_sq - open_sq
        if delta <= 3:
            for _ in range(delta):
                idx = fixed.rfind("]")
                if idx < 0:
                    break
                fixed = fixed[:idx] + fixed[idx + 1:]
            logger.info("Post-processed JavaScript: trimmed %d orphan ']' chars", delta)
    elif open_sq > close_sq:
        delta = open_sq - close_sq
        if delta <= 3:
            fixed = fixed + ("]" * delta)
            logger.info("Post-processed JavaScript: appended %d missing ']' chars", delta)

    return fixed


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


def _inject_game_low_height_safety_css(html: str) -> str:
    if not html or "</style>" not in html:
        return html
    if _GAME_LOW_HEIGHT_SAFETY_CSS.strip() in html:
        return html
    lower = html.lower()
    has_canvas = "<canvas" in lower
    # Only inject for shell-framework based games (game-shell, layout-root, etc.)
    # Do NOT inject for games that use simple fixed-position HUDs without a shell framework
    has_shell_signal = any(signal in lower for signal in (
        ".game-shell",
        ".layout-root",
        ".layout-frame",
        ".game-frame",
        ".shell-frame",
    ))
    has_rigid_viewport = any(signal in lower for signal in (
        "height:min(92vh",
        "height:min(95vh",
    ))
    if not has_canvas or not (has_shell_signal or has_rigid_viewport):
        return html
    return html.replace("</style>", f"{_GAME_LOW_HEIGHT_SAFETY_CSS}  </style>", 1)


def _inject_game_compact_hud_safety_css(html: str) -> str:
    if not html or "</style>" not in html:
        return html
    if _GAME_COMPACT_HUD_SAFETY_CSS.strip() in html:
        return html
    lower = html.lower()
    has_canvas = "<canvas" in lower
    has_hud = all(token in lower for token in (".hud", ".hud-card", ".hud-bottom"))
    has_loadout = ".weapon-panel" in lower or "weapon loadout" in lower
    if not (has_canvas and has_hud and has_loadout):
        return html
    return html.replace("</style>", f"{_GAME_COMPACT_HUD_SAFETY_CSS}  </style>", 1)


def _inject_game_overlay_safe_area_css(html: str) -> str:
    if not html or "</style>" not in html:
        return html
    if _GAME_OVERLAY_SAFE_AREA_CSS.strip() in html:
        return html
    lower = html.lower()
    if "<canvas" not in lower:
        return html
    overlay_markers = (
        ".menu-screen",
        ".briefing-screen",
        ".pause-screen",
        ".game-over-screen",
    )
    if not any(marker in lower for marker in overlay_markers):
        return html
    return html.replace("</style>", f"{_GAME_OVERLAY_SAFE_AREA_CSS}  </style>", 1)


def _inject_game_hidden_screen_strict_css(html: str) -> str:
    if not html or "</style>" not in html:
        return html
    if _GAME_HIDDEN_SCREEN_STRICT_CSS.strip() in html:
        return html
    lower = html.lower()
    if "<canvas" not in lower:
        return html
    hidden_screen_markers = (
        ".screen.hidden",
        "id=\"startscreen\"",
        "id='startscreen'",
        "id=\"gameoverscreen\"",
        "id='gameoverscreen'",
        "id=\"victoryscreen\"",
        "id='victoryscreen'",
        "class=\"screen hidden\"",
        "class='screen hidden'",
    )
    if not any(marker in lower for marker in hidden_screen_markers):
        return html
    return html.replace("</style>", f"{_GAME_HIDDEN_SCREEN_STRICT_CSS}  </style>", 1)


def _inject_three_capsule_geometry_compat_shim(html: str) -> str:
    if not html or "CapsuleGeometry" not in html:
        return html
    if 'data-evermind-runtime-shim="three-capsule"' in html:
        return html
    updated, count = _LOCAL_THREE_CLASSIC_SCRIPT_INSERT_RE.subn(
        r"\1\n" + _THREE_CAPSULE_GEOMETRY_SHIM,
        html,
        count=1,
    )
    if count:
        return updated
    return _inject_before_first_script_or_close(html, _THREE_CAPSULE_GEOMETRY_SHIM)


def _stabilize_game_pointer_lock(html: str) -> str:
    if not html:
        return html
    if ".requestPointerLock(" not in html and "_evermindSafeRequestPointerLock(" not in html:
        return html
    stabilized = _POINTER_LOCK_SHIM_BLOCK_RE.sub("\n", html)

    def _replace_pointer_lock_call(match: re.Match[str]) -> str:
        target = str(match.group("target") or "").strip()
        args = str(match.group("args") or "").strip()
        if not target:
            return match.group(0)
        if args:
            return f"_evermindSafeRequestPointerLock({target}, {args});"
        return f"_evermindSafeRequestPointerLock({target});"

    def _replace_pointer_lock_helper_method_call(match: re.Match[str]) -> str:
        object_ref = str(match.group("object") or "").strip()
        args = str(match.group("args") or "").strip()
        if not object_ref and not args:
            return "_evermindSafeRequestPointerLock();"
        if args == "canvas" and object_ref:
            return f"_evermindSafeRequestPointerLock({object_ref}.canvas);"
        if args:
            return f"_evermindSafeRequestPointerLock({args});"
        return "_evermindSafeRequestPointerLock();"

    stabilized = _POINTER_LOCK_HELPER_METHOD_CALL_RE.sub(
        _replace_pointer_lock_helper_method_call,
        stabilized,
    )
    stabilized = _POINTER_LOCK_CALL_RE.sub(_replace_pointer_lock_call, stabilized)
    if "_evermindSafeRequestPointerLock(" in stabilized:
        stabilized = _inject_before_first_script_or_close(stabilized, _POINTER_LOCK_SHIM)
    return stabilized


def _inject_game_runtime_perf_shim(html: str) -> str:
    if not html:
        return html
    if _GAME_RUNTIME_PERF_SHIM_BLOCK_RE.search(html):
        return html
    lower = html.lower()
    has_game_surface = "<canvas" in lower or "three.webglrenderer" in lower or "./_evermind_runtime/three/" in lower
    if not has_game_surface:
        return html
    return _inject_before_first_script_or_close(html, _GAME_RUNTIME_PERF_SHIM)


def _stabilize_game_weapon_state(html: str) -> str:
    if not html:
        return html
    if "_evermindSafeWeaponIndex" in html:
        return html
    if "function getWeapon()" not in html:
        return html
    if "game.ammo[game.currentWeaponIndex]" not in html:
        return html
    if "currentWeaponIndex" not in html or "const weapons = [" not in html:
        return html

    helper = (
        "\n      function _evermindSafeWeaponIndex(rawIndex){\n"
        "        if(!Array.isArray(weapons) || weapons.length === 0) return 0;\n"
        "        let safeIndex = Number(rawIndex);\n"
        "        if(!Number.isFinite(safeIndex)) safeIndex = 0;\n"
        "        safeIndex = Math.max(0, Math.min(weapons.length - 1, Math.trunc(safeIndex)));\n"
        "        if(!Array.isArray(game.ammo)) game.ammo = [];\n"
        "        weapons.forEach((weapon, idx) => {\n"
        "          const fallbackMag = Math.max(0, Number(weapon?.magSize ?? weapon?.magazine ?? weapon?.clipSize ?? 0) || 0);\n"
        "          const fallbackReserve = Math.max(0, Number(weapon?.reserve ?? weapon?.ammo ?? weapon?.stock ?? 0) || 0);\n"
        "          const slot = game.ammo[idx];\n"
        "          if(!slot || !Number.isFinite(Number(slot.mag)) || !Number.isFinite(Number(slot.reserve))){\n"
        "            game.ammo[idx] = { mag: fallbackMag, reserve: fallbackReserve };\n"
        "            return;\n"
        "          }\n"
        "          slot.mag = Math.max(0, Number(slot.mag) || 0);\n"
        "          slot.reserve = Math.max(0, Number(slot.reserve) || 0);\n"
        "        });\n"
        "        game.currentWeaponIndex = safeIndex;\n"
        "        return safeIndex;\n"
        "      }\n\n"
        "      function _evermindSafeWeapon(rawIndex = game.currentWeaponIndex){\n"
        "        const safeIndex = _evermindSafeWeaponIndex(rawIndex);\n"
        "        return weapons[safeIndex] || { name:'Fallback Weapon', type:'Weapon', mode:'Tap Fire', magSize:0, reserve:0, fireRate:1, bulletSpeed:1, damage:0, spread:0, color:0xffffff, auto:false, reload:0, pelletCount:1 };\n"
        "      }\n\n"
        "      function _evermindSafeAmmo(rawIndex = game.currentWeaponIndex){\n"
        "        const safeIndex = _evermindSafeWeaponIndex(rawIndex);\n"
        "        return game.ammo[safeIndex] || { mag:0, reserve:0 };\n"
        "      }\n"
    )

    stabilized = html
    anchor = "      function getWeapon(){"
    if anchor in stabilized:
        stabilized = stabilized.replace(anchor, helper + "\n" + anchor, 1)
    else:
        fallback_anchor = "function getWeapon(){"
        if fallback_anchor in stabilized:
            stabilized = stabilized.replace(
                fallback_anchor,
                helper.lstrip("\n") + "\nfunction getWeapon(){",
                1,
            )

    stabilized = stabilized.replace(
        "return weapons[game.currentWeaponIndex];",
        "return _evermindSafeWeapon();",
    )
    stabilized = stabilized.replace(
        "const ammo = game.ammo[game.currentWeaponIndex];",
        "const ammo = _evermindSafeAmmo();",
    )
    return stabilized


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
    html = _localize_runtime_dependencies(html)


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

    # 5b. For games: keep the first screen usable inside shorter preview windows.
    if task_type == "game":
        html = _inject_game_low_height_safety_css(html)
        html = _inject_game_compact_hud_safety_css(html)
        html = _inject_game_overlay_safe_area_css(html)
        html = _inject_game_hidden_screen_strict_css(html)
        html = _stabilize_game_weapon_state(html)
        html = _inject_three_capsule_geometry_compat_shim(html)
        html = _inject_game_runtime_perf_shim(html)
        html = _stabilize_game_pointer_lock(html)

    # 6. Remove emoji glyphs used as icons (should use SVG)
    # Only strip common UI emoji, not content emoji
    ui_emoji_pattern = re.compile(r'[\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF]')
    # Don't be too aggressive — only in elements that look like buttons/nav
    # Skip this for now to avoid breaking content

    # 7. Normalize common multi-page hook class aliases so shared assets can bind safely.
    html = _add_class_aliases(html, "page-transition", ["page-transition-overlay"])
    html = _add_class_aliases(html, "page-transition-overlay", ["page-transition"])
    html = _add_class_aliases(html, "mobile-menu-toggle", ["nav-toggle"])
    html = _postprocess_inline_script_blocks(html)
    # v6.1.13 (maintainer 2026-04-20): universal post-merge safety — applies to
    # every task type. Duplicate library scripts and IIFE-sealed handler
    # functions break games, webapps, slides, dashboards alike.
    html = _dedup_library_scripts(html)
    html = _inject_iife_handler_exports(html)
    # v6.1.14g (maintainer 2026-04-20): hardened image fallback — reviewer was
    # rejecting repeatedly because builder invented Unsplash URLs that 404.
    # Every <img> now gets an onerror handler + empty-src guard + CSS
    # gradient placeholder on parent. Purely deterministic — no AI call.
    html = _inject_image_fallback_guards(html)
    if task_type == "game":
        html = _inject_missing_game_menu_handler_shims(html)
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
